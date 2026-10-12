"""
services/snmp.py — SNMP: client, lettura dei MIB, banda per interfaccia, topologia
=================================================================================
Tutto passa dalla CLI net-snmp (`snmpget`, `snmpbulkwalk`, `snmpwalk`), gia'
installata nell'immagine per la discovery: nessuna libreria Python in piu'.

Tre consumatori:
  - la discovery (services/discovery.py, SNMPProvider) trova gli host che
    rispondono e ne legge sistema, indirizzi, vicini LLDP e tabella FDB;
  - il poller (`SnmpState.poll`, lanciato dal collector) legge a intervallo
    fisso la tabella interfacce e ne ricava la banda;
  - l'healthcheck `type: snmp` (services/healthcheck.py) legge un OID singolo.

Credenziali: mai sulla riga di comando. Ogni profilo ha un `snmp.conf` in una
cartella privata (0700, file 0600) sotto il tmpfs, passata a net-snmp con
SNMPCONFPATH: cosi' community e passphrase non compaiono in `ps`, nei log o nei
messaggi d'errore.

Nessun dato inventato: un host che non risponde e' "non raggiungibile" con il
motivo, e una velocita' non calcolabile e' `None` (un buco nel grafico).
"""
from __future__ import annotations

import asyncio
import atexit
import ipaddress
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings
from services.errors import exc_text
from services.ping import terminate_process

log = logging.getLogger("snmp")

# ── OID usati (numerici: nell'immagine non ci sono i file MIB) ──────

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
# sysUpTime e' da quanto gira l'AGENTE SNMP; hrSystemUptime (HOST-RESOURCES-MIB)
# da quanto e' accesa la macchina. Su uno switch coincidono, su un server no:
# riavviare snmpd faceva dire "acceso da 36s" a un host acceso da settimane.
HR_UPTIME = "1.3.6.1.2.1.25.1.1.0"

IF_TABLE = "1.3.6.1.2.1.2.2.1"          # ifEntry
IF_X_TABLE = "1.3.6.1.2.1.31.1.1.1"     # ifXEntry
IP_ADDR_IFINDEX = "1.3.6.1.2.1.4.20.1.2"   # ipAdEntIfIndex
LLDP_LOC_PORT = "1.0.8802.1.1.2.1.3.7.1"   # lldpLocPortEntry
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"        # lldpRemEntry
BRIDGE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"   # dot1dBasePortIfIndex
FDB = "1.3.6.1.2.1.17.4.3.1"                     # dot1dTpFdbEntry
QFDB = "1.3.6.1.2.1.17.7.1.2.2.1"                # dot1qTpFdbEntry

# Colonne di ifEntry / ifXEntry che servono (numero colonna -> campo).
_IF_COLS = {1: "index", 2: "descr", 3: "type", 4: "mtu", 5: "speed", 6: "mac",
            7: "admin", 8: "oper", 10: "in32", 14: "in_errors", 16: "out32",
            20: "out_errors"}
_IFX_COLS = {1: "name", 6: "in64", 10: "out64", 15: "high_speed", 18: "alias"}
_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant",
           6: "notPresent", 7: "lowerLayerDown"}

_OID_RE = re.compile(r"^\.?1(\.\d{1,10}){1,127}$")
# Hostname o IP: niente spazi e niente '-' iniziale, che net-snmp leggerebbe
# come un'opzione (iniezione di argomenti dalla config di un servizio).
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]][A-Za-z0-9_.:\[\]-]{0,252}$")

# Tetto alle righe di un walk: una tabella FDB enorme o un agente che non
# chiude mai il walk non devono riempire la memoria del backend.
_MAX_ROWS = 20000
# Tetto alle interfacce per host conservate nello storico.
MAX_IFACES_HISTORY = 64
_C32 = 2 ** 32


# ── Errori ─────────────────────────────────────────────────────────

class SnmpError(Exception):
    """Errore SNMP con un messaggio per l'utente, mai con le credenziali."""


class SnmpTimeout(SnmpError):
    pass


class SnmpAuthError(SnmpError):
    pass


class SnmpUnavailable(SnmpError):
    """net-snmp non installato o profilo inutilizzabile."""


def valid_oid(oid: str) -> str:
    oid = (oid or "").strip()
    if not _OID_RE.match(oid):
        raise ValueError(f"OID non valido: {oid!r} (serve la forma numerica, es. 1.3.6.1.2.1.1.3.0)")
    return oid.lstrip(".")


def valid_host(host: str) -> str:
    host = (host or "").strip()
    if not _HOST_RE.match(host):
        raise ValueError(f"host non valido: {host!r}")
    return host


