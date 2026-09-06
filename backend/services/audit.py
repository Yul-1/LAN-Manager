"""
services/audit.py — Registro append-only delle azioni sensibili
===============================================================
Traccia chi ha fatto cosa con i tool di rete e il terminale SSH. Le righe
vanno sia nei log del container (visibili da `docker logs`) sia in un file
persistente nella cartella config, che e' gia' montata in scrittura.

Formato: una riga per evento, `chiave=valore` separati da spazio, con i
valori quotati solo se contengono spazi. Leggibile a occhio e con grep.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from config import settings

log = logging.getLogger("audit")

_MAX_VALUE = 500          # taglia i valori lunghi (es. comandi incollati)

# Righe in attesa di finire nell'archivio cercabile (`history.db`), svuotate dal
# collector. Il file resta la fonte di verita' e la scrittura sul database NON
# avviene qui: un audit che aspetta il disco del database sarebbe piu' fragile
# del file append-only che deve documentare. Se il collector e' fermo la coda
# si tronca da sola invece di crescere senza fine.
_coda: deque = deque(maxlen=5000)
_coda_lock = threading.Lock()


def coda_da_archiviare() -> list[str]:
    """Righe accodate dall'ultimo giro. Le consuma."""
    with _coda_lock:
        righe = list(_coda)
        _coda.clear()
    return righe


def _fmt(value) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    if len(text) > _MAX_VALUE:
        text = text[:_MAX_VALUE] + "…"
    return f'"{text}"' if (" " in text or not text) else text


def audit(event: str, **fields) -> None:
    """Registra un evento. Non solleva mai: un audit che fa cadere la richiesta
    sarebbe peggio del problema che vuole documentare (ma il fallimento si logga)."""
    line = " ".join([time.strftime("%Y-%m-%dT%H:%M:%S%z"), event]
                    + [f"{k}={_fmt(v)}" for k, v in fields.items()])
    log.info(line)
    with _coda_lock:
        _coda.append(line)
    try:
        path = Path(settings.audit_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Puo' contenere comandi digitati: leggibile solo dal proprietario. Si
        # riapplica ogni volta, cosi' un file gia' esistente con permessi larghi
        # (copiato, ripristinato da backup) viene comunque richiuso.
        if (path.stat().st_mode & 0o777) != 0o600:
            os.chmod(path, 0o600)
    except OSError as e:
        log.error(f"audit su file fallito ({settings.audit_log}): {e}")
