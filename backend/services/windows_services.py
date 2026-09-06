"""
services/windows_services.py — Stato dei servizi di un host Windows
===================================================================
Quarto metodo di monitoraggio accanto a Docker, systemd e healthcheck: sorveglia
un servizio Windows su una macchina della LAN, letto via SSH con PowerShell.

Ogni voce dichiara **obbligatoriamente** il proprio host: un servizio Windows
non esiste sulla macchina Linux che ospita il backend, quindi qui non c'e' un
"host di default" come per systemd.

Si usa `Get-Service` e non `Win32_Service` per una ragione precisa: un servizio
che non esiste semplicemente **non compare** nell'output, e il confronto fra i
nomi chiesti e quelli tornati distingue "non installato" da "guasto" senza
costruire un filtro WMI per ogni nome. In piu' non richiede privilegi elevati.

Un servizio non installato non e' un errore: e' lo specchio del `not-found` di
systemd, e la dashboard lo dice con parole sue.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from services.errors import ssh_error
from services.ssh_hosts import esegui_powershell, live_ssh_config, resolve_ssh_target

log = logging.getLogger("windows-services")

# Tetto sull'esecuzione del comando remoto. Piu' largo dei 10 secondi di systemd
# perche' qui ogni comando e' un avvio completo di powershell.exe: misurato a
# 2,3 secondi per tre servizi su una macchina reale.
_TIMEOUT = 20

# `Get-Service -Name` accetta i wildcard: un nome che ne contiene aggancerebbe
# decine di servizi e il confronto con i nomi chiesti darebbe risultati a caso.
CARATTERI_JOLLY = "*?"

# Stati di ServiceController, normalizzati in minuscolo con il trattino. Quelli
# non elencati passano comunque, in minuscolo: meglio uno stato sconosciuto ma
# vero che uno inventato.
_STATI = {
    "running": "running", "stopped": "stopped", "paused": "paused",
    "startpending": "start-pending", "stoppending": "stop-pending",
    "continuepending": "continue-pending", "pausepending": "pause-pending",
}

# Il servizio non e' installato su quell'host. Non e' un guasto della macchina.
NON_INSTALLATO = "not-found"


@dataclass
class WindowsServiceStatus:
    """Stato di un servizio Windows. Ricalca `UnitStatus` di systemd dove il
    concetto esiste, cosi' la pagina Servizi tratta le due famiglie allo stesso
    modo invece di avere due strade parallele."""
    name: str
    label: str = ""
    host: str = ""
    display_name: str = ""
    state: str = "unknown"
    start_type: str = ""
    critical: bool = False
    # L'host ha risposto. False = non si sa niente di questo servizio, che e'
    # diverso da "so che non e' installato".
    available: bool = False
    error: str = ""
    dashboard: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict:
        return {
            "kind": "windows_service",
            "name": self.name,
            "label": self.label or self.display_name or self.name,
            "display_name": self.display_name,
            "state": self.state,
            # Stessa chiave che la UI legge per ogni famiglia di servizi.
            "status": self.state,
            "start_type": self.start_type,
            "ok": self.ok,
            "critical": self.critical,
            "available": self.available,
            "error": self.error,
            "host": self.host,
            "dashboard": self.dashboard,
        }


class WindowsServicesMonitor:

    async def status(self, entries: list[dict]) -> list[WindowsServiceStatus]:
        """Una connessione per host, **un solo comando** con tutti i suoi nomi.

        Non si imita il canale-per-unit di systemd: li' ogni canale e' un
        `systemctl show`, qui sarebbe un avvio completo di powershell.exe, cioe'
        un paio di secondi a servizio.
        """
        if not entries:
            return []
        gruppi: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            gruppi[(e.get("host") or "").strip()].append(e)

        risultati = await asyncio.gather(
            *[self._per_host(host, voci) for host, voci in gruppi.items()],
            return_exceptions=True)

        out: list[WindowsServiceStatus] = []
        for (host, voci), esito in zip(gruppi.items(), risultati):
            if isinstance(esito, BaseException):
                # Un host che solleva non deve far sparire i servizi degli altri.
                log.warning(f"servizi Windows su {host or '?'}: "
                            f"{esito.__class__.__name__}: {esito}")
                out.extend(_non_disponibili(voci, ssh_error(esito)))
                continue
            out.extend(esito)
        return out

    async def _per_host(self, host: str, voci: list[dict]) -> list[WindowsServiceStatus]:
        if not host:
            # L'host di default e' la macchina Linux del backend, dove un
            # servizio Windows non puo' esistere: meglio dirlo che tentare.
            log.warning(f"servizi Windows senza host: {[v.get('name') for v in voci]}. "
                        f"Ogni voce deve indicare l'host su cui gira il servizio")
            return _non_disponibili(voci, "host non indicato: un servizio Windows "
                                          "vive su una macchina precisa")
        buoni = [v for v in voci if not _nome_vietato(v.get("name"))]
        cattivi = [v for v in voci if _nome_vietato(v.get("name"))]
        fuori = _non_disponibili(cattivi, "il nome contiene un carattere jolly: "
                                          "aggancerebbe servizi a caso")
        if not buoni:
            return fuori

        import asyncssh
        kwargs = resolve_ssh_target(host)
        kwargs.setdefault("connect_timeout", live_ssh_config().connect_timeout)
        nomi = [str(v.get("name") or "").strip() for v in buoni]
        try:
            async with asyncssh.connect(**kwargs) as conn:
                raw = await esegui_powershell(conn, script(nomi), timeout=_TIMEOUT)
        except Exception as e:
            motivo = ssh_error(e)
            log.warning(f"servizi Windows su {kwargs.get('username')}@{host}: {motivo}")
            return fuori + _non_disponibili(buoni, motivo)
        try:
            trovati = interpreta(raw)
        except ValueError as e:
            log.warning(f"servizi Windows su {host}: {e}")
            return fuori + _non_disponibili(buoni, str(e))
        return fuori + [_da_risposta(v, trovati.get(str(v.get("name") or "").strip().lower()))
                        for v in buoni]


# ── Comando remoto e interpretazione ───────────────────────────────

def script(nomi: list[str]) -> str:
    """Lo script PowerShell che chiede lo stato di tutti i nomi in un colpo.

    I cast a `[string]` non sono decorativi: `ConvertTo-Json` di PowerShell 5.1
    serializza gli enum come **interi**, quindi senza di essi `Status` tornerebbe
    come `4` e nessun confronto di stato funzionerebbe - in silenzio.

    `@($r)` intorno al risultato serve perche' `ConvertTo-Json` restituisce un
    oggetto quando l'esito e' uno solo e `null` quando non ce n'e' nessuno.
    """
    elenco = ",".join(f"'{n.replace(chr(39), chr(39) * 2)}'" for n in nomi)
    return "\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false",
        f"$n = @({elenco})",
        "$r = Get-Service -Name $n | Select-Object Name,DisplayName,"
        "@{n='Status';e={[string]$_.Status}},@{n='StartType';e={[string]$_.StartType}}",
        "ConvertTo-Json -InputObject @($r) -Compress -Depth 3",
    ])


def interpreta(raw: str) -> dict[str, dict]:
    """I servizi trovati, indicizzati per nome minuscolo.

    Chi non compare non e' un errore: non e' installato su quell'host, e il
    chiamante lo distingue proprio dall'assenza.
    """
    import json

    testo = (raw or "").strip()
    if not testo:
        raise ValueError("nessuna risposta da PowerShell")
    try:
        d = json.loads(testo)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON non valido da PowerShell: {e}") from e
    righe = [d] if isinstance(d, dict) else (d or [])
    if not isinstance(righe, list):
        raise ValueError(f"atteso un elenco, ricevuto {type(d).__name__}")
    return {str(r.get("Name") or "").strip().lower(): r
            for r in righe if isinstance(r, dict) and r.get("Name")}


# ── Helper ─────────────────────────────────────────────────────────

def valida_nome(nome: str) -> str:
    """Unica definizione di che cosa e' un nome di servizio accettabile.

    Vale sia per il router, che rifiuta il salvataggio, sia per il monitor, che
    incontra anche i services.yaml scritti a mano e non passati dalla UI.
    """
    nome = str(nome or "").strip()
    if not nome:
        raise ValueError("il nome del servizio e' obbligatorio")
    if any(c in nome for c in CARATTERI_JOLLY):
        raise ValueError("il nome non puo' contenere caratteri jolly (* o ?): "
                         "Get-Service li espande e aggancerebbe servizi a caso")
    return nome


def _nome_vietato(nome) -> bool:
    try:
        valida_nome(nome)
    except ValueError:
        return True
    return False


def _in_dashboard(voce: dict) -> bool:
    """Chi compare fra i servizi in evidenza della dashboard.

    Chiave assente = fuori. A differenza di systemd non si eredita da `critical`:
    li' quel ripiego serve a non cambiare un services.yaml gia' in esercizio,
    qui non esiste nessun catalogo precedente da rispettare.
    """
    return bool(voce.get("dashboard", False))


def _base(voce: dict) -> WindowsServiceStatus:
    return WindowsServiceStatus(
        name=str(voce.get("name") or "").strip(),
        label=str(voce.get("label") or "").strip(),
        host=(voce.get("host") or "").strip(),
        critical=bool(voce.get("critical", False)),
        dashboard=_in_dashboard(voce),
    )


def _non_disponibili(voci: list[dict], motivo: str) -> list[WindowsServiceStatus]:
    out = []
    for voce in voci:
        st = _base(voce)
        st.error = motivo
        out.append(st)
    return out


def _da_risposta(voce: dict, riga: dict | None) -> WindowsServiceStatus:
    st = _base(voce)
    # L'host ha risposto: da qui in poi si sa qualcosa di certo, compreso il
    # fatto che il servizio non ci sia.
    st.available = True
    if not riga:
        st.state = NON_INSTALLATO
        return st
    grezzo = str(riga.get("Status") or "").strip()
    st.state = _STATI.get(grezzo.lower(), grezzo.lower() or "unknown")
    st.display_name = str(riga.get("DisplayName") or "").strip()
    st.start_type = str(riga.get("StartType") or "").strip()
    return st


_monitor: WindowsServicesMonitor | None = None


def get_windows_services() -> WindowsServicesMonitor:
    global _monitor
    if _monitor is None:
        _monitor = WindowsServicesMonitor()
    return _monitor
