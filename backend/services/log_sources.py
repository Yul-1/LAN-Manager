"""
services/log_sources.py — Le sorgenti di log, tutte con la stessa forma
=======================================================================
In origine la pagina Logs guardava una cosa sola: il syslog del router.
Ora le sorgenti sono quattro, e la regola che tiene insieme il tutto e' che una
riga di log ha **la stessa forma** da qualunque parte arrivi. Senza, la pagina
finirebbe con quattro render diversi e quattro modi di sbagliare.

    {"ts_ms", "ts", "level", "src", "msg", "raw", "host"}

Le quattro sorgenti, e quanto costano:

  router          `logread` via SSH sul router. Letto a richiesta, **mai
                  archiviato**: quella macchina ha poche risorse ed e' gia' lei
                  a tenere il proprio buffer.
  backend         il buffer in memoria di `log_buffer`, piu' l'archivio in
                  `history.db`. E' l'unica sorgente che oggi si perde davvero.
  audit           `config/audit.log`, il registro append-only delle azioni
                  sensibili. Il file resta la fonte di verita': qui si legge.
  journal:<host>  `journalctl` via SSH sugli host della LAN (a partire da
                  quello che ospita LANMng). Lo archivia gia' systemd.

Aggiungere una sorgente = una funzione `_leggi_<nome>` piu' una voce in
`elenco_sorgenti()`. I fallimenti sono isolati: una sorgente irraggiungibile
degrada con un motivo scritto, non con righe finte (regola 1 del progetto).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Optional

from config import settings
from services.errors import exc_text, ssh_error
from services.log_filter import regole, scarta, pattern_per_grep
from services.openwrt import get_ssh

log = logging.getLogger("log-sources")

LEVELS = ("error", "warn", "info", "debug")

# Prefisso delle sorgenti journalctl: `journal:192.0.2.10`, oppure `journal:`
# secco per l'host di default (quello configurato in `systemd`).
JOURNAL_PREFIX = "journal:"

# Sorgenti che raccontano cosa succede DENTRO il servizio o chi lo ha usato:
# richiedono una sessione vera, non basta essere in LAN (vedi routers/logs.py).
SENSITIVE = ("backend", "audit")


def is_journal(source: str) -> bool:
    return (source or "").startswith(JOURNAL_PREFIX)


def journal_host(source: str) -> str:
    return (source or "")[len(JOURNAL_PREFIX):].strip()


def is_sensitive(source: str) -> bool:
    return source in SENSITIVE or is_journal(source)


# ── Riga normalizzata ──────────────────────────────────────────────

def riga(ts_ms: Optional[int], level: str, src: str, msg: str,
         raw: str = "", host: str = "") -> dict:
    return {
        "ts_ms": ts_ms,
        "ts": _testo_ora(ts_ms),
        "level": level if level in LEVELS else "info",
        "src": src,
        "msg": msg,
        "raw": raw or (f"{_testo_ora(ts_ms)} {src}: {msg}".strip()),
        "host": host,
    }


def _testo_ora(ts_ms: Optional[int]) -> str:
    """Ora locale del backend in forma breve. Il fuso e' quello del container:
    dirlo in chiaro non serve nella riga, lo dichiara la pagina."""
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# ── Syslog del router (OpenWrt) ────────────────────────────────────
# Riga di `logread`: data, facility.livello, sorgente[pid], messaggio.
#   Sun Aug 16 19:59:00 2026 cron.err crond[3772]: USER root pid 7467 cmd ...
# L'anno non c'e' su tutte le build, quindi e' opzionale: senza questa
# tolleranza il livello finiva a leggere l'anno e ogni riga risultava "info".
_SYSLOG_RE = re.compile(
    r"^(?P<ts>\w{3}\s+\w{3}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}(?:\s+\d{4})?)\s+"
    r"(?P<facility>[a-z0-9]+\.[a-z]+)\s+"
    r"(?P<rest>.*)$",
    re.I,
)

# Parole del campo facility.livello che qualificano la riga (syslog standard).
_ERROR_WORDS = ("err", "crit", "alert", "emerg")
_WARN_WORDS = ("warn",)

# Esecuzione ordinaria di un job da parte di crond: `USER root pid 123 cmd ...`.
_CRON_JOB_RE = re.compile(r"^USER\s+\S+\s+pid\s+\d+\s+cmd\b")


def level_of(facility: str, src: str = "", msg: str = "") -> str:
    """`cron.err` -> error, `daemon.warn` -> warn, tutto il resto -> info.

    Eccezione per crond: su questo router gira come `crond -l 5`, quindi
    registra **ogni esecuzione** di un job a livello `cron.err`. Sono operazioni
    di routine (il controllo del modem, ogni minuto): lasciarle passare per
    errori tingerebbe di rosso tutto il log e nasconderebbe i guasti veri.
    """
    if src == "crond" and _CRON_JOB_RE.match(msg):
        return "info"
    severity = facility.rsplit(".", 1)[-1].lower()
    if severity.startswith(_ERROR_WORDS):
        return "error"
    if severity.startswith(_WARN_WORDS):
        return "warn"
    return "info"


def parse_syslog_line(line: str) -> dict:
    """Parsing best-effort del formato syslog OpenWrt."""
    m = _SYSLOG_RE.match(line.strip())
    if not m:
        # Riga fuori formato (continuazione, output di un comando): resta grezza.
        log.debug(f"syslog fuori formato: {line!r}")
        return {"ts": "", "ts_ms": None, "level": "info", "src": "", "msg": line,
                "raw": line, "host": ""}
    rest = m.group("rest")
    src, msg = "", rest
    if ":" in rest:
        head, tail = rest.split(":", 1)
        # La sorgente e' il nome del processo senza il pid: "crond[3772]" -> "crond".
        src, msg = head.strip().split("[")[0], tail.strip()
    return {
        "ts": m.group("ts"),
        "ts_ms": _syslog_ts_ms(m.group("ts")),
        "level": level_of(m.group("facility"), src, msg),
        "facility": m.group("facility"),
        "src": src,
        "msg": msg,
        "raw": line,
        "host": "",
    }


_MESI = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _syslog_ts_ms(ts: str) -> Optional[int]:
    """`Sun Aug 16 19:59:00 2026` -> millisecondi. L'anno puo' mancare.

    Quando manca si prende l'anno piu' recente che non collochi la riga nel
    futuro: a fine dicembre un log di gennaio appartiene all'anno dopo, e
    assumere sempre l'anno corrente lo spedirebbe undici mesi indietro,
    facendolo sparire da ogni finestra temporale.
    """
    parti = ts.split()
    if len(parti) < 4:
        return None
    try:
        mese = _MESI.get(parti[1][:3].lower())
        giorno = int(parti[2])
        ore, minuti, secondi = (int(x) for x in parti[3].split(":"))
        if not mese:
            return None
        if len(parti) >= 5:
            return int(datetime(int(parti[4]), mese, giorno, ore, minuti, secondi)
                       .timestamp() * 1000)
        adesso = datetime.now()
        for anno in (adesso.year, adesso.year - 1):
            try:
                quando = datetime(anno, mese, giorno, ore, minuti, secondi)
            except ValueError:            # 29 febbraio di un anno non bisestile
                continue
            # Un minuto di tolleranza: gli orologi di router e server non sono
            # allineati al secondo, e una riga "dal futuro" di 3 secondi e' di
            # quest'anno, non dell'anno scorso.
            if quando.timestamp() <= adesso.timestamp() + 60:
                return int(quando.timestamp() * 1000)
        return None
    except (ValueError, IndexError):
        return None


async def _leggi_router(lines: int, filtro: str, escludi: list[str]) -> tuple[list[dict], str]:
    """Il syslog del router. **Non archiviato mai**: quella macchina ha poche
    risorse ed e' gia' lei a tenere il proprio buffer.

    A differenza delle altre sorgenti, qui un guasto **si propaga**: e' il
    contratto che la pagina ha gia', e la pagina lo sa distinguere —
    router giu', backend giu' o sessione scaduta sono tre messaggi diversi.
    Degradare in silenzio qui sarebbe un passo indietro.
    """
    raw = await get_ssh().logread(lines=lines, filters=[filtro], exclude=escludi)
    return ([parse_syslog_line(l) for l in raw.strip().splitlines() if l.strip()], "")


# ── Log del backend ────────────────────────────────────────────────

async def _leggi_backend(lines: int, since_ms: Optional[int],
                         until_ms: Optional[int]) -> tuple[list[dict], str]:
    """Buffer in memoria, piu' l'archivio se la finestra chiede piu' indietro.

    Le due sorgenti si fondono e non si sommano: dopo un riavvio l'archivio
    contiene anche righe che il buffer ha gia', e mostrarle due volte farebbe
    sembrare doppio un errore che e' uno solo. Il taglio e' il timestamp della
    riga viva piu' vecchia, **esclusa**: la lettura dell'archivio e' inclusiva
    su entrambi gli estremi, quindi senza il -1 la riga esattamente sul confine
    uscirebbe da tutt'e due le parti.
    """
    from services.log_buffer import get_log_buffer

    vive = [riga(r.get("ts_ms"), r.get("level", "info"), r.get("src", ""),
                 _con_stack(r), host="") for r in get_log_buffer().tail()]
    piu_vecchia = min((r["ts_ms"] for r in vive if r["ts_ms"]), default=None)

    archiviate: list[dict] = []
    if settings.logs.persist and since_ms and (piu_vecchia is None or since_ms < piu_vecchia):
        try:
            from services.history_store import get_history
            grezze = await asyncio.to_thread(
                get_history().log_lines, "backend", since_ms,
                (piu_vecchia - 1) if piu_vecchia else until_ms,
                settings.logs.max_lines)
            archiviate = [riga(r["t"], r["level"], r["src"] or "", r["msg"],
                               host=r.get("host") or "") for r in grezze]
        except Exception as e:
            log.warning(f"archivio dei log non leggibile: {exc_text(e)}")

    return (archiviate + vive), ""


def _con_stack(r: dict) -> str:
    stack = r.get("exc") or ""
    return f"{r.get('msg', '')}\n{stack}" if stack else r.get("msg", "")


# ── Registro di audit ──────────────────────────────────────────────
# Riga scritta da services/audit.py:
#   2026-09-04T18:22:01+0200 terminal.aperta ip=192.0.2.5 host=... utente=user
# Il timestamp e' ancorato alla forma vera (`%Y-%m-%dT%H:%M:%S%z`): con un `\S+`
# qualunque riga di due parole passava per un evento di audit, e una riga fuori
# formato usciva mutilata della prima parola.
_AUDIT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?)\s+"
    r"(?P<evento>[a-z0-9_.]+)(?P<campi>.*)$", re.I)

# Eventi che raccontano un rifiuto: vanno in arancione, non annegati fra gli info.
_AUDIT_WARN = (".negata", ".rifiutato", ".rifiutata", ".fallita", ".fallito")

# Chiavi il cui valore non deve uscire dall'endpoint, quale che sia. La
# passphrase finita in chiaro in audit.log e' stata un difetto vero:
# chi legge non deve dare per scontato che chi ha scritto si sia comportato bene.
_CHIAVI_SEGRETE = re.compile(
    r"(pass|passwd|password|passphrase|token|secret|segreto|key|chiave|hash|cred)", re.I)

_COPPIA_RE = re.compile(r'([A-Za-z0-9_.\-]+)=("(?:[^"\\]|\\.)*"|\S*)')


def redigi_audit(campi: str) -> str:
    """Oscura il valore delle coppie `k=v` con una chiave sospetta."""
    def sostituisci(m: re.Match) -> str:
        return f"{m.group(1)}=***" if _CHIAVI_SEGRETE.search(m.group(1)) else m.group(0)
    return _COPPIA_RE.sub(sostituisci, campi)


def parse_audit_line(line: str) -> dict:
    line = line.rstrip("\n")
    m = _AUDIT_RE.match(line.strip())
    if not m:
        return {"ts": "", "ts_ms": None, "level": "info", "src": "", "msg": line,
                "raw": line, "host": ""}
    evento = m.group("evento")
    campi = redigi_audit(m.group("campi").strip())
    ts_ms = _iso_ts_ms(m.group("ts"))
    return {
        "ts": _testo_ora(ts_ms) or m.group("ts"),
        "ts_ms": ts_ms,
        "level": "warn" if evento.endswith(_AUDIT_WARN) else "info",
        "src": evento,
        "msg": campi,
        # `raw` e' quello redatto, non l'originale: chi esporta la pagina non
        # deve ritrovarsi in un file di testo il segreto che l'API ha oscurato.
        "raw": f"{m.group('ts')} {evento} {campi}".strip(),
        "host": "",
    }


def _iso_ts_ms(ts: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except ValueError:
        return None


async def _leggi_audit(lines: int) -> tuple[list[dict], str]:
    """Ultime righe del file. Si legge dalla coda: il registro cresce senza
    rotazione, e caricarlo tutto in memoria per mostrarne 500 righe sarebbe un
    modo di far cadere il backend il giorno in cui il file e' grande."""
    try:
        grezze = await asyncio.to_thread(_coda_file, settings.audit_log, lines)
    except FileNotFoundError:
        return [], ("il registro di audit non esiste ancora: si crea alla prima "
                    "azione sensibile (terminale o tool di rete)")
    except OSError as e:
        return [], f"registro di audit non leggibile: {exc_text(e)}"
    return [parse_audit_line(l) for l in grezze if l.strip()], ""


