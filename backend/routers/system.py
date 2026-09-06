"""routers/system.py — Info di sistema del router."""
from __future__ import annotations

from fastapi import APIRouter

from services.collector import _fmt_uptime
from services.openwrt import get_luci

router = APIRouter()


@router.get("/info")
async def system_info():
    """Info generali del router: hostname, uptime, load, memoria."""
    info = await get_luci().get_system_info()
    return {
        "hostname": info.hostname,
        "model": info.model,
        "os_version": info.os_version,
        "uptime_seconds": info.uptime_seconds,
        "uptime_human": _fmt_uptime(info.uptime_seconds),
        "load": info.load,
        "memory_total_mb": round(info.memory_total / 1_048_576, 1),
        "memory_free_mb": round(info.memory_free / 1_048_576, 1),
        "memory_used_pct": round((1 - info.memory_free / max(info.memory_total, 1)) * 100, 1),
    }


@router.get("/interfaces")
async def interfaces():
    """Tutte le interfacce di rete del router."""
    ifaces = await get_luci().get_interfaces()
    return [
        {
            "name": i.name, "ifname": i.ifname, "up": i.up, "ip4": i.ip4, "ip6": i.ip6,
            "rx_bytes": i.rx_bytes, "tx_bytes": i.tx_bytes,
            "rx_mb": round(i.rx_bytes / 1_048_576, 2), "tx_mb": round(i.tx_bytes / 1_048_576, 2),
            "mac": i.mac,
        }
        for i in ifaces
    ]


