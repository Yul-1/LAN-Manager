"""routers/history.py — Storico delle serie temporali.

Le pagine leggono la finestra "viva" dallo snapshot WebSocket; qui si prende
tutto quello che sta piu' indietro, dal database che sopravvive ai riavvii.

I punti escono a passo fisso su tutta la finestra chiesta, con i campi a `null`
dove non c'e' dato: il grafico del frontend dispone i punti per indice e non per
timestamp, quindi restituire solo quelli esistenti comprimerebbe un'ora di
backend spento nella larghezza di un pixel.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query

from services.history_store import get_history

router = APIRouter()

# Periodi ammessi, in millisecondi. Elenco chiuso di proposito: un numero
# libero dal client diventerebbe una scansione arbitraria del database.
PERIODI = {"1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000}


def _finestra(period: str) -> int:
    """Istante di inizio della finestra chiesta."""
    if period not in PERIODI:
        raise HTTPException(
            status_code=400,
            detail=f"periodo non valido: usa uno fra {', '.join(PERIODI)}")
    return int(time.time() * 1000) - PERIODI[period]


@router.get("/traffic")
async def traffic_history(period: str = Query("24h")):
    """Traffico WAN storico: stessa forma dei punti vivi, cosi' il frontend
    passa `points` a drawLineChart senza convertire nulla."""
    da = _finestra(period)
    punti = await asyncio.to_thread(get_history().traffic, da)
    return {"period": period, "from": da, "points": punti}


@router.get("/resources")
async def resources_history(host: str = Query(..., min_length=1, max_length=64),
                            period: str = Query("24h")):
    """Risorse storiche di un host, nella stessa forma di `HostResources.series`."""
    da = _finestra(period)
    punti = await asyncio.to_thread(get_history().host_series, host, da)
    return {"period": period, "from": da, "host": host, "points": punti}


@router.get("/device")
async def device_history(key: str = Query(..., min_length=1, max_length=64),
                         period: str = Query("24h")):
    """Presenza di un dispositivo: le transizioni acceso/spento nella finestra."""
    da = _finestra(period)
    eventi = await asyncio.to_thread(get_history().device_events, key, da)
    return {"period": period, "from": da, "key": key,
            "events": [{"t": e["t"], "online": bool(e["online"])} for e in eventi]}


@router.get("/service")
async def service_history(kind: str = Query(..., max_length=32),
                          name: str = Query(..., min_length=1, max_length=128),
                          host: str = Query("", max_length=64),
                          period: str = Query("24h")):
    """Su/giu' di un servizio. L'identita' e' (kind, host, name): lo stesso
    nome su macchine diverse e' un servizio diverso."""
    da = _finestra(period)
    eventi = await asyncio.to_thread(get_history().service_events, kind, host, name, da)
    return {"period": period, "from": da, "kind": kind, "host": host, "name": name,
            "events": [{"t": e["t"], "ok": bool(e["ok"]), "detail": e["detail"]}
                       for e in eventi]}


@router.get("/states")
async def stati_correnti():
    """Ultima transizione nota di ogni dispositivo e di ogni servizio.

    Una sola richiesta per tutta la pagina: chiedere il "da quando" riga per
    riga significherebbe decine di richieste ad ogni render. I timestamp
    possono precedere qualunque finestra — e' proprio il caso interessante
    ("giu' da tre giorni").
    """
    storico = get_history()
    device, servizi = await asyncio.gather(
        asyncio.to_thread(storico.device_states),
        asyncio.to_thread(storico.service_states),
    )
    return {
        "devices": [{"key": d["dev_key"], "online": bool(d["online"]), "since": d["t"]}
                    for d in device],
        "services": [{"kind": x["kind"], "host": x["host"], "name": x["name"],
                      "ok": bool(x["ok"]), "since": x["t"]} for x in servizi],
    }