def _coda_file(path: str, lines: int, blocco: int = 64 * 1024) -> list[str]:
    """Ultime `lines` righe leggendo dalla fine, senza caricare tutto il file."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        fine = f.tell()
        dati = b""
        while fine > 0 and dati.count(b"\n") <= lines:
            passo = min(blocco, fine)
            fine -= passo
            f.seek(fine)
            dati = f.read(passo) + dati
    return dati.decode("utf-8", errors="replace").splitlines()[-lines:]


# ── journalctl su host remoti ──────────────────────────────────────
# Priorita' syslog (0..7) come le riporta `journalctl -o json`.
_PRIO = {0: "error", 1: "error", 2: "error", 3: "error", 4: "warn",
         5: "info", 6: "info", 7: "debug"}


def parse_journal_json(line: str, host: str = "") -> Optional[dict]:
    """Una riga di `journalctl -o json`. Ritorna None se non e' un oggetto
    utile: il journal include voci senza messaggio (i marker di rotazione)."""
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    msg = d.get("MESSAGE")
    if isinstance(msg, list):             # messaggi binari: array di byte
        msg = bytes(msg).decode("utf-8", errors="replace")
    if msg is None:
        return None
    ts_ms = None
    grezzo = d.get("__REALTIME_TIMESTAMP")
    if grezzo:
        try:
            ts_ms = int(grezzo) // 1000   # il journal conta in microsecondi
        except (TypeError, ValueError):
            ts_ms = None
    try:
        prio = int(d.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    src = d.get("SYSLOG_IDENTIFIER") or d.get("_COMM") or d.get("_SYSTEMD_UNIT") or ""
    pid = d.get("_PID") or ""
    return {
        "ts": _testo_ora(ts_ms),
        "ts_ms": ts_ms,
        "level": _PRIO.get(prio, "info"),
        "src": str(src),
        "msg": str(msg),
        "raw": f"{_testo_ora(ts_ms)} {src}[{pid}]: {msg}" if pid else
               f"{_testo_ora(ts_ms)} {src}: {msg}",
        "host": host or str(d.get("_HOSTNAME") or ""),
    }


async def _leggi_journal(host_id: str, lines: int, filtro: str,
                         since_ms: Optional[int]) -> tuple[list[dict], str]:
    """`journalctl -o json` via SSH. Riusa `resolve_ssh_target`, come fanno
    gia' systemd_monitor e host_metrics: nessuna seconda anagrafica di host."""
    import asyncssh
    from services.ssh_hosts import resolve_ssh_target

    kwargs = resolve_ssh_target(host_id)
    if not kwargs.get("host"):
        return [], ("nessun host indicato e nessun host systemd configurato: "
                    "impostalo in Impostazioni -> systemd")
    cmd = f"journalctl --no-pager -o json -n {int(lines)}"
    if since_ms:
        istante = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
        cmd += f" --since {shlex.quote(istante.strftime('%Y-%m-%d %H:%M:%S UTC'))}"
    if filtro:
        # `journalctl -g` filtra lato host: molto meno traffico SSH che portarsi
        # a casa tutto il journal per scartarlo qui. Su systemd senza PCRE2 il
        # comando fallisce, e in quel caso si ripiega sul filtro locale.
        cmd += f" -g {shlex.quote(filtro)}"
    kwargs.setdefault("connect_timeout", settings.discovery.ssh.connect_timeout)
    try:
        async with asyncssh.connect(**kwargs) as conn:
            res = await asyncio.wait_for(conn.run(cmd, check=False), timeout=30)
    except Exception as e:
        return [], (f"journalctl non raggiungibile su "
                    f"{kwargs.get('username')}@{kwargs.get('host')}: {ssh_error(e)}")

    uscita = (res.stdout or "")
    if res.exit_status and not uscita.strip():
        errore = (res.stderr or "").strip().splitlines()
        motivo = errore[-1] if errore else f"uscita {res.exit_status}"
        if "-g" in cmd and "pcre" in motivo.lower():
            return await _leggi_journal(host_id, lines, "", since_ms)
        # Il caso di gran lunga piu' comune, e va detto con la soluzione:
        # leggere il journal vuole il gruppo systemd-journal (o root).
        if "permission" in motivo.lower() or "No journal files" in motivo:
            motivo += (" — l'utente SSH deve stare nel gruppo `systemd-journal` "
                       "per leggere il journal di sistema")
        return [], f"journalctl su {kwargs.get('host')}: {motivo}"

    righe = []
    for l in uscita.splitlines():
        if not l.strip():
            continue
        r = parse_journal_json(l, host=host_id or str(kwargs.get("host") or ""))
        if r:
            righe.append(r)
    return righe, ""


