"""routers/wireguard.py — Stato VPN WireGuard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from middleware.auth import require_session
from services.audit import audit
from services.wireguard import get_wireguard_service

router = APIRouter()


@router.get("/status")
async def wg_status():
    """Stato corrente: interfacce + peers."""
    ifaces = await get_wireguard_service().get_status()
    return {
        "interfaces": [i.to_dict() for i in ifaces],
        "total_peers": sum(i.total_peers for i in ifaces),
        "active_peers": sum(i.active_peers for i in ifaces),
    }


@router.get("/config")
async def wg_config():
    """Config file WireGuard (chiavi private oscurate)."""
    return {"config": await get_wireguard_service().get_config_file()}


@router.post("/reload")
async def wg_reload(request: Request, _: None = Depends(require_session)):
    """Ricarica la configurazione WireGuard sul router.

    Sessione sempre richiesta: la ricarica interrompe i tunnel attivi, e con
    `bypass_lan` sarebbe bastato essere in LAN (o un client VPN) per farlo.
    """
    audit("wireguard.ricarica", ip=(request.client.host if request.client else "unknown"))
    if not await get_wireguard_service().reload():
        raise HTTPException(status_code=500, detail="ricarica di WireGuard non riuscita")
    return {"status": "reloaded"}