# ── Parsing dell'output net-snmp ────────────────────────────────────
# Opzioni fisse: -On OID numerici, -Oe enum come numeri, -Ot timeticks grezzi,
# -Ox tutte le stringhe in esadecimale. L'ultima e' quella che conta: senza i
# MIB net-snmp sceglie da solo fra STRING e Hex-STRING in base al contenuto, e
# un MAC fatto di byte stampabili usciva come testo. In esadecimale si decide
# qui, per campo, cosa e' testo e cosa e' un indirizzo.

_LINE_RE = re.compile(r"^(\.?\d+(?:\.\d+)+) = (.*)$")
_NO_VALUE = ("No Such Object", "No Such Instance", "No more variables")


def parse_output(text: str) -> dict[str, tuple[str, Any]]:
    """`{oid: (tipo, valore)}` dall'output di snmpget/snmpwalk.

    Gli OID tornano senza il punto iniziale. Valori: bytes per le stringhe,
    int per numeri/contatori/timeticks, str per OID e IpAddress. Le righe "No
    Such Object/Instance" e la fine del MIB non producono voci.
    """
    out: dict[str, tuple[str, Any]] = {}
    current: Optional[str] = None
    raw: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line)
        if m:
            current = m.group(1).lstrip(".")
            raw[current] = m.group(2)
            if len(raw) > _MAX_ROWS:
                break
        elif current is not None:
            # Hex-STRING lunghe e stringhe con a capo continuano sulla riga dopo.
            raw[current] += "\n" + line
    for oid, val in raw.items():
        parsed = _parse_value(val)
        if parsed is not None:
            out[oid] = parsed
    return out


def _parse_value(val: str) -> Optional[tuple[str, Any]]:
    val = val.strip()
    if not val or any(val.startswith(x) for x in _NO_VALUE):
        return None
    if val == '""':
        return ("STRING", b"")
    # Con -Ot e senza MIB net-snmp stampa i Timeticks come numero nudo, senza
    # tipo (verificato su net-snmp 5.9.4): e' l'unico valore che esce cosi'.
    if re.fullmatch(r"\d+", val):
        return ("Timeticks", int(val))
    tipo, sep, resto = val.partition(":")
    if not sep:
        return ("STRING", val.encode())
    tipo, resto = tipo.strip(), resto.strip()
    if tipo == "Hex-STRING":
        try:
            return ("STRING", bytes.fromhex(" ".join(resto.split())))
        except ValueError:
            return ("STRING", resto.encode())
    if tipo == "STRING":
        if len(resto) >= 2 and resto[0] == '"' and resto[-1] == '"':
            resto = resto[1:-1]
        return ("STRING", resto.encode())
    if tipo in ("INTEGER", "Counter32", "Counter64", "Gauge32", "Timeticks",
                "UInteger32", "Unsigned32"):
        # Timeticks senza -Ot: "(1452) 0:00:14.52", il numero e' fra parentesi.
        m = re.match(r"\((\d+)\)", resto) or re.match(r"(-?\d+)", resto)
        return (tipo, int(m.group(1))) if m else None
    if tipo == "OID":
        return ("OID", resto.lstrip("."))
    if tipo in ("IpAddress", "Network Address"):
        return ("IpAddress", resto)
    return (tipo, resto)


def text(v: Any) -> str:
    """Valore come testo leggibile (le stringhe arrivano in bytes)."""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", errors="replace").rstrip("\x00").strip()
    return "" if v is None else str(v)


def mac(v: Any) -> str:
    """MAC da un OCTET STRING di 6 byte, altrimenti stringa vuota."""
    if isinstance(v, (bytes, bytearray)) and len(v) == 6:
        return ":".join(f"{b:02X}" for b in v)
    return ""


def _col(oid: str, base: str) -> Optional[tuple[int, str]]:
    """(colonna, indice) di un OID di tabella rispetto alla sua entry."""
    pref = base + "."
    if not oid.startswith(pref):
        return None
    col, _, idx = oid[len(pref):].partition(".")
    try:
        return int(col), idx
    except ValueError:
        return None


# ── Credenziali: snmp.conf privato per profilo ──────────────────────