# ── Elenco delle sorgenti ──────────────────────────────────────────

def elenco_sorgenti() -> list[dict]:
    """Sorgenti offerte alla pagina. Nessun nome di host vive nel frontend:
    l'elenco si costruisce qui dalla configurazione."""
    voci: list[dict] = [{
        "id": "router",
        "label": "Router (syslog)",
        "kind": "router",
        "sensitive": False,
        "persisted": False,
        "note": "letto dal router a richiesta, mai archiviato",
    }, {
        "id": "backend",
        "label": "Backend LANMng",
        "kind": "backend",
        "sensitive": True,
        "persisted": bool(settings.logs.persist),
        "note": (f"ultime {settings.logs.buffer_lines} righe in memoria"
                 + (f", archiviate per {settings.logs.retain_days} giorni"
                    if settings.logs.persist else "")),
    }]
    if settings.logs.audit_enabled:
        voci.append({
            "id": "audit",
            "label": "Registro di audit",
            "kind": "audit",
            "sensitive": True,
            "persisted": True,
            "note": "azioni sensibili: terminale SSH e tool di rete",
        })

    for host in _host_journal():
        voci.append({
            "id": f"{JOURNAL_PREFIX}{host['id']}",
            "label": f"journalctl · {host['label']}",
            "kind": "journal",
            "sensitive": True,
            "persisted": False,
            "note": "letto via SSH; lo conserva systemd sull'host",
        })
    return voci


