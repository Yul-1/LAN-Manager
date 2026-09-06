"""routers/setup.py — Primo avvio guidato.

Queste rotte esistono solo finche' `config.yaml` non c'e'. Appena il file
esiste si spengono da sole (`_solo_al_primo_avvio`): lasciarle raggiungibili
sarebbe un modo di riscrivere la configurazione del servizio **senza
autenticarsi**, cioe' esattamente cio' che il resto delle API impedisce.

Il secondo vincolo e' l'origine: come per l'impostazione della password admin
al primo accesso (`routers/auth.py`), si accetta solo da un indirizzo privato.
Chi installa l'app la raggiunge dalla propria rete; una richiesta che arriva da
altrove, in questa finestra, non e' l'utente.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import is_lan
from services import bootstrap
from services.audit import audit
from services.i18n import t

log = logging.getLogger("setup")
router = APIRouter()


class SubnetBody(BaseModel):
    cidr: str
    label: str = "LAN"
    color: str = ""
    scan: bool = True


class RouterBody(BaseModel):
    host: str = ""
    user: str = ""
    ssh_key: str = ""
    port: int = 0


class SetupBody(BaseModel):
    subnets: list[SubnetBody]
    router: RouterBody | None = None


def _solo_al_primo_avvio(request: Request) -> None:
    """Le rotte di setup vivono solo nella finestra del primo avvio."""
    if not bootstrap.serve_setup():
        # 404 e non 403: a configurazione fatta questa superficie non esiste
        # piu', e dire "vietato" confermerebbe che esiste.
        raise HTTPException(status_code=404, detail=t("err.nonTrovato"))
    if not (request.client and is_lan(request.client.host)):
        raise HTTPException(
            status_code=403,
            detail=t("err.setupSoloLan"))


@router.get("/status")
async def stato(request: Request):
    """Se il primo avvio serve. E' l'unica rotta che risponde sempre: la SPA la
    interroga per sapere se mostrare la procedura guidata o la dashboard."""
    return {"setup_required": bootstrap.serve_setup()}


@router.get("/suggest")
async def proposta(request: Request):
    """Subnet ricavate dalle interfacce dell'host, come proposta.

    Lista vuota = non si sono lette le interfacce. La pagina lo dice e lascia
    inserire le subnet a mano: meglio nessuna proposta che una inventata.
    """
    _solo_al_primo_avvio(request)
    subnets = await bootstrap.subnet_candidate()
    return {"subnets": subnets}


@router.post("")
async def completa(body: SetupBody, request: Request):
    """Scrive il config.yaml iniziale."""
    _solo_al_primo_avvio(request)
    try:
        salvato = bootstrap.scrivi_config(
            [s.model_dump() for s in body.subnets],
            body.router.model_dump() if body.router else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit("setup.completato", ip=(request.client.host if request.client else "?"),
          subnet=len(salvato["subnets"]), router=("router" in salvato))
    return {"ok": True, "restart_required": True, "subnets": len(salvato["subnets"])}