def _quote_conf(v: str) -> str:
    # Il parser di net-snmp legge un token fra virgolette con gli escape: le
    # virgolette e le barre nel segreto non devono chiudere il valore prima.
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile_conf(profile, secret) -> str:
    """Testo di snmp.conf per un profilo. `secret(kind)` da' il segreto.

    Solleva SnmpUnavailable se manca una credenziale: interrogare senza
    darebbe solo un timeout ingannevole ("host spento") a ogni giro.
    """
    righe = ["mibs :", f"defVersion {profile.version}"]
    if profile.version in ("1", "2c"):
        community = secret("community")
        if not community:
            raise SnmpUnavailable(f"profilo {profile.name}: community non impostata "
                                  "(Impostazioni -> Segreti)")
        righe.append(f"defCommunity {_quote_conf(community)}")
    else:
        righe += [f"defSecurityName {_quote_conf(profile.user)}",
                  f"defSecurityLevel {profile.level}"]
        if profile.level in ("authNoPriv", "authPriv"):
            auth = secret("auth")
            if len(auth) < 8:
                raise SnmpUnavailable(f"profilo {profile.name}: passphrase auth assente "
                                      "o sotto gli 8 caratteri (Impostazioni -> Segreti)")
            righe += [f"defAuthType {profile.auth_protocol}",
                      f"defAuthPassphrase {_quote_conf(auth)}"]
        if profile.level == "authPriv":
            priv = secret("priv")
            if len(priv) < 8:
                raise SnmpUnavailable(f"profilo {profile.name}: passphrase priv assente "
                                      "o sotto gli 8 caratteri (Impostazioni -> Segreti)")
            righe += [f"defPrivType {profile.priv_protocol}",
                      f"defPrivPassphrase {_quote_conf(priv)}"]
    for r in righe:
        if "\n" in r or "\r" in r:
            raise SnmpUnavailable(f"profilo {profile.name}: un segreto contiene un a capo")
    return "\n".join(righe) + "\n"


class _ConfDir:
    """Cartella privata con un sottocartella per profilo.

    Il file si riscrive solo quando cambia, quindi un segreto aggiornato dalla
    UI vale dalla richiesta successiva senza riavviare il servizio."""

    def __init__(self):
        self._base: Optional[str] = None
        self._scritti: dict[str, str] = {}

    def path_for(self, name: str, contenuto: str) -> str:
        if self._base is None or not os.path.isdir(self._base):
            self._base = tempfile.mkdtemp(prefix="lanmng-snmp-")
            os.chmod(self._base, 0o700)
            # Contiene credenziali: non deve sopravvivere al processo.
            atexit.register(shutil.rmtree, self._base, ignore_errors=True)
            self._scritti = {}
        d = os.path.join(self._base, name)
        if self._scritti.get(name) != contenuto:
            os.makedirs(d, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(contenuto)
                os.replace(tmp, os.path.join(d, "snmp.conf"))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self._scritti[name] = contenuto
        return d


# ── Client ──────────────────────────────────────────────────────────

class SnmpClient:
    """Esecuzione dei comandi net-snmp, con concorrenza limitata."""

    def __init__(self, cfg=None, secret=None):
        self._cfg = cfg
        self._secret = secret
        self._conf = _ConfDir()
        self._sem: Optional[asyncio.Semaphore] = None

    @property
    def cfg(self):
        return self._cfg or settings.discovery.snmp

    def _secret_for(self, profile_name: str):
        if self._secret:
            return lambda kind: self._secret(profile_name, kind)
        from services.secrets_store import snmp_secret
        return lambda kind: snmp_secret(profile_name, kind)

    def _env(self, profile_name: str) -> dict:
        try:
            profile = self.cfg.profile(profile_name)
        except KeyError:
            raise SnmpUnavailable(f"profilo SNMP '{profile_name}' non definito")
        d = self._conf.path_for(profile.name,
                                profile_conf(profile, self._secret_for(profile.name)))
        # Ambiente minimo: niente /etc/snmp/snmp.conf ne' ~/.snmp, solo il file
        # del profilo. SNMP_PERSISTENT_DIR punta li' perche' il rootfs del
        # container e' read-only e net-snmp vuole salvare la cache v3.
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SNMPCONFPATH": d,
                "SNMP_PERSISTENT_DIR": d, "HOME": d, "MIBS": "", "MIBDIRS": d}

    def check_profile(self, profile_name: str) -> None:
        """Solleva SnmpUnavailable se il profilo non e' utilizzabile."""
        self._env(profile_name)

    def version(self, profile_name: str) -> str:
        try:
            return self.cfg.profile(profile_name).version
        except KeyError:
            raise SnmpUnavailable(f"profilo SNMP '{profile_name}' non definito")

    @staticmethod
    def _target(host: str) -> str:
        host = valid_host(host)
        try:
            if ipaddress.ip_address(host.strip("[]")).version == 6:
                return f"udp6:[{host.strip('[]')}]"
        except ValueError:
            pass
        return host

    def _base(self, cmd: str, timeout: Optional[int], retries: Optional[int]) -> list[str]:
        if not shutil.which(cmd):
            raise SnmpUnavailable(f"{cmd} (net-snmp) non installato")
        t = self.cfg.timeout if timeout is None else timeout
        r = self.cfg.retries if retries is None else retries
        return [cmd, "-On", "-Oe", "-Ot", "-Ox", "-t", str(t), "-r", str(r)]

    async def get(self, host: str, oids: list[str], profile: str = "default",
                  timeout: Optional[int] = None, retries: Optional[int] = None) -> dict:
        args = self._base("snmpget", timeout, retries) + [self._target(host)] + \
            [valid_oid(o) for o in oids]
        t = (self.cfg.timeout if timeout is None else timeout) * \
            ((self.cfg.retries if retries is None else retries) + 1) + 5
        return await self._run(args, profile, host, t)

    async def walk(self, host: str, oid: str, profile: str = "default") -> dict:
        # v1 non ha GETBULK: si ripiega sul walk classico, piu' lento.
        cmd = "snmpwalk" if self.version(profile) == "1" else "snmpbulkwalk"
        args = self._base(cmd, None, None)
        if cmd == "snmpbulkwalk":
            args.append("-Cr25")
        args += [self._target(host), valid_oid(oid)]
        return await self._run(args, profile, host, 60)

    async def _run(self, args: list[str], profile: str, host: str, timeout: float) -> dict:
        env = self._env(profile)
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, self.cfg.concurrency))
        async with self._sem:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await terminate_process(proc)
                raise SnmpTimeout(f"{host}: nessuna risposta entro {int(timeout)}s")
        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace")
        values = parse_output(stdout)
        if proc.returncode != 0 or (not values and stderr.strip()):
            # Un walk interrotto a meta' ha comunque righe utili: le teniamo
            # solo se il comando ha dato almeno quelle e nessun errore d'auth.
            errore = classify_error(stderr, host)
            if values and not isinstance(errore, SnmpAuthError):
                log.debug(f"snmp {host}: risposta parziale ({errore})")
                return values
            raise errore
        return values


