"""routers/tools.py — Strumenti di rete (ping, traceroute, dig, nmap, porta TCP).

Rotte sensibili: richiedono sempre una sessione valida (vedi require_session) e
ogni esecuzione finisce nel registro di audit.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from middleware.auth import require_session
from services.audit import audit
from services.nettools import (STREAM_TOOLS, TOOLS, ToolError, run_tool,
                               run_tool_stream, timeout_massimo)
from services.ratelimit import RateLimiter

router = APIRouter(dependencies=[Depends(require_session)])

# Un tool alla volta e' piu' che sufficiente per un uso manuale: il limite serve
# a evitare che una pagina lasciata aperta (o un click ripetuto) inondi la rete.
_rl = RateLimiter(max_hits=10, window_seconds=60)


class ToolRequest(BaseModel):
    tool: str
    target: str = ""
    # Opzioni specifiche del tool (count, max_hops, type, port, services…):
    # i valori ammessi li controlla services/nettools.py, non il client.
    options: dict = {}


@router.get("/")
async def list_tools():
    """Tool disponibili e opzioni che accettano (la UI ci costruisce il form).

    Ogni voce porta anche `max_seconds`: quanto puo' durare al massimo. Lo
    calcola `nettools.timeout_massimo` dagli stessi timeout dell'esecuzione,
    cosi' la pagina non promette un'attesa diversa da quella vera.
    """
    elenco = [
            {"id": "ping", "label": "Ping", "help": "raggiungibilita' e latenza",
             "options": {"count": {"type": "int", "default": 4, "min": 1, "max": 10}}},
            {"id": "traceroute", "label": "Traceroute", "help": "percorso verso il bersaglio",
             "options": {"max_hops": {"type": "int", "default": 20, "min": 1, "max": 30}}},
            {"id": "dig", "label": "DNS (dig)", "help": "risoluzione dei nomi",
             "options": {"type": {"type": "choice", "default": "A",
                                  "values": ["A", "AAAA", "PTR", "MX", "TXT", "NS",
                                             "CNAME", "SOA", "SRV"]}}},
            {"id": "nmap", "label": "Scan porte (nmap)", "help": "prime 50 porte piu' comuni",
             "options": {"services": {"type": "bool", "default": False,
                                      "label": "rileva i servizi (piu' lento)"}}},
            {"id": "port", "label": "Porta TCP", "help": "una porta specifica e' aperta?",
             "options": {"port": {"type": "int", "default": 22, "min": 1, "max": 65535}}},
            {"id": "arping", "label": "ARP ping", "help": "vivo sulla LAN anche se ignora il ping",
             "options": {"count": {"type": "int", "default": 3, "min": 1, "max": 10},
                         "iface": {"type": "text", "default": "",
                                   "label": "interfaccia (vuoto = automatica)"}}},
            {"id": "whois", "label": "Whois", "help": "di chi e' un dominio o un IP pubblico",
             "options": {}},
            {"id": "http", "label": "HTTP (curl)", "help": "stato, redirect e intestazioni di un URL",
             "options": {"method": {"type": "choice", "default": "GET", "values": ["GET", "HEAD"]},
                         "follow": {"type": "bool", "default": True, "label": "segui i redirect"},
                         "verify": {"type": "bool", "default": True, "label": "verifica il certificato"},
                         "timeout": {"type": "int", "default": 10, "min": 1, "max": 30}}},
            {"id": "speedtest", "label": "Velocita' della linea",
             "help": f"scarica dei dati e misura: consuma traffico "
                     f"({settings.tools.speedtest_mb} MB di serie)",
             "no_target": True,
             "options": {"mb": {"type": "int", "default": settings.tools.speedtest_mb,
                                "min": 1, "max": settings.tools.speedtest_max_mb,
                                "label": "MB da scaricare"}}},
        ]
    for t in elenco:
        t["max_seconds"] = timeout_massimo(t["id"])
        # `stream`: l'output esce mentre il comando gira. La pagina lo legge da
        # qui invece di tenersi un secondo elenco che prima o poi diverge.
        t["stream"] = t["id"] in STREAM_TOOLS
    return {"tools": elenco, "ids": list(TOOLS)}


@router.post("/run")
async def run(body: ToolRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _rl.allowed(ip):
        raise HTTPException(status_code=429, detail="troppe esecuzioni, attendi un minuto",
                            headers={"Retry-After": str(_rl.retry_after(ip))})
    _rl.hit(ip)
    try:
        result = await run_tool(body.tool, body.target, body.options)
    except ToolError as e:
        # L'input rifiutato non ha toccato la rete: lo slot torna disponibile.
        _rl.refund(ip)
        audit("tool.rifiutato", ip=ip, tool=body.tool, target=body.target, motivo=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    audit("tool.eseguito", ip=ip, tool=result["tool"], target=result["target"],
          comando=result["command"], exit=result["exit_code"], ms=result["duration_ms"])
    return result


@router.post("/stream")
async def run_stream(body: ToolRequest, request: Request):
    """Come /run, ma l'output arriva mentre il comando gira.

    Il corpo e' NDJSON: una riga JSON per evento (`start`, `out`, `end`).
    Serve per i comandi lunghi — un traceroute o un nmap da tre minuti
    lasciavano la pagina vuota fino all'ultimo secondo.

    Nota di deploy: la risposta porta `X-Accel-Buffering: no`, che dice a nginx
    di non accumularla in un buffer. Senza, il proxy consegnerebbe tutto insieme
    alla fine e lo streaming sarebbe solo apparente.
    """
    ip = request.client.host if request.client else "unknown"
    if not _rl.allowed(ip):
        raise HTTPException(status_code=429, detail="troppe esecuzioni, attendi un minuto",
                            headers={"Retry-After": str(_rl.retry_after(ip))})
    _rl.hit(ip)

    gen = run_tool_stream(body.tool, body.target, body.options)
    # Il primo evento si tira fuori qui: la validazione sta tutta prima di esso,
    # quindi un input rifiutato diventa un 400 vero e non un errore infilato
    # dentro un corpo gia' cominciato (che il client non saprebbe classificare).
    try:
        primo = await gen.__anext__()
    except ToolError as e:
        _rl.refund(ip)
        audit("tool.rifiutato", ip=ip, tool=body.tool, target=body.target, motivo=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except StopAsyncIteration:
        await gen.aclose()
        raise HTTPException(status_code=500, detail="il tool non ha prodotto nulla")

    async def corpo():
        fine: dict = {}
        try:
            yield json.dumps(primo) + "\n"
            async for ev in gen:
                if ev.get("type") == "end":
                    fine = ev
                yield json.dumps(ev) + "\n"
        finally:
            # L'audit va scritto per primo: se il browser stacca a meta', la
            # pulizia qui sotto puo' essere interrotta dalla cancellazione del
            # task, ma il comando e' stato eseguito e deve restare nel registro.
            audit("tool.eseguito", ip=ip, tool=primo["tool"], target=primo["target"],
                  comando=primo["command"], exit=fine.get("exit_code"),
                  ms=fine.get("duration_ms"), interrotto=not fine)
            # `aclose()` fa scattare il kill del processo nel `finally` del
            # generatore: e' cio' che impedisce a un nmap abbandonato di
            # continuare a girare.
            await gen.aclose()

    return StreamingResponse(corpo(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-store"})
