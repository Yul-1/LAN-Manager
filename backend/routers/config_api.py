"""routers/config_api.py — Lettura/scrittura della configurazione dalla UI."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import require_session
from services.audit import audit
from services.config_store import get_config_store
from services.restart import programma_uscita
from services.restart import stato as stato_riavvio
from services.secrets_store import get_secrets_store

router = APIRouter()


class ConfigBody(BaseModel):
    yaml: str


class SectionBody(BaseModel):
    value: Any


class SecretsBody(BaseModel):
    # {id_campo: valore}. Vuoto = invariato, "__CLEAR__" = rimuovi.
    values: dict[str, str]


@router.get("/")
async def read_config():
    """Configurazione corrente in YAML, con i segreti mascherati."""
    return {
        "yaml": get_config_store().read_redacted_yaml(),
        "path": str(get_config_store().path),
    }


@router.put("/")
async def write_config(body: ConfigBody):
    """Valida e salva la configurazione (backup automatico). Richiede riavvio."""
    try:
        return await asyncio.to_thread(get_config_store().save_yaml, body.yaml)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/section/{name}")
async def read_section(name: str):
    """Valore (redatto) di una sezione editabile di config.yaml
    (subnets, wg_peers, discovery_hosts, docker_hosts)."""
    try:
        return {"section": name, "value": get_config_store().read_section(name)}
    except KeyError:
        raise HTTPException(status_code=404, detail="sezione sconosciuta")


@router.put("/section/{name}")
async def write_section(name: str, body: SectionBody):
    """Salva una sezione (validata, con backup). Richiede riavvio per applicare."""
    try:
        return await asyncio.to_thread(get_config_store().save_section, name, body.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="sezione sconosciuta")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/secrets")
async def secrets_status():
    """Stato dei segreti (impostato/non impostato). Non ritorna mai i valori."""
    return {"secrets": get_secrets_store().status()}


@router.put("/secrets")
async def update_secrets(body: SecretsBody):
    """Imposta/aggiorna i segreti nel file secrets.env (permessi 600)."""
    return await asyncio.to_thread(get_secrets_store().update, body.values)


@router.get("/backups")
async def list_backups():
    """Elenco dei backup di configurazione."""
    return {"backups": get_config_store().list_backups()}


@router.post("/backups/{name}/restore")
async def restore_backup(name: str):
    """Ripristina un backup (crea comunque un backup dello stato attuale)."""
    try:
        return await asyncio.to_thread(get_config_store().restore_backup, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Riavvio del servizio ───────────────────────────────────────────
#  Le modifiche alla configurazione si applicano solo all'avvio, e finora
#  applicarle voleva dire aprire una sessione SSH sul server. Il processo esce
#  e a farlo ripartire e' Docker: vedi services/restart.py per il perche' e per
#  i casi in cui il pulsante non si deve mostrare.


@router.get("/restart")
async def restart_status():
    """Si puo' riavviare da qui? Con il motivo quando la risposta e' no.

    Senza sessione (lo dice il middleware) non ci si arriva; qui pero' non si
    usa `require_session`, che senza password admin impostata risponderebbe 503
    proprio nella pagina dove la password si imposta.
    """
    return await stato_riavvio()


@router.post("/restart")
async def restart_service(request: Request, _: None = Depends(require_session)):
    """Fa uscire il processo: Docker lo riaccende.

    Superficie sensibile — chiude tutte le sessioni aperte, terminale SSH
    compreso — quindi sessione sempre richiesta (nessuna deroga da auth.method
    o bypass_lan) e riga nel registro di audit.
    """
    info = await stato_riavvio()
    if not info["available"]:
        raise HTTPException(status_code=409, detail=info["reason"])
    ip = request.client.host if request.client else "unknown"
    audit("servizio.riavvio", ip=ip, container=info["container"] or "?",
          policy=info["policy"] or "sconosciuta")
    programma_uscita()
    return {"ok": True, "policy": info["policy"],
            "detail": "riavvio in corso: la dashboard si ricollega da sola"}