def _host_journal() -> list[dict]:
    """Host di cui offrire il journal: quelli dichiarati in `logs.journal_hosts`,
    altrimenti l'host systemd di default piu' quelli di `discovery.ssh`."""
    from services.ssh_hosts import live_ssh_config

    if settings.logs.journal_hosts:
        return [{"id": h, "label": h} for h in settings.logs.journal_hosts]

    voci: list[dict] = []
    predefinito = (settings.systemd.ssh_host or "").strip()
    if predefinito:
        voci.append({"id": "", "label": f"{predefinito} (host di LANMng)"})
    try:
        noti = live_ssh_config().hosts
    except Exception as e:                # config illeggibile: meglio meno voci che un 500
        log.warning(f"host SSH non elencabili: {exc_text(e)}")
        noti = []
    for h in noti:
        ip = (getattr(h, "ip", "") or "").strip()
        if ip and ip != predefinito:
            voci.append({"id": ip, "label": ip})
    return voci


# ── Lettura, filtri, conteggi ──────────────────────────────────────

async def leggi(source: str = "router", lines: int = 200, filtro: str = "",
                escludi: Optional[list[str]] = None, level: str = "",
                since_ms: Optional[int] = None,
                until_ms: Optional[int] = None) -> dict:
    """Legge una sorgente e applica i filtri comuni.

    Ritorna `{lines, count, sources, levels, source, warning}`. `warning` e' il
    motivo per cui una sorgente non ha dato niente: la pagina lo mostra invece
    di far credere che il log sia vuoto.
    """
    lines = max(1, min(int(lines or 200), settings.logs.max_lines))
    escludi = [x for x in (escludi or []) if x]
    warning = ""

    if source == "backend":
        righe, warning = await _leggi_backend(lines, since_ms, until_ms)
    elif source == "audit":
        righe, warning = await _leggi_audit(lines)
    elif is_journal(source):
        righe, warning = await _leggi_journal(journal_host(source), lines, filtro, since_ms)
    else:
        source = "router"
        # Sul router i filtri viaggiano come grep PRIMA del tail: cercare dentro
        # le sole ultime N righe non trovava niente su un log dominato dal
        # rumore del monitoraggio (un difetto gia' misurato).
        # Le regole di `logs.exclude` viaggiano nello stesso grep di quelle
        # scritte in pagina: sul router il taglio alle ultime N righe avviene
        # la', quindi scartarle dopo averle ricevute lascerebbe una manciata di
        # righe invece delle N chieste.
        righe, warning = await _leggi_router(lines, filtro, escludi + pattern_per_grep(source))
        filtro, escludi = "", []          # gia' applicati sul router

    # Le regole di scarto valgono anche in lettura, non solo quando la riga
    # nasce: cosi' spariscono pure le righe archiviate prima che la regola
    # esistesse. L'audit e' escluso dentro `scarta()`, non qui.
    if regole():
        righe = [r for r in righe if not scarta(r, source)]

    righe = _filtra(righe, filtro, escludi, since_ms, until_ms)
    if len(righe) > lines:
        righe = righe[-lines:]

    # I conteggi si fanno PRIMA del filtro per livello: servono a vedere che ci
    # sono tre errori mentre si stanno guardando gli info.
    livelli = {l: 0 for l in LEVELS}
    for r in righe:
        livelli[r["level"]] = livelli.get(r["level"], 0) + 1

    if level in LEVELS:
        righe = [r for r in righe if r["level"] == level]

    return {
        "lines": righe,
        "count": len(righe),
        "sources": _per_sorgente(righe),
        "levels": livelli,
        "source": source,
        "warning": warning,
        # Quante regole di `logs.exclude` stanno togliendo righe da questa
        # sorgente. La pagina lo dichiara: un filtro che fa sparire righe senza
        # dirlo e' il modo migliore per cercare per mezz'ora una riga che c'e'.
        "filtri": sum(1 for r in regole() if r.vale_per(source)) if source != "audit" else 0,
    }


