"""routers/docker_svc.py — Container Docker (multi-host)."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.docker_client import get_docker_manager

router = APIRouter()

# ID/nome container Docker: hex dell'ID o nome ([a-zA-Z0-9][a-zA-Z0-9_.-]*).
# Validato prima di finire nell'URL Engine API o in un comando SSH.
_CID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class ActionRequest(BaseModel):
    action: str            # start | stop | restart | pause | unpause
    host: str              # host Docker su cui agire


@router.get("/containers")
async def list_containers():
    """Lista container aggregata su tutti gli host Docker."""
    containers = await get_docker_manager().list_containers()
    return {
        "containers": [c.to_dict() for c in containers],
        "hosts": get_docker_manager().hosts(),
        "running": sum(1 for c in containers if c.is_running),
        "stopped": sum(1 for c in containers if not c.is_running),
        "total": len(containers),
    }


@router.get("/hosts")
async def list_hosts():
    """Host Docker monitorati (locale + remoti SSH + auto-scoperti)."""
    return {"hosts": get_docker_manager().hosts()}


@router.post("/containers/{container_id}/action")
async def container_action(container_id: str, req: ActionRequest):
    """Azione su un container del dato host (start/stop/restart/pause/unpause)."""
    if not _CID_RE.match(container_id):
        raise HTTPException(status_code=400, detail="container_id non valido")
    try:
        ok = await get_docker_manager().container_action(req.host, container_id, req.action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=500, detail=f"azione '{req.action}' non riuscita")
    return {"status": "ok", "host": req.host, "container": container_id, "action": req.action}


@router.get("/networks")
async def list_networks():
    """Reti Docker (aggregate sugli host)."""
    networks = await get_docker_manager().list_networks()
    return {"networks": [n.to_dict() for n in networks]}
