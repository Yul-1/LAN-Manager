"""routers/wan.py — Connettivita' WAN (4G/tethering/cablata)."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from config import settings
from services.openwrt import get_luci, get_ssh

router = APIRouter()

# Un host di ping valido e' un IP o un hostname: niente spazi/metacaratteri shell.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,255}$")


def _wan_interfaces(ifaces):
    """Interfacce di uplink da mostrare: l'uplink configurato (quello reale)
    piu' eventuali altri candidati effettivamente su; senza config, i candidati."""
    configured = settings.router.wan_interface
    candidates = settings.router.wan_candidates
    if configured:
        return [i for i in ifaces
                if i.name == configured or (i.name in candidates and i.up)]
    return [i for i in ifaces if i.name in candidates]


@router.get("/status")
async def wan_status():
    """Interfacce WAN attive, IP, traffico."""
    ifaces = await get_luci().get_interfaces()
    wan = _wan_interfaces(ifaces)
    return {
        "wan_interfaces": [
            {
                "name": i.name, "ifname": i.ifname, "up": i.up, "ip4": i.ip4,
                "rx_mb": round(i.rx_bytes / 1_048_576, 2), "tx_mb": round(i.tx_bytes / 1_048_576, 2),
            }
            for i in wan
        ],
        "connected": any(i.up for i in wan),
    }


@router.get("/ping")
async def ping_test(host: str = "", count: int = 4):
    """Ping verso un host dal router."""
    host = host or settings.router.internet_probe
    if not _HOST_RE.match(host):
        raise HTTPException(status_code=400, detail="host non valido")
    count = max(1, min(int(count), 10))
    online, latency = await get_ssh().ping_host(host, count=count)
    # `latency_ms: 0.0` su un host che non risponde era indistinguibile da un
    # ping riuscito in un tempo brevissimo: se non c'e' misura si dice null.
    return {"host": host, "online": online,
            "latency_ms": latency if online and latency else None}


@router.get("/firewall")
async def firewall_rules():
    """Regole nftables correnti del router."""
    return {"rules": await get_ssh().get_nftables_rules()}