def _filtra(righe: list[dict], filtro: str, escludi: list[str],
            since_ms: Optional[int], until_ms: Optional[int]) -> list[dict]:
    if filtro:
        f = filtro.lower()
        righe = [r for r in righe if f in r.get("raw", "").lower()
                 or f in r.get("msg", "").lower()]
    for e in escludi:
        low = e.lower()
        righe = [r for r in righe if low not in r.get("raw", "").lower()
                 and low not in r.get("src", "").lower()]
    if since_ms or until_ms:
        # Una riga senza timestamp interpretabile RESTA: buttarla vorrebbe dire
        # nascondere in silenzio proprio le righe fuori formato, che sono spesso
        # le piu' interessanti (stack, continuazioni).
        righe = [r for r in righe
                 if r.get("ts_ms") is None
                 or ((not since_ms or r["ts_ms"] >= since_ms)
                     and (not until_ms or r["ts_ms"] <= until_ms))]
    return righe


def _per_sorgente(righe: list[dict]) -> list[dict]:
    """Da dove viene il rumore: la UI lo usa per proporre di nasconderlo, invece
    di lasciare che sia l'utente a scoprire da solo chi riempie il log."""
    conteggio: dict[str, int] = {}
    for r in righe:
        if r.get("src"):
            conteggio[r["src"]] = conteggio.get(r["src"], 0) + 1
    return sorted(({"src": k, "count": v} for k, v in conteggio.items()),
                  key=lambda x: (-x["count"], x["src"]))


def ora_ms() -> int:
    return int(time.time() * 1000)
