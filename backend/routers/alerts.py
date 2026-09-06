"""routers/alerts.py — Catalogo delle regole e silenziamento degli alert.

Gli alert stessi viaggiano nello snapshot (`alerts`, `alerts_summary`): qui c'e'
solo cio' che serve a governarli dalla UI.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.alerts import CATALOGO, catalogo, live_alerts_config
from services.config_store import get_config_store

router = APIRouter()


class SilenceBody(BaseModel):
    rule: str
    subject: str = ""
    reason: str = ""


@router.get("/rules")
async def alert_rules():
    """Regole disponibili e silenziamenti attivi (tendina + pagina Impostazioni)."""
    cfg = live_alerts_config()
    return {
        "rules": catalogo(),
        "silenced": [s.model_dump() for s in cfg.silenced],
        "enabled": cfg.enabled,
    }


@router.post("/silence")
async def silence_alert(body: SilenceBody):
    """Zittisce per sempre una regola (o un suo soggetto), con il motivo scritto.

    Endpoint dedicato invece del leggi-modifica-riscrivi lato client: quello
    perderebbe le modifiche fatte altrove fra la lettura e il salvataggio, e non
    potrebbe pretendere il motivo. Non richiede riavvio: la sezione viene
    riletta dal file entro il ciclo successivo.
    """
    rule = (body.rule or "").strip()
    if rule not in CATALOGO:
        raise HTTPException(status_code=400, detail=f"regola sconosciuta: {rule}")
    motivo = (body.reason or "").strip()
    if not motivo:
        raise HTTPException(
            status_code=400,
            detail="scrivi il motivo: serve a ricordare fra sei mesi perche' "
                   "questo avviso non suona piu'")

    subject = (body.subject or "").strip()
    store = get_config_store()
    attuali = [dict(s) for s in (store.read_section("alerts_silenced") or [])]
    if any(s.get("rule") == rule and (s.get("subject") or "") == subject for s in attuali):
        return {"ok": True, "silenced": len(attuali), "restart_required": False}
    attuali.append({"rule": rule, "subject": subject, "reason": motivo,
                    "since": int(time.time())})
    try:
        esito = await asyncio.to_thread(store.save_section, "alerts_silenced", attuali)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**esito, "silenced": len(attuali)}
