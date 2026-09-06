"""routers/terminal.py — Terminale SSH: elenco host (REST) e sessione (WebSocket).

Il WebSocket sta sotto `/ws` e non sotto `/api`: nginx fa l'upgrade della
connessione solo in quel location. Il controllo di sessione e' inline, perche'
il middleware non intercetta le connessioni WebSocket.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from config import settings
from middleware.auth import password_set, require_session, same_origin, session_valid
from services.audit import audit
from services.config_store import get_config_store
from services.errors import exc_text
from services.ssh_hosts import live_ssh_config, validate_key_path
from services.terminal import TerminalSession, allowed_hosts, available_keys

log = logging.getLogger("terminal")

# REST: montato su /api/terminal, protetto come le altre rotte sensibili.
router = APIRouter(dependencies=[Depends(require_session)])
# WebSocket: montato su /ws.
ws_router = APIRouter()


@router.get("/hosts")
async def list_hosts():
    """Host su cui si puo' aprire una sessione (dalla config, non dal client)."""
    return {"hosts": allowed_hosts(), "enabled": settings.terminal.enabled,
            "idle_timeout": settings.terminal.idle_timeout,
            # Chiavi disponibili: la UI le propone in elenco invece di far
            # scrivere (o incollare) un percorso a mano.
            "keys": available_keys(), "default_key": live_ssh_config().default_key or ""}


class HostBody(BaseModel):
    host: str
    user: str = ""
    key: str = ""
    port: int = 22


# Nome host o IP: niente CIDR, niente spazi, niente inizio con "-".
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")


def _validated_key(key: str) -> str:
    """Percorso della chiave validato (regola condivisa con le Impostazioni)."""
    try:
        return validate_key_path(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _save_hosts(hosts: list[dict], event: str, request: Request, **fields):
    """Riscrive `discovery.ssh.hosts` conservando il resto della sezione.

    Passa dal config store cosi' valgono le stesse garanzie dell'editor delle
    Impostazioni: segreti mascherati preservati, schema validato, backup del
    file. Il riavvio non serve: la sezione viene riletta a caldo dal file
    (services/ssh_hosts.live_ssh_config).
    """
    section = live_ssh_config().model_dump()
    section["hosts"] = hosts
    try:
        get_config_store().save_section("discovery_ssh", section)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ip = request.client.host if request.client else "unknown"
    audit(event, ip=ip, **fields)
    return {"hosts": allowed_hosts()}


@router.post("/hosts")
async def add_host(body: HostBody, request: Request):
    """Aggiunge (o aggiorna) un host raggiungibile dal terminale."""
    host = body.host.strip()
    if not _HOST_RE.match(host):
        raise HTTPException(status_code=400, detail="nome host o IP non valido")
    if not 1 <= body.port <= 65535:
        raise HTTPException(status_code=400, detail="porta fuori intervallo")

    entry = {"ip": host, "port": body.port}
    if body.user.strip():
        entry["user"] = body.user.strip()
    key = body.key.strip()
    if key:
        entry["key"] = _validated_key(key)

    hosts = [h.model_dump(exclude_none=True) for h in live_ssh_config().hosts
             if h.ip != host]
    hosts.append(entry)
    return _save_hosts(hosts, "terminal.host_aggiunto", request, host=host,
                       utente=entry.get("user", "(default)"))


@router.delete("/hosts/{host}")
async def delete_host(host: str, request: Request):
    """Toglie un host dall'elenco (solo quelli gestiti qui, non router/systemd)."""
    current = live_ssh_config().hosts
    if not any(h.ip == host for h in current):
        raise HTTPException(status_code=404,
                            detail="host non gestito da qui: router e host di "
                                   "LANMng si cambiano da Impostazioni")
    hosts = [h.model_dump(exclude_none=True) for h in current if h.ip != host]
    return _save_hosts(hosts, "terminal.host_rimosso", request, host=host)


@ws_router.websocket("/terminal")
async def terminal_ws(ws: WebSocket):
    ip = ws.client.host if ws.client else "unknown"
    # Una pagina di un altro host della LAN puo' aprire un WebSocket verso di
    # noi e il browser allegherebbe i cookie: l'unica barriera sarebbe
    # SameSite=Lax. Il controllo esplicito di origine non dipende da quello.
    if not same_origin(ws):
        audit("terminal.negata", ip=ip, motivo="origine non consentita")
        await ws.close(code=1008)
        return
    # Sessione sempre obbligatoria: niente bypass LAN, niente auth disattivata.
    if not password_set() or not session_valid(ws.cookies):
        audit("terminal.negata", ip=ip, motivo="sessione assente o non valida")
        await ws.close(code=1008)
        return
    await ws.accept()

    session: TerminalSession | None = None

    async def send(msg: dict):
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass          # connessione gia' caduta: ci pensa il finally a pulire

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "open":
                if session:
                    await send({"type": "error", "detail": "sessione gia' aperta"})
                    continue
                host = str(msg.get("host", ""))
                session = TerminalSession(host, ip, send)
                try:
                    await session.open(msg.get("cols", 100), msg.get("rows", 30))
                except Exception as e:
                    audit("terminal.fallita", ip=ip, host=host, errore=str(e)[:200])
                    await send({"type": "error", "detail": str(e)[:200]})
                    session = None
                    continue
                await send({"type": "ready", "host": host, "user": session.user})

            elif kind == "data" and session:
                await session.write(str(msg.get("data", "")))

            elif kind == "resize" and session:
                session.resize(msg.get("cols", 100), msg.get("rows", 30))

            elif kind == "close":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.info(f"WS terminale ({ip}): {exc_text(e)}")
    finally:
        if session:
            await session.close("websocket chiuso")
        try:
            await ws.close()
        except Exception:
            pass