def classify_error(stderr: str, host: str) -> SnmpError:
    """Errore leggibile dallo stderr di net-snmp, senza riportarne il testo
    grezzo (che in qualche versione ripete il nome utente v3)."""
    s = stderr.lower()
    if "timeout" in s or "no response" in s:
        return SnmpTimeout(f"{host}: nessuna risposta (host spento, SNMP disattivato "
                           "o community/credenziali errate)")
    if any(x in s for x in ("authentication failure", "unknown user", "wrong digest",
                            "decryption error", "authorizationerror",
                            "unsupported security level", "usmstatsunknownusernames",
                            "usmstatswrongdigests", "usmstatsdecryptionerrors")):
        return SnmpAuthError(f"{host}: credenziali SNMPv3 rifiutate dall'agente")
    if "passphrase" in s:
        return SnmpAuthError(f"{host}: passphrase SNMPv3 non valida (minimo 8 caratteri)")
    if "unknown host" in s:
        return SnmpError(f"{host}: host sconosciuto")
    prima = next((l.strip() for l in stderr.splitlines() if l.strip()), "errore sconosciuto")
    return SnmpError(f"{host}: {prima[:120]}")


# ── Lettori di alto livello ─────────────────────────────────────────

async def read_system(client: SnmpClient, host: str, profile: str) -> dict:
    v = await client.get(host, [SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, SYS_CONTACT,
                                SYS_NAME, SYS_LOCATION, HR_UPTIME], profile)
    if not v:
        raise SnmpError(f"{host}: l'agente non espone il gruppo system")
    g = lambda oid: v.get(oid, (None, None))[1]      # noqa: E731
    return {
        "descr": text(g(SYS_DESCR))[:300],
        "object_id": text(g(SYS_OBJECT_ID)),
        "uptime_s": host_uptime(v),
        "contact": text(g(SYS_CONTACT))[:120],
        "name": text(g(SYS_NAME))[:120],
        "location": text(g(SYS_LOCATION))[:120],
    }


def host_uptime(v: dict) -> Optional[int]:
    """Secondi di accensione della macchina: hrSystemUptime se c'e', altrimenti
    sysUpTime (gli apparati di rete di solito non hanno HOST-RESOURCES-MIB)."""
    for oid in (HR_UPTIME, SYS_UPTIME):
        ticks = (v.get(oid) or (None, None))[1]
        if isinstance(ticks, int):
            return ticks // 100
    return None


