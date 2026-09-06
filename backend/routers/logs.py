"""
routers/logs.py — Log, da tutte le sorgenti
===========================================
In origine qui c'era una sola rotta e una sola sorgente (il syslog del
router). Ora le sorgenti sono quattro e stanno in `services/log_sources.py`;
questo modulo resta sottile: valida l'input, decide i permessi e impagina.

I parser del syslog restano **ri-esportati** da qui perche' e' da qui che li
importa la suite gia' scritta (`tests/unit/test_logs_parser.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config import settings
from middleware.auth import require_session
from services import log_sources
# `_level_of` e `_parse_syslog_line` restano ri-esportati da qui: e' da qui che
# li importa la suite gia' scritta (tests/unit/test_logs_parser.py).
from services.log_sources import (LEVELS, elenco_sorgenti,  # noqa: F401
                                  is_sensitive, level_of as _level_of,
                                  parse_syslog_line as _parse_syslog_line)

router = APIRouter()
log = logging.getLogger("logs")

# Finestre offerte dalla pagina. Elenco chiuso, come per `/api/history`: un
# `since` libero dal client sarebbe un modo di chiedere al router mezzo mese.
PERIODI = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "": 0}


def _permessi(source: str, request: Request) -> None:
    """Le sorgenti nuove non sono della stessa categoria del syslog del router.

    `audit` racconta chi ha fatto cosa, `backend` puo' contenere percorsi ed
    errori interni, `journal` e' il log di sistema di una macchina: tutte e tre
    vogliono una sessione vera, senza le deroghe di `auth.method` o
    `bypass_lan`. La sorgente `router` resta com'era: nessuna regressione.
    """
    if is_sensitive(source):
        require_session(request)


def _finestra(period: str, since: Optional[int]) -> Optional[int]:
    """Millisecondi da cui partire. `since` esplicito (in secondi epoch) vince."""
    if since:
        return int(since) * 1000
    secondi = PERIODI.get(period or "")
    if secondi is None:
        raise HTTPException(status_code=400,
                            detail=f"periodo non valido: usa {', '.join(k for k in PERIODI if k)}")
    return (log_sources.ora_ms() - secondi * 1000) if secondi else None


def _escludi(exclude: str) -> list[str]:
    return [x.strip() for x in (exclude or "").split(",") if x.strip()]


@router.get("/sources")
async def get_sources(request: Request):
    """Sorgenti disponibili, con quali servono una sessione e quali si archiviano.

    La pagina si popola da qui: nessun nome di host o di macchina deve vivere
    nel frontend (regola 2 del progetto).
    """
    return {"sources": elenco_sorgenti(),
            "tail_interval": settings.logs.tail_interval,
            "max_lines": settings.logs.max_lines}


@router.get("/")
async def get_logs(request: Request, lines: int = 200, filter: str = "",
                   level: str = "", exclude: str = "", source: str = "router",
                   period: str = "", since: Optional[int] = None):
    """
    Righe di log da una sorgente.
      source:  router | backend | audit | journal:<host>
      filter:  testo da cercare (es. "wireguard", "dnsmasq")
      exclude: criteri da NASCONDERE, separati da virgola (es. "dropbear,crond")
      level:   error | warn | info | debug
      period:  15m | 1h | 6h | 24h | 7d  (oppure `since`, epoch in secondi)

    Il filtro per livello si applica **dopo** il parsing, non con un grep sul
    testo: cosi' quello che si chiede combacia con il livello mostrato accanto
    alla riga (i job di crond, per dire, dicono `cron.err` ma sono normali).

    `exclude` esiste perche' il monitoraggio si vede nel log che sta leggendo:
    ogni giro apre connessioni SSH al router, e il demone le registra. Senza un
    modo per nasconderle, il log utile e' sepolto sotto le righe di LANMng.
    """
    _permessi(source, request)
    return await log_sources.leggi(
        source=source, lines=lines, filtro=filter, escludi=_escludi(exclude),
        level=level, since_ms=_finestra(period, since))


@router.get("/stream")
async def stream_logs(request: Request, filter: str = "", level: str = "",
                      exclude: str = "", source: str = "router",
                      lines: int = 200):
    """Segui in tempo reale, in NDJSON: una riga JSON per evento.

    Stessa strada dei tool di rete (`/api/tools/stream`, 0.1.72): il WebSocket
    `/ws` e' un broadcast del collector senza sottoscrizioni per client, quindi
    usarlo vorrebbe dire mandare a ogni dashboard aperta i log filtrati da
    qualcun altro.

    Per `backend` e `audit` e' un inseguimento vero (buffer in memoria e coda
    dell'audit). Per `router` e `journal` e' un ri-controllo ogni
    `logs.tail_interval` secondi: sul router **non si tiene aperto un canale**,
    perche' Dropbear ne ha pochissimi e servono al collector.
    """
    _permessi(source, request)
    escludi = _escludi(exclude)
    attesa = max(settings.logs.tail_interval, settings.logs.tail_interval_min)
    scadenza = asyncio.get_event_loop().time() + max(settings.logs.tail_max_seconds, attesa)

    async def eventi():
        yield json.dumps({"type": "start", "source": source, "interval": attesa,
                          "max_seconds": settings.logs.tail_max_seconds}) + "\n"
        visti: set[str] = set()
        try:
            # Primo giro: si dichiara da dove si parte senza rimandare tutto il
            # log gia' in pagina. Le chiavi servono a non ripetere le righe.
            primo = await log_sources.leggi(source=source, lines=lines, filtro=filter,
                                            escludi=escludi, level=level)
            visti = {_chiave(r) for r in primo["lines"]}
            if primo.get("warning"):
                yield json.dumps({"type": "warning", "text": primo["warning"]}) + "\n"
            while asyncio.get_event_loop().time() < scadenza:
                await asyncio.sleep(attesa)
                if await request.is_disconnected():
                    return
                giro = await log_sources.leggi(source=source, lines=lines, filtro=filter,
                                               escludi=escludi, level=level)
                nuove = [r for r in giro["lines"] if _chiave(r) not in visti]
                for r in nuove:
                    visti.add(_chiave(r))
                    yield json.dumps({"type": "line", "line": r}, default=str) + "\n"
                # La memoria delle righe gia' viste non puo' crescere all'infinito
                # su una pagina lasciata aperta tutta la notte.
                if len(visti) > 4 * max(lines, 1):
                    visti = {_chiave(r) for r in giro["lines"]}
            # Scaduto il tetto: si dice, invece di chiudere di colpo lasciando
            # la pagina a credere di stare ancora seguendo.
            yield json.dumps({"type": "end", "reason": "scaduto"}) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from services.errors import exc_text
            log.warning(f"stream log ({source}): {exc_text(e)}")
            yield json.dumps({"type": "end", "error": exc_text(e)}) + "\n"

    return StreamingResponse(
        eventi(), media_type="application/x-ndjson",
        # nginx bufferizza le risposte proxate: senza questo header le righe
        # arriverebbero tutte insieme alla fine, cioe' mai.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


def _chiave(r: dict) -> str:
    """Identita' di una riga ai fini del "gia' vista". Il timestamp da solo non
    basta (piu' righe nello stesso secondo), il testo da solo nemmeno (un
    messaggio ripetuto e' un evento nuovo)."""
    return f"{r.get('ts_ms')}\x00{r.get('src')}\x00{r.get('raw') or r.get('msg')}"
