"""
services/host_metrics.py — Risorse degli host della LAN (CPU/RAM/dischi/temp)
=============================================================================
Legge le risorse di **piu' host** via SSH, con la stessa strada gia' usata per
systemd e Docker: nessun agente da installare, si riusano gli host e le
credenziali di `discovery.ssh` (`services/ssh_hosts.py`).

Un solo comando per host, una sola connessione: si scaricano i file grezzi di
/proc e /sys e il parsing si fa qui. Gli host Windows non hanno /proc e via SSH
rispondono con cmd.exe: per loro il comando e' uno script PowerShell e vive in
`host_metrics_win.py`. Le due strade si incontrano subito, in `Lettura`, e da li'
in poi il codice e' uno solo - comprese le medie su finestra. Cosi' non si dipende da tool che potrebbero
mancare (top, vmstat, lm-sensors) e non si lascia lavoro allo host remoto.

**Come si misura la CPU.** La percentuale non e' un campione di un secondo preso
chissa' quando (era il difetto delle CPU dei container, che dava il 104%): e' il
**delta di /proc/stat fra due raccolte**, cioe' la media sull'intera finestra fra
un ciclo e il precedente. La finestra viaggia insieme al dato (`window_seconds`)
e alla prima lettura il valore e' `null` — un buco, non uno zero inventato.
Stesso trattamento per l'I/O dei dischi.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings
from services import host_metrics_win as win
from services.errors import ssh_error
from services.ssh_hosts import (esegui_powershell, live_ssh_config,
                                resolve_ssh_target, risolvi_os)

log = logging.getLogger("host-metrics")

# Filesystem che non rappresentano spazio reale su disco: mostrarli riempie la
# tabella di righe che non dicono niente (e il loro "100% pieno" e' normale).
_PSEUDO_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "proc", "sysfs",
    "cgroup", "cgroup2", "ramfs", "autofs", "devpts", "debugfs", "tracefs",
    "mqueue", "hugetlbfs", "pstore", "fusectl", "configfs", "binfmt_misc",
    "securityfs", "bpf", "nsfs", "rpc_pipefs",
}
# Dispositivi a blocchi da ignorare nell'I/O: non sono dischi fisici.
_PSEUDO_BLOCK = ("loop", "ram", "zram", "sr", "fd")

_SECTOR = 512          # /proc/diskstats conta settori da 512 byte, sempre

# Finestra minima perche' un delta significhi qualcosa. Due letture ravvicinate
# (una raccolta manuale subito dopo quella del collector) dividono una manciata
# di jiffies per una frazione di secondo: misurato, dava 5,4 GB/s di scrittura
# su un disco fermo. Sotto questa soglia si aspetta, invece di pubblicare un
# numero inventato.
_MIN_WINDOW = 2.0


def _remote_command(top_processes: int) -> str:
    """Comando unico per host: stampa a sezioni il contenuto grezzo di /proc e
    /sys. Solo costrutti POSIX, perche' /bin/sh puo' essere dash."""
    n = max(1, min(int(top_processes or 6), 30))
    return "\n".join([
        'echo "##STAT"; cat /proc/stat 2>/dev/null',
        'echo "##LOADAVG"; cat /proc/loadavg 2>/dev/null',
        'echo "##MEMINFO"; cat /proc/meminfo 2>/dev/null',
        'echo "##UPTIME"; cat /proc/uptime 2>/dev/null',
        'echo "##HOSTNAME"; hostname 2>/dev/null',
        'echo "##MODEL"; grep -m1 "^model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2-',
        'echo "##DISKSTATS"; cat /proc/diskstats 2>/dev/null',
        # Dischi interi: una partizione ha il file "partition", un disco no.
        'echo "##BLOCK"; for d in /sys/class/block/*; do '
        '[ -e "$d/partition" ] || echo "${d##*/}"; done',
        # -T aggiunge il tipo di filesystem (GNU coreutils); se manca si ripiega.
        'echo "##DF"; df -P -B1 -T 2>/dev/null || df -P -B1 2>/dev/null',
        # Una sonda che non risponde (radeon senza carico) fa fallire la cat:
        # si salta quella riga, non tutta la sezione.
        'echo "##TEMP"; for f in /sys/class/hwmon/hwmon*/temp*_input; do '
        '[ -r "$f" ] || continue; d="${f%/*}"; v=$(cat "$f" 2>/dev/null) || continue; '
        '[ -n "$v" ] || continue; n=$(cat "$d/name" 2>/dev/null); '
        'l=$(cat "${f%_input}_label" 2>/dev/null); '
        'echo "$n|$l|$v"; done',
        'echo "##THERMAL"; for z in /sys/class/thermal/thermal_zone*; do '
        '[ -r "$z/temp" ] || continue; v=$(cat "$z/temp" 2>/dev/null) || continue; '
        'echo "$(cat "$z/type" 2>/dev/null)|$v"; done',
        f'echo "##PS"; ps -eo pid,user:16,pcpu,pmem,rss,comm --sort=-pcpu '
        f'--no-headers 2>/dev/null | head -n {n}',
        'echo "##END"',
    ])


# ── Modello dati ───────────────────────────────────────────────────

@dataclass
class HostResources:
    """Risorse di un host in un istante. `reachable=False` = ultimo stato noto
    piu' il motivo: mai dati inventati, mai una riga che sparisce."""
    host: str                                   # IP/hostname interrogato
    name: str = ""                              # nome leggibile (catalogo device)
    os: str = ""                                # linux | windows: con quale dialetto si e' letto
    reachable: bool = False
    # Saltato perche' l'host risultava spento: e' diverso da "acceso ma non
    # risponde", e solo il secondo caso e' un guasto da segnalare.
    skipped: bool = False
    error: str = ""
    ts: int = 0
    hostname: str = ""
    uptime_seconds: int = 0
    uptime_human: str = ""
    load: list[float] = field(default_factory=list)
    cpu: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    swap: dict = field(default_factory=dict)
    disks: list[dict] = field(default_factory=list)
    disk_io: list[dict] = field(default_factory=list)
    temperatures: list[dict] = field(default_factory=list)
    processes: list[dict] = field(default_factory=list)
    series: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "name": self.name or self.hostname or self.host,
            "os": self.os,
            "reachable": self.reachable,
            "skipped": self.skipped,
            "error": self.error,
            "ts": self.ts,
            "hostname": self.hostname,
            "uptime_seconds": self.uptime_seconds,
            "uptime_human": self.uptime_human,
            "load": self.load,
            "cpu": self.cpu,
            "memory": self.memory,
            "swap": self.swap,
            "disks": self.disks,
            "disk_io": self.disk_io,
            "temperatures": self.temperatures,
            "processes": self.processes,
            "series": self.series,
        }