def build_interfaces(if_rows: dict, ifx_rows: dict) -> list[dict]:
    """Righe di ifTable + ifXTable fuse per ifIndex (funzione pura)."""
    per: dict[str, dict] = {}
    for oid, (_, val) in if_rows.items():
        c = _col(oid, IF_TABLE)
        if c and c[0] in _IF_COLS:
            per.setdefault(c[1], {})[_IF_COLS[c[0]]] = val
    for oid, (_, val) in ifx_rows.items():
        c = _col(oid, IF_X_TABLE)
        if c and c[0] in _IFX_COLS:
            per.setdefault(c[1], {})[_IFX_COLS[c[0]]] = val
    out = []
    for idx, r in per.items():
        try:
            index = int(idx)
        except ValueError:
            continue
        hc = "in64" in r and "out64" in r
        speed = r.get("high_speed")
        if not speed and isinstance(r.get("speed"), int):
            speed = r["speed"] // 1_000_000
        out.append({
            "index": index,
            "name": text(r.get("name")) or text(r.get("descr")) or f"if{index}",
            "descr": text(r.get("descr"))[:120],
            "alias": text(r.get("alias"))[:120],
            "type": r.get("type"),
            "mtu": r.get("mtu"),
            "speed_mbps": speed or None,
            "mac": mac(r.get("mac")),
            "admin": _STATUS.get(r.get("admin"), "unknown"),
            "oper": _STATUS.get(r.get("oper"), "unknown"),
            "in_octets": r.get("in64") if hc else r.get("in32"),
            "out_octets": r.get("out64") if hc else r.get("out32"),
            "counter_bits": 64 if hc else 32,
            "in_errors": r.get("in_errors"),
            "out_errors": r.get("out_errors"),
        })
    return sorted(out, key=lambda x: x["index"])


async def read_interfaces(client: SnmpClient, host: str, profile: str) -> list[dict]:
    if_rows = await client.walk(host, IF_TABLE, profile)
    try:
        ifx_rows = await client.walk(host, IF_X_TABLE, profile)
    except SnmpError as e:
        # ifXTable manca sugli agenti piu' vecchi: si resta sui contatori a 32 bit.
        log.debug(f"snmp {host}: ifXTable non disponibile ({e})")
        ifx_rows = {}
    return build_interfaces(if_rows, ifx_rows)


def build_ip_addresses(rows: dict) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for oid, (_, val) in rows.items():
        if not oid.startswith(IP_ADDR_IFINDEX + ".") or not isinstance(val, int):
            continue
        out.setdefault(val, []).append(oid[len(IP_ADDR_IFINDEX) + 1:])
    return out


def _lldp_id(subtype: Any, val: Any) -> str:
    # Sottotipi: 4 = macAddress per lo chassis, 3 = macAddress per la porta.
    if isinstance(val, (bytes, bytearray)) and len(val) == 6 and subtype in (3, 4):
        return mac(val)
    t = text(val)
    if t and t.isprintable():
        return t
    if isinstance(val, (bytes, bytearray)):
        return val.hex(":")
    return t


def build_lldp(loc_rows: dict, rem_rows: dict) -> list[dict]:
    """Vicini LLDP: porta locale + chassis/porta/nome del vicino."""
    loc: dict[str, dict] = {}
    for oid, (_, val) in loc_rows.items():
        c = _col(oid, LLDP_LOC_PORT)
        if c and c[0] in (3, 4):
            loc.setdefault(c[1], {})["id" if c[0] == 3 else "desc"] = text(val)
    rem: dict[str, dict] = {}
    for oid, (_, val) in rem_rows.items():
        c = _col(oid, LLDP_REM)
        if c and c[0] in (4, 5, 6, 7, 8, 9, 10):
            rem.setdefault(c[1], {})[c[0]] = val
    out = []
    for idx, r in rem.items():
        parti = idx.split(".")
        porta_locale = parti[1] if len(parti) >= 3 else ""
        lp = loc.get(porta_locale, {})
        out.append({
            "local_port": lp.get("desc") or lp.get("id") or porta_locale,
            "chassis": _lldp_id(r.get(4), r.get(5)),
            "port": _lldp_id(r.get(6), r.get(7)),
            "port_descr": text(r.get(8))[:120],
            "sys_name": text(r.get(9))[:120],
            "sys_descr": text(r.get(10))[:200],
        })
    return out


