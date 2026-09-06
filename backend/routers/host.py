"""routers/host.py — Stato di rete dell'host che ospita LANMng e risorse degli host."""
from __future__ import annotations

from fastapi import APIRouter

from services.host_inspector import get_host_network
from services.host_metrics import get_host_metrics

router = APIRouter()


@router.get("/network")
async def host_network():
    """Interfacce, indirizzi, rotte e contatori.

    Sola lettura di /proc, /sys e `ip`: non richiede sessione come i tool, che
    invece agiscono verso l'esterno.
    """
    return await get_host_network()


@router.get("/resources")
async def host_resources():
    """Risorse (CPU, RAM, dischi, temperature) degli host della LAN.

    Ritorna l'ultima raccolta del collector invece di aprire nuove connessioni
    SSH: interrogare qui gli host accorcerebbe la finestra su cui e' calcolata
    la media della CPU, cioe' cambierebbe la misura solo per averla guardata.
    La raccolta parte da qui solo al primo avvio, quando non c'e' ancora nulla.
    """
    metrics = get_host_metrics()
    return metrics.last() or await metrics.collect()
