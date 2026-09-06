"""
services/host_metrics_win.py — Risorse di un host Windows
=========================================================
Gemello Windows di `host_metrics.py`. Stessa idea: **un solo comando per host,
una sola connessione**, e il lavoro di interpretazione si fa qui invece di
lasciarlo alla macchina remota.

Su Windows non esistono `/proc` e `/sys`, e la shell di default via SSH e'
`cmd.exe`: uno script POSIX non fallisce nemmeno, restituisce spazzatura. Si
manda quindi uno script PowerShell che stampa **un solo oggetto JSON**, e lo si
normalizza nella stessa forma che produce la strada POSIX, cosi' da li' in poi
il codice e' uno solo (comprese le medie su finestra, che restano in
`host_metrics.py`).

**La percentuale di CPU si calcola allo stesso modo di Linux.** Il contatore
`Win32_PerfRawData_PerfOS_Processor.PercentProcessorTime` non e' una
percentuale: e' il tempo **inattivo** in unita' da 100 ns, e viaggia insieme a
`Timestamp_Sys100NS`. La coppia (totale, inattivo) ha percio' la stessa forma di
quella che si ricava da `/proc/stat`, e `_cpu_percent()` la digerisce senza
modifiche. Vale anche per l'I/O dei dischi e `_disk_rates()`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("host-metrics-win")

# Contatori a cui non corrisponde un disco vero.
_IO_ESCLUSI = ("_total",)

# Fuori da questo intervallo non e' una temperatura. Stessa regola della strada
# POSIX: la si riusa davvero (import differito in `_temperature`), non la si
# ricopia, cosi' resta una sola definizione di "valore plausibile".
_ZERO_ASSOLUTO_DK = 2731.5


def script(top_processes: int) -> str:
    """Lo script PowerShell da mandare su stdin.

    Tre dettagli sono obbligatori, e ognuno dei tre produce dati sbagliati **in
    silenzio** se manca:

    - `$ProgressPreference` spento: altrimenti PowerShell scrive record di
      progresso serializzati (`#< CLIXML`) sullo stderr al primo uso dei moduli;
    - `[Console]::OutputEncoding` a UTF-8: PowerShell 5.1 scrive nella codepage
      della console (CP850 sotto OpenSSH) e un nome accentato tornerebbe
      illeggibile;
    - i cast espliciti a `[string]`: `ConvertTo-Json` di PowerShell 5.1
      serializza gli enum come **interi**, non come nomi.

    Vale anche un vincolo di dimensione: lo script viaggia codificato in una riga
    di comando che cmd.exe tronca a 8191 caratteri (vedi `comando_powershell`),
    e la codifica moltiplica il testo per quasi tre. Un test lo verifica.
    """
    n = max(1, min(int(top_processes or 6), 30))
    return "\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false",
        "$os = Get-CimInstance Win32_OperatingSystem",
        "$cpu = Get-CimInstance Win32_PerfRawData_PerfOS_Processor",
        "$mem = Get-CimInstance Win32_PerfRawData_PerfOS_Memory",
        "$page = Get-CimInstance Win32_PageFileUsage",
        "$ld = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'",
        "$pd = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk",
        # Le sonde ACPI richiedono una sessione elevata: se nega l'accesso resta
        # vuota e la scheda dell'host semplicemente non mostra le temperature.
        "$tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature",
        "$pr = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process",
        "$dati = @{",
        "  hostname = [string]$env:COMPUTERNAME",
        "  uptime = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds",
        "  model = [string](Get-CimInstance Win32_Processor | Select-Object -First 1).Name",
        "  cpu = @($cpu | ForEach-Object { @{ nome = [string]$_.Name;"
        " inattivo = [uint64]$_.PercentProcessorTime; ts = [uint64]$_.Timestamp_Sys100NS } })",
        "  memoria = @{ totale = [uint64]$os.TotalVisibleMemorySize * 1024;"
        " disponibile = [uint64]$mem.AvailableBytes; cache = [uint64]$mem.CacheBytes }",
        "  swap = @{ totale = [uint64](($page | Measure-Object AllocatedBaseSize -Sum).Sum) * 1MB;"
        " usata = [uint64](($page | Measure-Object CurrentUsage -Sum).Sum) * 1MB }",
        "  dischi = @($ld | ForEach-Object { @{ device = [string]$_.DeviceID;"
        " fstype = [string]$_.FileSystem; totale = [uint64]$_.Size; libero = [uint64]$_.FreeSpace } })",
        "  io = @($pd | ForEach-Object { @{ device = [string]$_.Name;"
        " letti = [uint64]$_.DiskReadBytesPersec; scritti = [uint64]$_.DiskWriteBytesPersec } })",
        "  temperature = @($tz | ForEach-Object { @{ etichetta = [string]$_.InstanceName;"
        " decikelvin = [int]$_.CurrentTemperature } })",
        f"  processi = @($pr | Where-Object {{ $_.Name -ne '_Total' -and $_.Name -ne 'Idle' }}"
        f" | Sort-Object PercentProcessorTime -Descending | Select-Object -First {n}"
        " | ForEach-Object { @{ pid = [int]$_.IDProcess; comando = [string]$_.Name;"
        " cpu = [double]$_.PercentProcessorTime; rss = [uint64]$_.WorkingSetPrivate } })",
        "}",
        "ConvertTo-Json -InputObject $dati -Compress -Depth 5",
    ])


def interpreta(raw: str) -> dict[str, Any]:
    """Normalizza il JSON nella forma che `host_metrics.Lettura` si aspetta.

    Solleva ValueError su output non interpretabile: il chiamante lo scrive
    nella riga dell'host, dove si vede. Un JSON storto e' un guasto da mostrare,
    non un motivo per pubblicare una scheda a zero.
    """
    testo = (raw or "").strip()
    if not testo:
        raise ValueError("nessuna risposta da PowerShell")
    try:
        d = json.loads(testo)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON non valido da PowerShell: {e}") from e
    if not isinstance(d, dict):
        raise ValueError(f"atteso un oggetto JSON, ricevuto {type(d).__name__}")

    memoria = _memoria(d.get("memoria"))
    return {
        "hostname": str(d.get("hostname") or "").strip(),
        "uptime_seconds": int(d.get("uptime") or 0),
        "model": str(d.get("model") or "").strip(),
        # Windows non ha un load average: nessun equivalente da inventare, la
        # scheda mostra "—" come per qualunque altro valore assente.
        "load": [],
        "cpu_totals": _cpu(d.get("cpu")),
        "memory": memoria,
        "swap": _swap(d.get("swap")),
        "disks": _dischi(d.get("dischi")),
        "disk_counters": _io(d.get("io")),
        "temperatures": _temperature(d.get("temperature")),
        # La quota di RAM per processo si ricava qui perche' serve il totale
        # della macchina: Windows non la fornisce gia' in percentuale.
        "processes": _processi(d.get("processi"), memoria["total"]),
    }


# ── Sezioni ────────────────────────────────────────────────────────

def _righe(valore: Any) -> list[dict]:
    """`ConvertTo-Json` restituisce un oggetto solo quando l'elenco ha un solo
    elemento, e `null` quando e' vuoto: qui si riportano tutti e tre i casi a una
    lista, cosi' il resto del modulo non deve ricordarselo."""
    if valore is None:
        return []
    if isinstance(valore, dict):
        return [valore]
    return [x for x in valore if isinstance(x, dict)]


