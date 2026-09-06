"""
services/openwrt.py — Connettore OpenWrt (router della LAN)
============================================================
Tutti i dati del router si prendono via **SSH** (Dropbear): `ubus call ...`
per system/interfacce/DHCP/wireless, letture di file e comandi `wg`/`nft`/
`logread` per il resto. Nessuna API HTTP (LuCI-RPC / uhttpd-mod-ubus non sono
installati sul router e non li installiamo: risorse minime, LuCI web intatta).

La connessione SSH e' **persistente e riusata** da tutti i chiamanti (un solo
handshake, canali multipli): pesa il minimo sul router. Le factory
get_router()/get_ssh() restituiscono i client reali (nessun mock: l'app
monitora sempre l'infrastruttura vera).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass
from typing import Any, Optional

import asyncssh

from config import settings
from services.errors import exc_text, ssh_error

log = logging.getLogger("openwrt")

# Backoff dopo un fallimento di connessione SSH: un router spento o una
# credenziale errata non devono causare flood di tentativi/log ad ogni tick.
_CONN_BACKOFF_BASE = 30   # secondi (primo retry)
_CONN_BACKOFF_MAX = 300   # secondi (cap a 5 min)


# ── Dataclasses risultato ──────────────────────────────────────────

@dataclass
class RouterInfo:
    hostname: str
    model: str
    os_version: str
    uptime_seconds: int
    load: list[float]
    memory_total: int
    memory_free: int
    cpu_percent: float = 0.0


@dataclass
class Interface:
    name: str
    ifname: str
    up: bool
    ip4: list[str]
    ip6: list[str]
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    mac: str


@dataclass
class DHCPLease:
    mac: str
    ip: str
    hostname: str
    expires: int


@dataclass
class ARPEntry:
    ip: str
    mac: str
    iface: str


# ── Client SSH (connessione persistente) ───────────────────────────

class SSHClient:
    """Esegue comandi sul router via SSH, riusando una singola connessione.

    asyncssh apre un canale nuovo per ogni `conn.run()`, quindi le chiamate
    concorrenti sulla stessa connessione sono sicure. Se la connessione cade,
    viene reimpostata e ristabilita al giro successivo (con backoff sui
    fallimenti ripetuti, per non martellare il router)."""

    def __init__(self):
        self.host = settings.router.host
        self.port = settings.router.port
        self.user = settings.router.user
        self.password = settings.router.password
        self.key = settings.router.ssh_key
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._lock = asyncio.Lock()
        self._conn_block_until: float = 0.0
        self._conn_fail_count: int = 0
        # Dropbear (SSH del router mini) accetta pochissimi canali simultanei:
        # aprirne piu' di uno insieme causa "Session request failed". Serializziamo
        # i comandi (un canale alla volta): gentile sul router e piu' affidabile.
        self._sem = asyncio.Semaphore(1)

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "known_hosts": None,      # rete locale fidata; valutare known_hosts esplicito
            "keepalive_interval": 20, # tiene viva la sessione (NAT/idle)
        }
        # Offriamo sia la chiave sia la password: asyncssh prova prima la chiave
        # e poi la password. Cosi' funziona sia con chiave autorizzata sul router
        # sia con la sola password (fallback), senza dover scegliere in config.
        if self.key:
            kwargs["client_keys"] = [self.key]
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    async def _get_conn(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None:
            return self._conn
        now = time.monotonic()
        if now < self._conn_block_until:
            raise ConnectionError(
                f"SSH in backoff (riprova fra {int(self._conn_block_until - now)}s)"
            )
        async with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                self._conn = await asyncssh.connect(**self._connect_kwargs())
            except Exception:
                self._conn_fail_count += 1
                delay = min(_CONN_BACKOFF_BASE * (2 ** (self._conn_fail_count - 1)),
                            _CONN_BACKOFF_MAX)
                self._conn_block_until = now + delay
                raise
            self._conn_fail_count = 0
            self._conn_block_until = 0.0
        return self._conn

    def _reset_conn(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.abort()
            except Exception:
                pass

    async def run(self, cmd: str, timeout: int = 15) -> tuple[str, str]:
        """Ritorna (stdout, stderr). Non solleva: degrada con ('', errore)."""
        try:
            conn = await self._get_conn()
        except Exception as e:
            reason = ssh_error(e)
            log.error(f"SSH connect ({self.host}): {reason}")
            return "", reason
        # Un retry: l'apertura del canale su Dropbear puo' fallire in modo
        # transitorio ("Session request failed") sotto carico; riprovare sullo
        # stesso link risolve senza dover riconnettere. Al secondo fallimento
        # reimpostiamo la connessione (probabilmente caduta).
        for attempt in (1, 2):
            try:
                async with self._sem:
                    result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
                return result.stdout or "", result.stderr or ""
            except asyncio.TimeoutError:
                log.error(f"SSH timeout: {cmd}")
                self._reset_conn()
                return "", "timeout"
            except (asyncssh.Error, ConnectionError, OSError) as e:
                if attempt == 1:
                    await asyncio.sleep(0.3)
                    continue
                reason = exc_text(e)
                log.warning(f"SSH command failed ({cmd}): {reason}")
                self._reset_conn()
                return "", reason
        return "", "unreachable"

    async def get_arp_table(self) -> list[ARPEntry]:
        stdout, _ = await self.run("cat /proc/net/arp")
        entries = []
        for line in stdout.strip().splitlines()[1:]:   # salta header
            parts = line.split()
            if len(parts) >= 6 and parts[2] == "0x2":  # 0x2 = entry completa
                entries.append(ARPEntry(ip=parts[0], mac=parts[3], iface=parts[5]))
        return entries

    async def get_nftables_rules(self) -> str:
        stdout, _ = await self.run(
            "nft list ruleset 2>/dev/null || iptables -L -n -v 2>/dev/null"
        )
        return stdout

    async def ping_host(self, ip: str, count: int = 2) -> tuple[bool, float]:
        """Ritorna (online, latency_ms)."""
        # ip interpolato in un comando shell sul router: va quotato per evitare
        # command injection (il chiamante puo' passare input non fidato).
        stdout, _ = await self.run(
            f"ping -c {int(count)} -W 1 {shlex.quote(ip)} 2>/dev/null")
        if "min/avg/max" in stdout:
            try:
                avg = float(stdout.split("min/avg/max")[1].split("/")[1])
                return True, avg
            except Exception as e:
                # Prima si ritornava 0.0 in silenzio: un output non
                # interpretabile diventava "online in 0 ms", cioe' un dato
                # inventato indistinguibile da un ping davvero istantaneo.
                log.warning(f"ping {ip}: riga min/avg/max non interpretabile "
                            f"({exc_text(e)}): {stdout.strip()[:120]}")
        return "0 packets received" not in stdout and stdout != "", 0.0

    async def logread(self, lines: int = 100, filters: list[str] | None = None,
                      exclude: list[str] | None = None) -> str:
        """Ultime `lines` righe **che soddisfano i filtri**.

        I criteri vanno in AND su grep separati: concatenarli in una stringa sola
        cercherebbe quella frase letterale, che in un log non compare mai
        (era il caso di livello + testo insieme).

        I grep stanno **prima** del `tail`, non dopo. Con il taglio prima si
        cercava solo dentro le ultime N righe: su questo router le ultime 300
        sono tutte connessioni SSH di LANMng stessa, quindi qualunque ricerca
        (`wireguard`, `dnsmasq`, ...) tornava vuota pur avendo il log pieno di
        quelle righe poco piu' indietro. `lines` ora limita il risultato, che e'
        quello che uno si aspetta da un filtro.
        """
        cmd = "logread"
        for f in (filters or []):
            if f:
                # I criteri arrivano da query param: quotare per evitare injection.
                cmd += f" | grep -i {shlex.quote(f)}"
        for f in (exclude or []):
            if f:
                cmd += f" | grep -i -v {shlex.quote(f)}"
        cmd += f" | tail -n {int(lines)}"
        stdout, _ = await self.run(cmd)
        return stdout


# ── Client dati router (ubus via SSH) ──────────────────────────────

class RouterClient:
    """Legge i dati del router via `ubus` su SSH (nessun HTTP)."""

    def __init__(self):
        self.ssh = get_ssh()

    async def _ubus(self, obj: str, method: str, params: dict | None = None) -> dict:
        """Esegue `ubus call <obj> <method> ['<json>']` e ritorna il dict.

        Degrada a {} (con warning) se l'output e' vuoto o non e' JSON valido."""
        cmd = f"ubus call {obj} {method}"
        if params:
            cmd += f" '{json.dumps(params)}'"
        stdout, stderr = await self.ssh.run(cmd)
        if not stdout.strip():
            if stderr.strip():
                log.warning(f"ubus {obj}.{method}: {stderr.strip()}")
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            log.warning(f"ubus {obj}.{method} parse error: {e}")
            return {}

    async def get_system_info(self) -> RouterInfo:
        info = await self._ubus("system", "info")
        board = await self._ubus("system", "board")
        return RouterInfo(
            hostname=board.get("hostname", "unknown"),
            model=board.get("model", "unknown"),
            os_version=board.get("release", {}).get("description", "OpenWrt"),
            uptime_seconds=info.get("uptime", 0),
            load=[r / 65535 for r in info.get("load", [0, 0, 0])],
            memory_total=info.get("memory", {}).get("total", 0),
            memory_free=info.get("memory", {}).get("free", 0),
        )

    async def _device_stats(self, dev: str) -> dict:
        """Statistiche rx/tx del device L2/L3 (non esposte da network.interface
        dump su questo router: vanno lette da network.device status)."""
        data = await self._ubus("network.device", "status", {"name": dev})
        return data.get("statistics", {}) or {}

    async def get_interfaces(self) -> list[Interface]:
        data = await self._ubus("network.interface", "dump")
        raw = data.get("interface", [])
        # I byte rx/tx non sono nel dump: li prendiamo dal device sottostante
        # (una status per device unico, concorrenti sulla connessione riusata).
        devs = {i.get("l3_device") or i.get("device") or "" for i in raw}
        devs.discard("")
        stats_list = await asyncio.gather(*[self._device_stats(d) for d in devs])
        stats_by_dev = dict(zip(devs, stats_list))

        result = []
        for iface in raw:
            dev = iface.get("l3_device") or iface.get("device") or ""
            st = stats_by_dev.get(dev, {})
            result.append(Interface(
                name=iface.get("interface", ""),
                ifname=dev,
                up=iface.get("up", False),
                ip4=[a["address"] for a in iface.get("ipv4-address", []) if a.get("address")],
                ip6=[a["address"] for a in iface.get("ipv6-address", []) if a.get("address")],
                rx_bytes=st.get("rx_bytes", 0),
                tx_bytes=st.get("tx_bytes", 0),
                rx_packets=st.get("rx_packets", 0),
                tx_packets=st.get("tx_packets", 0),
                mac=iface.get("data", {}).get("macaddr", ""),
            ))
        return result

    async def get_dhcp_leases(self) -> list[DHCPLease]:
        # dnsmasq scrive i lease attivi in /tmp/dhcp.leases:
        #   <expiry> <mac> <ip> <hostname|*> <clientid|*>
        stdout, _ = await self.ssh.run("cat /tmp/dhcp.leases 2>/dev/null")
        leases = []
        for line in stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                leases.append(DHCPLease(
                    mac=parts[1],
                    ip=parts[2],
                    hostname="" if parts[3] == "*" else parts[3],
                    expires=int(parts[0]) if parts[0].isdigit() else 0,
                ))
        if leases:
            return leases
        # Fallback per setup con odhcpd (senza file dnsmasq): ubus dhcp ipv4leases.
        data = await self._ubus("dhcp", "ipv4leases")
        for iface_leases in data.get("device", {}).values():
            for mac, info in iface_leases.items():
                leases.append(DHCPLease(
                    mac=mac,
                    ip=info.get("ipaddr", ""),
                    hostname=info.get("hostname", ""),
                    expires=info.get("expires", 0),
                ))
        return leases

    async def get_wireless_clients(self, device: str = "") -> list[dict]:
        device = device or settings.router.wireless_device
        data = await self._ubus("iwinfo", "assoclist", {"device": device})
        return data.get("results", [])


# ── Factory ───────────────────────────────────────────────────────
_router: Optional[RouterClient] = None
_ssh: Optional[SSHClient] = None


def get_router() -> RouterClient:
    global _router
    if _router is None:
        _router = RouterClient()
    return _router


# Alias storico: il resto del codice chiama ancora get_luci().
get_luci = get_router


def get_ssh() -> SSHClient:
    global _ssh
    if _ssh is None:
        _ssh = SSHClient()
    return _ssh
