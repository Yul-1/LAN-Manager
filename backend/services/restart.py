"""
services/restart.py — Riavvio del servizio richiesto dalla dashboard
====================================================================
Mezza pagina Impostazioni chiede di riavviare per applicare una modifica, e
farlo voleva dire aprire una sessione SSH sul server. Qui il riavvio si chiede
dalla UI.

Come funziona: il processo **esce**, e a farlo ripartire e' Docker, grazie alla
politica di riavvio del container (`restart: unless-stopped` nei compose del
progetto). Non c'e' nessun comando `docker restart` lanciato dall'interno — che
richiederebbe di dare al container il permesso di comandare il proprio motore.

Da qui la regola: **se non c'e' chi lo faccia ripartire, il pulsante non si
mostra**. Uscire e basta lascerebbe la dashboard spenta e irraggiungibile
proprio dalla pagina che si stava usando.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import threading
from pathlib import Path

log = logging.getLogger("restart")

# Quanto si aspetta lo spegnimento pulito prima di uscire in modo brusco. Senza
# questa rete di sicurezza, una connessione che non si chiude terrebbe il
# processo vivo e il container non ripartirebbe mai.
ATTESA_DURA = 15.0

# Politiche Docker che fanno ripartire il container da sole.
POLITICHE_BUONE = ("always", "unless-stopped")

# Id del container nei percorsi dei mount che Docker inietta (/etc/hostname,
# /etc/resolv.conf): con `network_mode: host` l'hostname e' quello dell'host,
# quindi non e' una fonte utilizzabile.
_ID_RE = re.compile(r"/containers/([0-9a-f]{12,64})/")


def in_container() -> bool:
    """True se il backend gira dentro un container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        return bool(_ID_RE.search(Path("/proc/self/mountinfo").read_text()))
    except OSError:
        return False


def container_id() -> str:
    """Id del proprio container, letto dai mount. Stringa vuota se non si sa."""
    try:
        trovato = _ID_RE.search(Path("/proc/self/mountinfo").read_text())
    except OSError:
        return ""
    return trovato.group(1) if trovato else ""


async def _politica_di_riavvio(cid: str) -> str:
    """Politica di riavvio del container, chiesta al Docker locale.

    Stringa vuota = non si e' potuto sapere (socket non montato, id non
    trovato): non e' un no, e va detto come tale invece di dare per buono
    l'uno o l'altro.
    """
    if not cid:
        return ""
    try:
        from services.docker_client import EngineDockerClient, get_docker_manager
        locale = next((c for c in get_docker_manager().clients()
                       if isinstance(c, EngineDockerClient) and c.is_local), None)
        if locale is None:
            return ""
        dati = await locale.inspect(cid)
        return str(((dati.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no")
    except Exception as e:
        log.debug(f"politica di riavvio non leggibile: {e}")
        return ""


async def stato() -> dict:
    """Si puo' riavviare da qui? Con il motivo, che la pagina mostra tale quale."""
    if not in_container():
        return {"available": False, "policy": "", "container": "",
                "reason": "il servizio non gira in un container: uscire dal processo lo "
                          "spegnerebbe e basta, senza farlo ripartire"}
    cid = container_id()
    politica = await _politica_di_riavvio(cid)
    if politica and politica not in POLITICHE_BUONE:
        return {"available": False, "policy": politica, "container": cid[:12],
                "reason": f"il container ha politica di riavvio '{politica}': uscire lo "
                          f"spegnerebbe e nessuno lo riaccenderebbe. Serve "
                          f"'restart: unless-stopped' nel compose"}
    if not politica:
        # Il socket Docker non e' montato, o l'id non si e' trovato: il riavvio
        # resta possibile, ma la pagina deve dire che non e' stato verificato.
        return {"available": True, "policy": "", "container": cid[:12],
                "reason": "non e' stato possibile leggere la politica di riavvio del "
                          "container: se non e' impostata, il servizio resta spento"}
    return {"available": True, "policy": politica, "container": cid[:12], "reason": ""}


def _esci():
    """SIGTERM a se stessi: uvicorn spegne il collector, chiude lo storico e
    termina. E' lo stesso percorso di un `docker restart`, non una morte secca."""
    log.warning("riavvio richiesto dalla dashboard: spegnimento in corso")
    # Se lo spegnimento pulito si impianta, il container non ripartirebbe mai:
    # il timer e' daemon, quindi non tiene in piedi il processo se tutto va bene.
    guardia = threading.Timer(ATTESA_DURA, lambda: os._exit(0))
    guardia.daemon = True
    guardia.start()
    os.kill(os.getpid(), signal.SIGTERM)


def programma_uscita(ritardo: float = 0.5):
    """Esce fra poco, non subito: la risposta HTTP deve arrivare al browser
    prima che il processo se ne vada, altrimenti la pagina vede una connessione
    caduta e non sa se il riavvio e' partito."""
    asyncio.get_running_loop().call_later(ritardo, _esci)
