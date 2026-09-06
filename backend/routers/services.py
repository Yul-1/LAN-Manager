"""routers/services.py — Vista unificata dei servizi + gestione del catalogo."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.collector import get_collector
from services.ratelimit import RateLimiter
from services.service_store import get_service_store
from services.services_overview import build_services_overview
from services.windows_services import valida_nome

router = APIRouter()

# Cooldown dell'aggiornamento manuale: ogni giro apre una connessione SSH per
# host, quindi tenere premuto il pulsante non deve moltiplicarle.
_refresh_rl = RateLimiter(max_hits=1, window_seconds=10)


@router.get("/")
async def services_overview():
    """Stato unificato: container Docker, unit systemd, healthcheck HTTP/TCP/ping."""
    return await build_services_overview()


@router.post("/refresh")
async def refresh_monitor():
    """Aggiorna subito servizi, container e risorse, senza attendere il ciclo.

    Attende la raccolta e risponde a dati aggiornati (i client hanno gia'
    ricevuto lo snapshot nuovo sul WebSocket), cosi' il pulsante torna
    disponibile quando c'e' davvero qualcosa di nuovo da vedere."""
    if not _refresh_rl.allowed("refresh"):
        raise HTTPException(status_code=429, detail="aggiornato da poco, attendi",
                            headers={"Retry-After": str(_refresh_rl.retry_after("refresh"))})
    _refresh_rl.hit("refresh")
    result = await get_collector().refresh_monitor()
    if result.get("status") == "busy":
        raise HTTPException(status_code=409, detail=result.get("reason", "occupato"))
    return result


# ── Gestione del catalogo (services.yaml) dalla UI ─────────────────

class ServiceEntry(BaseModel):
    kind: str                    # docker | systemd | windows_service | http
    # docker (pin di un container)
    name: str = ""
    label: str = ""
    url: str = ""
    # systemd
    unit: str = ""
    critical: bool = False
    # windows_service: name + label + critical + host (obbligatorio)
    # http/tcp/ping
    type: str = "http"           # http | tcp | ping
    host: str = ""
    port: int = 0
    expect_status: list[int] = []
    # Comune ai tre metodi: comparire o no fra i servizi in evidenza in dashboard.
    dashboard: bool = True


def _entry_for_kind(b: ServiceEntry) -> dict:
    """Costruisce l'entry da salvare in base al metodo scelto."""
    if b.kind == "docker":
        e = {"name": b.name, "label": b.label, "url": b.url}
    elif b.kind == "systemd":
        e = {"unit": b.unit, "label": b.label, "critical": b.critical, "host": b.host}
    elif b.kind == "windows_service":
        # L'host non e' facoltativo come per systemd: l'host di default sarebbe
        # la macchina Linux del backend, dove un servizio Windows non esiste.
        if not b.host.strip():
            raise HTTPException(
                status_code=400,
                detail="indica l'host Windows su cui gira il servizio")
        try:
            nome = valida_nome(b.name)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))
        e = {"name": nome, "label": b.label, "critical": b.critical,
             "host": b.host.strip()}
    elif b.kind == "http":
        e = {"name": b.name, "type": b.type}
        if b.type == "http":
            e["url"] = b.url
            if b.expect_status:
                e["expect_status"] = b.expect_status
        elif b.type == "tcp":
            e["host"] = b.host
            e["port"] = b.port
        elif b.type == "ping":
            e["host"] = b.host
        else:
            raise HTTPException(status_code=400, detail=f"type non valido: {b.type}")
    else:
        raise HTTPException(status_code=400, detail=f"kind non valido: {b.kind}")
    # Sempre esplicito, anche quando e' False: la regola di default (pinned e
    # unit critiche in dashboard) serve solo ai cataloghi scritti prima che la
    # scelta esistesse. Se qui lo omettessimo, togliere la spunta a un
    # container pinnato non avrebbe alcun effetto.
    e["dashboard"] = bool(b.dashboard)
    # rimuove i campi vuoti (ma tiene i bool e gli 0 significativi solo se valorizzati)
    return {k: v for k, v in e.items() if v not in ("", None)}


@router.get("/config")
async def services_config():
    """Catalogo dei servizi monitorati (editabile dalla UI)."""
    return get_service_store().read()


@router.post("/config")
async def add_service(body: ServiceEntry):
    """Aggiunge/aggiorna un servizio monitorato. Applicato al prossimo ciclo."""
    entry = _entry_for_kind(body)
    try:
        await asyncio.to_thread(get_service_store().add, body.kind, entry)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "added", "entry": entry}


@router.delete("/config/{kind}/{ident}")
async def remove_service(kind: str, ident: str):
    """Rimuove un servizio dal catalogo per identificativo (name/unit).

    Dopo la scrittura su disco si **pota subito lo snapshot**: la vista dei
    servizi si ricalcola solo nel giro lento (60s), ma quello veloce la
    ribroadcasta ogni 10s, e senza la potatura il servizio cancellato tornava
    in pagina un attimo dopo essere sparito.
    """
    try:
        n = await asyncio.to_thread(get_service_store().remove, kind, ident)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not n:
        # Prima rispondeva 200 con `count: 0` e il client, che guarda solo lo
        # stato, dichiarava riuscita una cancellazione che non era avvenuta.
        raise HTTPException(status_code=404,
                            detail=f"'{ident}' non e' nel catalogo dei servizi")
    get_collector().dimentica_servizio(kind, ident)
    return {"status": "removed", "count": n}