def build_fdb(base_rows: dict, fdb_rows: dict, qfdb_rows: dict,
              ifnames: dict[int, str]) -> list[dict]:
    """Tabella FDB: MAC appresi -> porta (ifIndex/nome).

    BRIDGE-MIB indicizza per MAC, Q-BRIDGE per (fdbId, MAC): si leggono
    entrambe perche' gli switch con le VLAN spesso riempiono solo la seconda.
    Si scartano le voci con stato `self` (4), cioe' i MAC dello switch."""
    porta_if: dict[int, int] = {}
    for oid, (_, val) in base_rows.items():
        if oid.startswith(BRIDGE_PORT_IFINDEX + ".") and isinstance(val, int):
            try:
                porta_if[int(oid.rsplit(".", 1)[1])] = val
            except ValueError:
                continue

    def voci(rows: dict, base: str, mac_len_idx: int) -> dict[str, dict]:
        per: dict[str, dict] = {}
        for oid, (_, val) in rows.items():
            c = _col(oid, base)
            if not c or c[0] not in (2, 3):
                continue
            ott = c[1].split(".")[-mac_len_idx:]
            if len(ott) != 6:
                continue
            try:
                m = ":".join(f"{int(x):02X}" for x in ott)
            except ValueError:
                continue
            per.setdefault(m, {})["port" if c[0] == 2 else "status"] = val
        return per

    tutte = voci(fdb_rows, FDB, 6)
    for m, r in voci(qfdb_rows, QFDB, 6).items():
        tutte.setdefault(m, r)
    out = []
    for m, r in tutte.items():
        if r.get("status") == 4 or not isinstance(r.get("port"), int) or r["port"] == 0:
            continue
        ifindex = porta_if.get(r["port"], r["port"])
        out.append({"mac": m, "ifindex": ifindex,
                    "port": ifnames.get(ifindex) or str(ifindex)})
    return sorted(out, key=lambda x: (x["port"], x["mac"]))


async def _walk_quiet(client: SnmpClient, host: str, oid: str, profile: str) -> dict:
    """Walk di una tabella facoltativa: assente = vuota, non un errore."""
    try:
        return await client.walk(host, oid, profile)
    except SnmpAuthError:
        raise
    except SnmpError as e:
        log.debug(f"snmp {host} {oid}: {e}")
        return {}


async def read_topology(client: SnmpClient, host: str, profile: str,
                        ifnames: dict[int, str]) -> tuple[list[dict], list[dict]]:
    loc, rem = await asyncio.gather(_walk_quiet(client, host, LLDP_LOC_PORT, profile),
                                    _walk_quiet(client, host, LLDP_REM, profile))
    base, fdb, qfdb = await asyncio.gather(
        _walk_quiet(client, host, BRIDGE_PORT_IFINDEX, profile),
        _walk_quiet(client, host, FDB, profile),
        _walk_quiet(client, host, QFDB, profile))
    return build_lldp(loc, rem), build_fdb(base, fdb, qfdb, ifnames)


# ── Banda: contatori -> bit/s ───────────────────────────────────────

def rate_bps(prev: Optional[dict], cur: dict) -> Optional[float]:
    """Velocita' in bit/s fra due letture di un contatore di ottetti.

    prev/cur: {"octets", "t" (monotonic s), "uptime_s", "bits"}. None quando il
    dato non e' confrontabile: prima lettura, contatore assente, device
    riavviato (uptime calato) o un contatore a 64 bit che torna indietro. Un
    contatore a 32 bit che scende con l'uptime che sale ha fatto il giro, e
    si corregge una volta sola (a 10 Gbit/s il giro dura pochi secondi, ma con
    letture a 30s su link domestici e' l'unico caso realistico).
    """
    if not prev or prev.get("octets") is None or cur.get("octets") is None:
        return None
    dt = cur["t"] - prev["t"]
    if dt <= 0:
        return None
    pu, cu = prev.get("uptime_s"), cur.get("uptime_s")
    if pu is not None and cu is not None and cu < pu:
        return None
    delta = cur["octets"] - prev["octets"]
    if delta < 0:
        if cur.get("bits") != 32 or prev.get("bits") != 32:
            return None
        delta += _C32
        if delta < 0:
            return None
    return round(delta * 8 / dt, 1)


# ── Topologia: chi sta su quale porta ───────────────────────────────

def _nome_corto(s: str) -> str:
    return (s or "").split(".")[0].strip().lower()