def _intero(riga: dict, chiave: str) -> int:
    try:
        return int(riga.get(chiave) or 0)
    except (TypeError, ValueError):
        return 0


def _cpu(valore: Any) -> dict[str, tuple[int, int]]:
    """(totale, inattivo) per la CPU complessiva e per ogni core.

    Il "totale" e' il timestamp di sistema: fra due letture la sua differenza e'
    il tempo trascorso nelle stesse unita' del contatore di inattivita', che e'
    esattamente il rapporto che serve. `_Total` prende il nome `cpu` per
    combaciare con `/proc/stat`, dove la riga complessiva si chiama cosi'.
    """
    out: dict[str, tuple[int, int]] = {}
    for riga in _righe(valore):
        nome = str(riga.get("nome") or "").strip()
        if not nome:
            continue
        ts, inattivo = _intero(riga, "ts"), _intero(riga, "inattivo")
        if ts <= 0:
            continue
        out["cpu" if nome == "_Total" else f"cpu{nome}"] = (ts, inattivo)
    return out


def _memoria(valore: Any) -> dict:
    d = valore if isinstance(valore, dict) else {}
    totale = _intero(d, "totale")
    # AvailableBytes e' l'analogo di MemAvailable: quanto si puo' davvero usare
    # senza andare sul pagefile, non "il totale meno l'occupato".
    disponibile = _intero(d, "disponibile")
    usata = max(totale - disponibile, 0)
    return {
        "total": totale, "available": disponibile, "used": usata,
        "free": disponibile, "buffers": 0, "cached": _intero(d, "cache"),
        "used_pct": round(usata / totale * 100, 1) if totale else None,
    }