@dataclass
class Lettura:
    """Una lettura grezza gia' normalizzata, da qualunque sistema arrivi.

    E' il confine fra *come* si ottengono i numeri - diverso su Linux e Windows -
    e *che cosa significano nel tempo*, che invece e' uguale per tutti e sta
    scritto una volta sola in `_componi`.
    """
    hostname: str = ""
    uptime_seconds: int = 0
    model: str = ""
    load: list[float] = field(default_factory=list)
    # (totale, inattivo) per la CPU complessiva e per ogni core
    cpu_totals: dict[str, tuple[int, int]] = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    swap: dict = field(default_factory=dict)
    disks: list[dict] = field(default_factory=list)
    # (byte letti, byte scritti) cumulativi per disco
    disk_counters: dict[str, tuple[int, int]] = field(default_factory=dict)
    temperatures: list[dict] = field(default_factory=list)
    processes: list[dict] = field(default_factory=list)


# ── Parsing delle sezioni ──────────────────────────────────────────

def _split_sections(raw: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in raw.splitlines():
        if line.startswith("##"):
            current = line[2:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def _cpu_totals(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Da /proc/stat: per "cpu" e ogni core, (jiffies totali, jiffies inattivi).

    Inattivo = idle + iowait: un core in attesa del disco non sta lavorando, e
    contarlo come occupato gonfierebbe la percentuale durante ogni scan.
    """
    out: dict[str, tuple[int, int]] = {}
    for line in lines:
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        try:
            values = [int(v) for v in parts[1:]]
        except ValueError:
            continue
        if len(values) < 5:
            continue
        idle = values[3] + values[4]
        out[parts[0]] = (sum(values), idle)
    return out


def _meminfo(lines: list[str]) -> dict[str, int]:
    mem: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            mem[key.strip()] = int(fields[0]) * 1024      # /proc/meminfo e' in kB
        except ValueError:
            continue
    return mem


def _diskstats(lines: list[str], whole_disks: set[str]) -> dict[str, tuple[int, int]]:
    """Byte letti/scritti cumulativi per disco intero."""
    out: dict[str, tuple[int, int]] = {}
    for line in lines:
        f = line.split()
        if len(f) < 10:
            continue
        name = f[2]
        if name not in whole_disks or name.startswith(_PSEUDO_BLOCK):
            continue
        try:
            out[name] = (int(f[5]) * _SECTOR, int(f[9]) * _SECTOR)
        except ValueError:
            continue
    return out


def _filesystems(lines: list[str]) -> list[dict]:
    """Righe di `df`. Con `-T` la seconda colonna e' il tipo di filesystem;
    senza, il tipo resta vuoto e si filtra solo sui mount point di sistema."""
    if not lines:
        return []
    header = lines[0].split()
    typed = len(header) > 1 and header[1].lower() == "type"
    disks: list[dict] = []
    for line in lines[1:]:
        f = line.split(None, 6 if typed else 5)
        if len(f) < (7 if typed else 6):
            continue
        device, fstype = f[0], (f[1] if typed else "")
        total, used, avail, mount = f[2 if typed else 1], f[3 if typed else 2], \
            f[4 if typed else 3], f[-1].strip()
        if fstype and fstype in _PSEUDO_FS:
            continue
        if not fstype and (device.startswith("/dev/loop") or mount.startswith("/sys")
                           or mount.startswith("/proc") or mount.startswith("/run")):
            continue
        try:
            total_b, used_b, avail_b = int(total), int(used), int(avail)
        except ValueError:
            continue
        if total_b <= 0:
            continue
        # Percentuale calcolata su used+free, non su total: e' quello che fa
        # `df`, e mostrare un numero diverso da quello del comando (su ext4 la
        # riserva del 5% per root cambia il conto di 2-3 punti) fa solo dubitare
        # della dashboard.
        usable = used_b + avail_b
        disks.append({
            "device": device, "fstype": fstype, "mount": mount,
            "total": total_b, "used": used_b, "free": avail_b,
            "used_pct": round(used_b / usable * 100, 1) if usable else None,
        })
    disks.sort(key=lambda d: d["mount"])
    return disks


def _temperatures(hwmon: list[str], thermal: list[str]) -> list[dict]:
    temps: list[dict] = []
    for line in hwmon:
        chip, _, rest = line.partition("|")
        label, _, value = rest.partition("|")
        celsius = _milli_to_celsius(value)
        if celsius is None:
            continue
        temps.append({"chip": chip.strip(), "label": (label.strip() or chip.strip()),
                      "celsius": celsius})
    if temps:
        return temps
    # Ripiego: alcune macchine (VM, SBC) espongono solo le thermal zone.
    for line in thermal:
        zone, _, value = line.partition("|")
        celsius = _milli_to_celsius(value)
        if celsius is None:
            continue
        temps.append({"chip": "thermal", "label": zone.strip() or "zone",
                      "celsius": celsius})
    return temps


def _milli_to_celsius(value: str) -> Optional[float]:
    try:
        milli = int(value.strip())
    except (ValueError, AttributeError):
        return None
    # Fuori da questo intervallo non e' una temperatura: le sonde scollegate
    # restituiscono valori di comodo (0, -274, numeri enormi).
    celsius = milli / 1000
    return round(celsius, 1) if -50 < celsius < 150 else None


def _processes(lines: list[str]) -> list[dict]:
    procs: list[dict] = []
    for line in lines:
        f = line.split(None, 5)
        if len(f) < 6:
            continue
        try:
            procs.append({
                "pid": int(f[0]), "user": f[1],
                "cpu_percent": float(f[2]), "mem_percent": float(f[3]),
                "rss": int(f[4]) * 1024,      # ps riporta i kB
                "command": f[5].strip(),
            })
        except ValueError:
            continue
    return procs


def _fmt_uptime(seconds: int) -> str:
    d, h, m = seconds // 86400, (seconds % 86400) // 3600, (seconds % 3600) // 60
    if d:
        return f"{d}g {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _leggi_posix(raw: str) -> Lettura:
    """Le sezioni `##NOME` dello script /bin/sh, normalizzate.

    E' il gemello di `host_metrics_win.interpreta`: stessa uscita, sorgente
    diversa. Non calcola niente che dipenda dal tempo - quello e' compito di
    `_componi`, che vale per tutti i sistemi.
    """
    s = _split_sections(raw)
    lettura = Lettura(hostname=(s.get("HOSTNAME") or [""])[0].strip(),
                      model=(s.get("MODEL") or [""])[0].strip())

    uptime_line = (s.get("UPTIME") or [""])[0].split()
    if uptime_line:
        lettura.uptime_seconds = int(float(uptime_line[0]))
    load_line = (s.get("LOADAVG") or [""])[0].split()
    if len(load_line) >= 3:
        lettura.load = [float(x) for x in load_line[:3]]

    lettura.cpu_totals = _cpu_totals(s.get("STAT", []))
    lettura.memory, lettura.swap = _memory_sections(_meminfo(s.get("MEMINFO", [])))
    whole = {line.strip() for line in s.get("BLOCK", []) if line.strip()}
    lettura.disk_counters = _diskstats(s.get("DISKSTATS", []), whole)
    lettura.disks = _filesystems(s.get("DF", []))
    lettura.temperatures = _temperatures(s.get("TEMP", []), s.get("THERMAL", []))
    lettura.processes = _processes(s.get("PS", []))
    return lettura


# ── Servizio ───────────────────────────────────────────────────────

class HostMetricsCollector:
    """Raccoglie le risorse degli host configurati e ne tiene la storia.

    Lo stato che porta con se' e' il minimo necessario a derivare le velocita':
    i contatori della lettura precedente per host, piu' la serie storica.
    """

    def __init__(self):
        self._prev: dict[str, dict[str, Any]] = {}
        self._series: dict[str, list[dict]] = {}
        self._last: dict = {}
        self._last_run: float = 0.0

    # -- elenco degli host --------------------------------------------------
    def targets(self) -> list[str]:
        """Host da interrogare: quelli elencati in `host_metrics.hosts`,
        altrimenti tutti quelli della discovery SSH (config riletta dal file,
        cosi' un host aggiunto dalla UI entra al ciclo successivo)."""
        cfg = settings.host_metrics
        explicit = [h.strip() for h in (cfg.hosts or []) if h and h.strip()]
        if explicit:
            return explicit
        ssh = live_ssh_config()
        if not ssh.enabled:
            return []
        return [h.ip for h in ssh.hosts if h.ip]

    def last(self) -> dict:
        return self._last

    def due(self) -> bool:
        """True se e' passato l'intervallo configurato dall'ultima raccolta."""
        interval = max(int(settings.host_metrics.interval or 0), 0)
        return (time.monotonic() - self._last_run) >= interval

    # -- raccolta -----------------------------------------------------------
    async def collect(self, online: Optional[set[str]] = None) -> dict:
        """Interroga tutti gli host e aggiorna la vista corrente.

        `online` (facoltativo) e' l'insieme degli IP che hanno risposto al ping
        in questo ciclo: gli altri si saltano, perche' una connect verso un host
        spento costa il timeout intero.
        """
        cfg = settings.host_metrics
        if not cfg.enabled:
            self._last = {"enabled": False, "hosts": [], "ts": int(time.time())}
            return self._last

        hosts = self.targets()
        self._last_run = time.monotonic()
        if not hosts:
            self._last = {"enabled": True, "hosts": [], "ts": int(time.time()),
                          "warning": "nessun host SSH configurato: aggiungine uno in "
                                     "discovery.ssh.hosts o in host_metrics.hosts"}
            return self._last

        names = _catalog_names()
        results = await asyncio.gather(
            *[self._one(ip, names.get(ip, ""), online) for ip in hosts],
            return_exceptions=True,
        )
        rows: list[dict] = []
        for ip, res in zip(hosts, results):
            if isinstance(res, BaseException):
                # Un host che solleva non deve far sparire gli altri dalla vista.
                log.warning(f"risorse {ip}: {res.__class__.__name__}: {res}")
                rows.append(HostResources(host=ip, name=names.get(ip, ""),
                                          error=ssh_error(res),
                                          ts=int(time.time())).to_dict())
                continue
            rows.append(res.to_dict())
        self._last = {
            "enabled": True,
            "hosts": rows,
            "reachable": sum(1 for r in rows if r["reachable"]),
            "total": len(rows),
            "interval": int(cfg.interval or 0),
            "ts": int(time.time()),
        }
        return self._last

    async def _one(self, ip: str, name: str, online: Optional[set[str]]) -> HostResources:
        cfg = settings.host_metrics
        res = HostResources(host=ip, name=name, ts=int(time.time()))
        res.series = self._series.get(ip, [])

        if cfg.skip_offline and online is not None and ip not in online:
            res.skipped = True
            res.error = "host non raggiungibile (nessuna risposta al ping)"
            # Il ciclo successivo non deve calcolare una media su una finestra
            # che comprende il periodo di spegnimento.
            self._prev.pop(ip, None)
            return res

        raw, error, so = await self._fetch(ip)
        if raw is None:
            res.error = error
            res.os = so
            # Senza una lettura buona la finestra si interrompe: il ciclo
            # successivo ricomincia a contare invece di mediare su un buco.
            self._prev.pop(ip, None)
            return res
        try:
            self._parse_into(res, raw, so)
        except Exception as e:
            log.warning(f"risorse {ip}: output non interpretabile ({e})")
            res.error = f"output non interpretabile: {e}"
            return res
        res.reachable = True
        self._push_series(ip, res)
        return res

    async def _fetch(self, ip: str) -> tuple[Optional[str], str, str]:
        """Ritorna (output, errore, sistema): l'errore risale al chiamante invece
        di restare solo nel log, perche' la scheda dell'host deve dire *perche'*
        non e' raggiungibile.

        Il sistema si risolve sulla connessione appena aperta, quindi la sonda
        non costa mai una connessione in piu'.
        """
        import asyncssh
        cfg = settings.host_metrics
        ssh = live_ssh_config()
        kwargs = resolve_ssh_target(ip)
        # resolve_ssh_target non impone un tetto sulla connect: senza, un host
        # con la porta 22 filtrata blocca la raccolta per l'intero timeout TCP.
        kwargs.setdefault("connect_timeout", ssh.connect_timeout)
        try:
            async with asyncssh.connect(**kwargs) as conn:
                so = await risolvi_os(ip, conn)
                if so == "windows":
                    raw = await esegui_powershell(conn, win.script(cfg.top_processes),
                                                  timeout=cfg.command_timeout)
                    return raw, "", so
                if so != "linux":
                    # Mandare comandi a caso a un host sconosciuto produrrebbe una
                    # scheda vuota che sembra un guasto della macchina remota.
                    return None, ("sistema operativo non riconosciuto: dichiaralo con "
                                  "`os:` fra gli host SSH in Impostazioni"), so
                result = await asyncio.wait_for(
                    conn.run(_remote_command(cfg.top_processes), check=False),
                    timeout=cfg.command_timeout)
                return (result.stdout or ""), "", so
        except Exception as e:
            reason = ssh_error(e)
            log.warning(f"risorse {kwargs.get('username')}@{ip}: {reason}")
            return None, reason, ""

    def _parse_into(self, res: HostResources, raw: str, so: str) -> None:
        """Interpreta la lettura grezza col dialetto giusto e la compone.

        Il dialetto finisce qui: `_componi` non sa piu' da che sistema arrivino
        i numeri, e la matematica della finestra resta scritta una volta sola.
        """
        res.os = so
        lettura = Lettura(**win.interpreta(raw)) if so == "windows" else _leggi_posix(raw)
        self._componi(res, lettura)

    def _componi(self, res: HostResources, lettura: Lettura) -> None:
        now = time.monotonic()
        prev = self._prev.get(res.host) or {}

        res.hostname = lettura.hostname
        res.uptime_seconds = lettura.uptime_seconds
        if res.uptime_seconds:
            res.uptime_human = _fmt_uptime(res.uptime_seconds)
        res.load = lettura.load

        # Un riavvio azzera i contatori: la finestra precedente non e' piu'
        # confrontabile e va buttata, altrimenti si disegnano picchi inventati.
        rebooted = bool(prev.get("uptime")) and res.uptime_seconds < prev["uptime"]
        if rebooted:
            prev = {}

        totals = lettura.cpu_totals
        elapsed = now - prev["t"] if prev.get("t") else 0.0
        usable = elapsed >= _MIN_WINDOW
        res.cpu = self._cpu_section(totals, (prev.get("cpu") or {}) if usable else {},
                                    elapsed if usable else 0.0, lettura.model)

        res.memory, res.swap = lettura.memory, lettura.swap

        disk_counters = lettura.disk_counters
        res.disks = lettura.disks
        res.disk_io = _disk_rates(disk_counters, (prev.get("disk") or {}) if usable else {},
                                  elapsed if usable else 0.0)
        res.temperatures = lettura.temperatures
        res.processes = lettura.processes

        # Il punto di partenza si sposta solo quando la finestra e' stata usata:
        # se lo si aggiornasse ad ogni lettura, due raccolte ravvicinate
        # rimetterebbero il cronometro a zero e le medie non arriverebbero mai.
        if usable or not prev.get("t"):
            self._prev[res.host] = {"t": now, "cpu": totals, "disk": disk_counters,
                                    "uptime": res.uptime_seconds}

    @staticmethod
    def _cpu_section(totals: dict, prev_totals: dict, elapsed: float,
                     model: str = "") -> dict:
        cores = sorted(k for k in totals if k != "cpu")
        return {
            "model": model,
            "cores": len(cores),
            "percent": _cpu_percent(totals.get("cpu"), prev_totals.get("cpu")),
            "per_core": [_cpu_percent(totals.get(c), prev_totals.get(c)) for c in cores],
            # Su quanti secondi e' calcolata la media: senza, un numero secco non
            # dice se descrive un istante o un minuto.
            "window_seconds": round(elapsed, 1) if elapsed > 0 else None,
        }

    def _push_series(self, ip: str, res: HostResources) -> None:
        """Aggiunge un punto alla storia dell'host. Solo i valori che si guardano
        nel tempo: la serie viaggia nello snapshot ad ogni update WebSocket."""
        temps = [t["celsius"] for t in res.temperatures]
        point = {
            "t": int(time.time() * 1000),
            "cpu": res.cpu.get("percent"),
            "mem": (res.memory or {}).get("used_pct"),
            "swap": (res.swap or {}).get("used_pct"),
            "load1": res.load[0] if res.load else None,
            "temp": max(temps) if temps else None,
        }
        series = self._series.setdefault(ip, [])
        series.append(point)
        limit = max(int(settings.host_metrics.history_points or 60), 10)
        while len(series) > limit:
            series.pop(0)
        res.series = series


def _cpu_percent(now: Optional[tuple], before: Optional[tuple]) -> Optional[float]:
    """Percentuale occupata fra due letture di /proc/stat.

    None quando il confronto non ha senso (prima lettura, core comparso,
    contatori azzerati): il grafico mostra un buco, non uno zero inventato.
    """
    if not now or not before:
        return None
    total_delta = now[0] - before[0]
    idle_delta = now[1] - before[1]
    if total_delta <= 0 or idle_delta < 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)


def _memory_sections(mem: dict[str, int]) -> tuple[dict, dict]:
    total = mem.get("MemTotal", 0)
    # MemAvailable e' la stima del kernel di quanto e' davvero disponibile senza
    # swappare: "total - free" conterebbe come occupata tutta la cache.
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(total - available, 0)
    memory = {
        "total": total, "available": available, "used": used,
        "free": mem.get("MemFree", 0),
        "buffers": mem.get("Buffers", 0), "cached": mem.get("Cached", 0),
        "used_pct": round(used / total * 100, 1) if total else None,
    }
    swap_total = mem.get("SwapTotal", 0)
    swap_used = max(swap_total - mem.get("SwapFree", 0), 0)
    swap = {
        "total": swap_total, "used": swap_used,
        "used_pct": round(swap_used / swap_total * 100, 1) if swap_total else None,
    }
    return memory, swap


def _disk_rates(now: dict[str, tuple[int, int]], before: dict[str, tuple[int, int]],
                elapsed: float) -> list[dict]:
    """Byte/s letti e scritti per disco, derivati dai contatori cumulativi."""
    rows: list[dict] = []
    for name in sorted(now):
        read_bps = write_bps = None
        old = before.get(name)
        if old and elapsed > 0 and now[name][0] >= old[0] and now[name][1] >= old[1]:
            read_bps = round((now[name][0] - old[0]) / elapsed, 1)
            write_bps = round((now[name][1] - old[1]) / elapsed, 1)
        rows.append({"device": name, "read_bps": read_bps, "write_bps": write_bps,
                     "read_total": now[name][0], "write_total": now[name][1]})
    return rows


def _catalog_names() -> dict[str, str]:
    """Nome leggibile per IP dal catalogo dispositivi: la pagina Risorse deve
    dire il nome dell'host, non solo il suo indirizzo. Nessun nome nel codice."""
    try:
        from services.device_store import get_device_store
        return {ip: (entry.get("name") or "")
                for ip, entry in get_device_store().catalog_by_ip().items()}
    except Exception as e:
        log.debug(f"catalogo nomi non disponibile: {e}")
        return {}


_collector: HostMetricsCollector | None = None


def get_host_metrics() -> HostMetricsCollector:
    global _collector
    if _collector is None:
        _collector = HostMetricsCollector()
    return _collector