def build_topology(hosts: list[dict], devices: list[dict]) -> list[dict]:
    """Collegamenti device -> switch/AP, da LLDP (preferito) e FDB.

    hosts: dati SNMP per host ({ip, lldp, fdb, interfaces}); devices: come in
    snapshot["devices"]. Ritorna [{device, via, port, source}] con le chiavi
    dei device. Regole:
      - LLDP vince sull'FDB: e' il vicino che si dichiara, non un indizio;
      - una porta che porta a un altro host SNMP (via LLDP) e' un uplink e non
        si usa per l'FDB, che li' vede tutti i MAC "a valle";
      - fra piu' porte che vedono lo stesso MAC vince quella con meno MAC:
        e' quella piu' vicina al device;
      - nessun ciclo: un collegamento che chiuderebbe un anello si scarta.
    """
    by_mac: dict[str, dict] = {}
    by_ip: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for d in devices:
        if d.get("mac"):
            by_mac.setdefault(d["mac"].upper(), d)
        for ip in d.get("ips") or []:
            by_ip.setdefault(ip, d)
        for n in (d.get("hostname"), d.get("name")):
            if n:
                by_name.setdefault(_nome_corto(n), d)
    snmp_ips = {h.get("ip") for h in hosts}

    links: dict[str, dict] = {}
    uplink: set[tuple[str, str]] = set()
    for h in sorted(hosts, key=lambda x: x.get("ip") or ""):
        via = by_ip.get(h.get("ip"))
        if not via:
            continue
        for n in h.get("lldp") or []:
            dev = by_mac.get((n.get("chassis") or "").upper()) or \
                by_name.get(_nome_corto(n.get("sys_name")))
            if not dev or dev is via:
                continue
            if any(ip in snmp_ips for ip in dev.get("ips") or []):
                uplink.add((h["ip"], n.get("local_port") or ""))
            links.setdefault(dev["key"], {"device": dev["key"], "via": via["key"],
                                          "port": n.get("local_port") or "",
                                          "source": "lldp"})

    candidati: dict[str, tuple[int, dict]] = {}
    for h in hosts:
        via = by_ip.get(h.get("ip"))
        if not via:
            continue
        fdb = h.get("fdb") or []
        conta: dict[str, int] = {}
        for e in fdb:
            conta[e["port"]] = conta.get(e["port"], 0) + 1
        for e in fdb:
            if (h["ip"], e["port"]) in uplink:
                continue
            dev = by_mac.get(e["mac"].upper())
            if not dev or dev is via or dev["key"] in links:
                continue
            n = conta[e["port"]]
            if dev["key"] not in candidati or n < candidati[dev["key"]][0]:
                candidati[dev["key"]] = (n, {"device": dev["key"], "via": via["key"],
                                             "port": e["port"], "source": "fdb"})
    for k in sorted(candidati):
        links.setdefault(k, candidati[k][1])

    # Anelli: si tiene un collegamento solo se risalendo da `via` non si
    # torna al device stesso.
    padre: dict[str, str] = {}
    out = []
    for link in sorted(links.values(), key=lambda x: (x["source"] != "lldp", x["device"])):
        cur, visti = link["via"], {link["device"]}
        while cur in padre and cur not in visti:
            visti.add(cur)
            cur = padre[cur]
        if cur in visti:
            continue
        padre[link["device"]] = link["via"]
        out.append(link)
    return out


# ── Stato condiviso: bersagli dalla discovery, banda dal poller ─────

@dataclass
class SnmpHostData:
    ip: str
    profile: str = "default"
    reachable: bool = False
    error: str = ""
    system: dict = field(default_factory=dict)
    interfaces: list[dict] = field(default_factory=list)
    ip_addresses: dict = field(default_factory=dict)     # ifIndex -> [ip]
    lldp: list[dict] = field(default_factory=list)
    fdb: list[dict] = field(default_factory=list)
    discovered_at: int = 0
    polled_at: int = 0

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "profile": self.profile,
            "reachable": self.reachable,
            "error": self.error,
            "system": self.system,
            "interfaces": [
                {**{k: v for k, v in i.items() if k not in ("in_octets", "out_octets")},
                 "ips": self.ip_addresses.get(i["index"], [])}
                for i in self.interfaces],
            "lldp": self.lldp,
            "fdb_count": len(self.fdb),
            "discovered_at": self.discovered_at,
            "polled_at": self.polled_at,
        }