def _swap(valore: Any) -> dict:
    d = valore if isinstance(valore, dict) else {}
    totale, usata = _intero(d, "totale"), _intero(d, "usata")
    return {
        "total": totale, "used": usata,
        "used_pct": round(usata / totale * 100, 1) if totale else None,
    }


def _dischi(valore: Any) -> list[dict]:
    """Unita' logiche fisse. Su Windows la lettera **e'** il punto di mount:
    `device` e `mount` coincidono, e la scheda resta leggibile come su Linux."""
    dischi: list[dict] = []
    for riga in _righe(valore):
        lettera = str(riga.get("device") or "").strip()
        totale, libero = _intero(riga, "totale"), _intero(riga, "libero")
        if not lettera or totale <= 0:
            continue
        usato = max(totale - libero, 0)
        dischi.append({
            "device": lettera, "fstype": str(riga.get("fstype") or "").strip(),
            "mount": lettera, "total": totale, "used": usato, "free": libero,
            "used_pct": round(usato / totale * 100, 1),
        })
    dischi.sort(key=lambda d: d["mount"])
    return dischi


def _io(valore: Any) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for riga in _righe(valore):
        nome = str(riga.get("device") or "").strip()
        if not nome or nome.lower() in _IO_ESCLUSI:
            continue
        out[nome] = (_intero(riga, "letti"), _intero(riga, "scritti"))
    return out


def _temperature(valore: Any) -> list[dict]:
    """Le sonde ACPI parlano in decikelvin. Il controllo di plausibilita' e'
    quello della strada POSIX, importato invece che ricopiato: l'idea di quale
    valore sia una temperatura vera deve restare scritta in un posto solo."""
    from services.host_metrics import _milli_to_celsius   # import qui: evita il ciclo

    temps: list[dict] = []
    for riga in _righe(valore):
        dk = _intero(riga, "decikelvin")
        if dk <= 0:
            continue
        celsius = _milli_to_celsius(str(int((dk - _ZERO_ASSOLUTO_DK) * 100)))
        if celsius is None:
            continue
        etichetta = str(riga.get("etichetta") or "").strip() or "thermal"
        temps.append({"chip": "acpi", "label": etichetta, "celsius": celsius})
    return temps


def _processi(valore: Any, memoria_totale: int) -> list[dict]:
    procs: list[dict] = []
    for riga in _righe(valore):
        comando = str(riga.get("comando") or "").strip()
        if not comando:
            continue
        try:
            cpu = float(riga.get("cpu") or 0)
        except (TypeError, ValueError):
            cpu = 0.0
        rss = _intero(riga, "rss")
        procs.append({
            "pid": _intero(riga, "pid"),
            # L'utente proprietario costerebbe una query per processo: si lascia
            # vuoto invece di pagarlo o di inventarlo.
            "user": "",
            "cpu_percent": round(cpu, 1),
            "mem_percent": round(rss / memoria_totale * 100, 1) if memoria_totale else None,
            "rss": rss,
            "command": comando,
        })
    return procs