class SnmpState:
    """Host SNMP noti (dalla discovery) e banda per interfaccia (dal poller)."""

    def __init__(self):
        self.client = SnmpClient()
        self.hosts: dict[str, SnmpHostData] = {}
        self._prev: dict[tuple[str, int], dict] = {}
        self._last_poll = 0.0
        # Orologio della banda: sostituibile nei test senza toccare quello
        # globale, che serve anche all'event loop.
        self._now = time.monotonic

    # Discovery

    def set_discovered(self, results: list[SnmpHostData]) -> None:
        """Sostituisce l'elenco degli host: quelli spariti escono anche dal
        poller, i nuovi entrano. Le interfacce gia' lette restano finche' il
        poller non le rilegge, cosi' la scheda non si svuota fra i due giri."""
        nuovi = {}
        for r in results:
            vecchio = self.hosts.get(r.ip)
            # Le interfacce lette dal poller hanno la banda, quelle della
            # discovery no: finche' il poller non ripassa vincono le sue.
            if vecchio and (vecchio.polled_at or not r.interfaces):
                r.interfaces = vecchio.interfaces
                r.polled_at = vecchio.polled_at
            nuovi[r.ip] = r
        for k in [k for k in self._prev if k[0] not in nuovi]:
            del self._prev[k]
        self.hosts = nuovi

    # Poller

    def due(self) -> bool:
        cfg = settings.discovery.snmp
        if not cfg.enabled or not (cfg.interfaces or cfg.traffic) or not self.hosts:
            return False
        return self._now() - self._last_poll >= cfg.traffic_interval

    async def poll(self) -> dict[str, list[dict]]:
        """Rilegge interfacce e uptime di ogni host noto. Ritorna i punti di
        banda per lo storico: {ip: [{ifname, rx_bps, tx_bps}]}."""
        self._last_poll = self._now()
        hosts = list(self.hosts.values())
        risultati = await asyncio.gather(*[self._poll_one(h) for h in hosts],
                                         return_exceptions=True)
        punti: dict[str, list[dict]] = {}
        for h, r in zip(hosts, risultati):
            if isinstance(r, BaseException):
                log.error(f"snmp poll {h.ip}: {exc_text(r)}")
                h.reachable, h.error = False, exc_text(r)[:200]
            elif r:
                punti[h.ip] = r
        return punti

    async def _poll_one(self, h: SnmpHostData) -> list[dict]:
        try:
            up = await self.client.get(h.ip, [SYS_UPTIME, HR_UPTIME], h.profile)
            ifaces = await read_interfaces(self.client, h.ip, h.profile)
        except SnmpError as e:
            if h.reachable:
                log.warning(f"snmp {h.ip}: {e}")
            h.reachable, h.error = False, str(e)
            # Dopo un buco i contatori non si confrontano: la prossima lettura
            # buona riparte da capo invece di spalmare il traffico sul buco.
            for k in [k for k in self._prev if k[0] == h.ip]:
                del self._prev[k]
            return []
        # Per i contatori conta l'agente: e' lui che li riparte da zero.
        ticks = (up.get(SYS_UPTIME) or (None, None))[1]
        uptime = ticks // 100 if isinstance(ticks, int) else None
        acceso = host_uptime(up)
        if acceso is not None:
            h.system["uptime_s"] = acceso
        now = self._now()
        traffic = settings.discovery.snmp.traffic
        punti = []
        for i in ifaces:
            k = (h.ip, i["index"])
            cur_rx = {"octets": i.get("in_octets"), "t": now, "uptime_s": uptime,
                      "bits": i["counter_bits"]}
            cur_tx = {**cur_rx, "octets": i.get("out_octets")}
            prev = self._prev.get(k)
            if prev and prev.get("name") != i["name"]:
                prev = None
            i["rx_bps"] = rate_bps(prev and prev["rx"], cur_rx) if traffic else None
            i["tx_bps"] = rate_bps(prev and prev["tx"], cur_tx) if traffic else None
            self._prev[k] = {"name": i["name"], "rx": cur_rx, "tx": cur_tx}
            if traffic and i["oper"] == "up" and len(punti) < MAX_IFACES_HISTORY:
                punti.append({"ifname": i["name"], "rx_bps": i["rx_bps"],
                              "tx_bps": i["tx_bps"]})
        h.interfaces = ifaces
        h.reachable, h.error = True, ""
        h.polled_at = int(time.time())
        return punti

    def snapshot(self) -> dict:
        cfg = settings.discovery.snmp
        return {"enabled": cfg.enabled,
                "hosts": [h.to_dict() for h in sorted(self.hosts.values(),
                                                      key=lambda x: x.ip)]}

    def topology_input(self) -> list[dict]:
        return [{"ip": h.ip, "lldp": h.lldp, "fdb": h.fdb} for h in self.hosts.values()]


def community_in_yaml_warning() -> Optional[str]:
    """Avviso se la community sta ancora in config.yaml invece che nei segreti."""
    cfg = settings.discovery.snmp
    if not cfg.enabled or not cfg.community:
        return None
    from services.secrets_store import get_secrets_store, snmp_secret_env
    if get_secrets_store().value(snmp_secret_env("default", "community")):
        return None
    return ("SNMP: la community del profilo default sta in config.yaml; spostala nei "
            "segreti (Impostazioni -> Segreti) e togli `discovery.snmp.community`")


_state: Optional[SnmpState] = None


def get_snmp() -> SnmpState:
    global _state
    if _state is None:
        _state = SnmpState()
    return _state
