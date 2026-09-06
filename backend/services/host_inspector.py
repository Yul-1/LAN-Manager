"""
services/host_inspector.py — Stato di rete dell'host che ospita LANMng
======================================================================
Interfacce, indirizzi, rotte e contatori della macchina su cui gira il
backend. Funziona perche' il container gira in `network_mode: host`: quello
che si legge in /proc e /sys e' dell'host, non di una rete virtuale.

Sorgente principale `ip -j` (JSON, richiede iproute2 nell'immagine); se manca
si ripiega su `nmap --iflist`, che c'e' sempre, perdendo rotte e maschere.

Solo dati grezzi: le regole che li interpretano (piu' schede sulla stessa
subnet, piu' default route, link giu' con un IP) stanno in services/alerts.py,
dove si possono silenziare e contare.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import shutil
import socket
import time
from pathlib import Path

from config import settings

log = logging.getLogger("host")

_SYS_NET = Path("/sys/class/net")
_PROC_DEV = Path("/proc/net/dev")

# Interfacce virtuali di Docker: si mostrano a parte, non sono "rete di casa".
_VIRTUAL_RE = re.compile(r"^(docker|br-|veth|lo$)")


async def _run(args: list[str], timeout: int = 10) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace")
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        log.warning(f"{args[0]}: {e}")
        return ""


# ── Lettura da /proc e /sys ────────────────────────────────────────

def _counters() -> dict[str, dict]:
    """Contatori per interfaccia da /proc/net/dev."""
    stats: dict[str, dict] = {}
    try:
        lines = _PROC_DEV.read_text().splitlines()[2:]
    except OSError as e:
        log.warning(f"/proc/net/dev non leggibile: {e}")
        return stats
    for line in lines:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) < 16:
            continue
        stats[name.strip()] = {
            "rx_bytes": int(f[0]), "rx_packets": int(f[1]),
            "rx_errors": int(f[2]), "rx_dropped": int(f[3]),
            "tx_bytes": int(f[8]), "tx_packets": int(f[9]),
            "tx_errors": int(f[10]), "tx_dropped": int(f[11]),
        }
    return stats


def _sys_attr(dev: str, attr: str) -> str:
    try:
        return (_SYS_NET / dev / attr).read_text().strip()
    except OSError:
        return ""


def _is_wireless(dev: str) -> bool:
    return (_SYS_NET / dev / "wireless").exists()


def _speed_mbps(dev: str) -> int | None:
    raw = _sys_attr(dev, "speed")
    try:
        speed = int(raw)
    except ValueError:
        return None
    return speed if speed > 0 else None       # -1 = link giu' o non applicabile


# ── Sorgenti interfacce/rotte ──────────────────────────────────────

async def _interfaces_ip() -> list[dict]:
    out = await _run(["ip", "-j", "addr", "show"])
    if not out.strip():
        return []
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        log.warning(f"output di 'ip -j addr' non interpretabile: {e}")
        return []
    ifaces = []
    for entry in raw:
        name = entry.get("ifname", "")
        ifaces.append({
            "name": name,
            "mac": entry.get("address", ""),
            "mtu": entry.get("mtu"),
            "state": (entry.get("operstate") or "").lower(),
            "addresses": [
                {"ip": a.get("local", ""), "prefix": a.get("prefixlen"),
                 "family": a.get("family", "")}
                for a in entry.get("addr_info", []) if a.get("local")
            ],
        })
    return ifaces


async def _interfaces_nmap() -> list[dict]:
    """Ripiego senza iproute2: nmap --iflist da nome, IP/maschera, stato, MAC."""
    out = await _run(["nmap", "--iflist"], timeout=20)
    ifaces: dict[str, dict] = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+\(\S+\)\s+([\d.]+)/(\d+)\s+\S+\s+(up|down)\s+(\d+)\s*(\S+)?", line)
        if not m:
            continue
        name = m.group(1)
        iface = ifaces.setdefault(name, {
            "name": name, "mac": (m.group(6) or "").lower(), "mtu": int(m.group(5)),
            "state": m.group(4), "addresses": [],
        })
        iface["addresses"].append({"ip": m.group(2), "prefix": int(m.group(3)), "family": "inet"})
    return list(ifaces.values())


async def _routes() -> list[dict]:
    out = await _run(["ip", "-j", "route", "show"])
    if not out.strip():
        return []
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        # Senza questa riga una tabella di routing illeggibile si presentava
        # come "nessuna rotta", che e' un'altra cosa.
        log.warning(f"output di 'ip -j route' non interpretabile: {e}: {out[:200]}")
        return []
    return [
        {
            "dest": r.get("dst", ""),
            "gateway": r.get("gateway", ""),
            "dev": r.get("dev", ""),
            "metric": r.get("metric"),
            "default": r.get("dst") == "default",
        }
        for r in raw
    ]


# ── Rete di un indirizzo ───────────────────────────────────────────
# Le regole sulla rete dell'host vivono in services/alerts.py: qui restavano
# invisibili fuori dalla pagina Host e non si potevano ne' silenziare ne' contare.

def _network_of(ip: str, prefix: int | None) -> str:
    try:
        return str(ipaddress.ip_network(f"{ip}/{prefix or 32}", strict=False))
    except ValueError:
        return ""


# ── API pubblica ───────────────────────────────────────────────────

async def get_host_network() -> dict:
    source = "ip"
    ifaces = await _interfaces_ip() if shutil.which("ip") else []
    if not ifaces:
        source = "nmap"
        ifaces = await _interfaces_nmap()
        if ifaces:
            log.warning("iproute2 assente: uso nmap --iflist (niente rotte ne' contatori completi)")

    stats = _counters()
    for iface in ifaces:
        name = iface["name"]
        iface["virtual"] = bool(_VIRTUAL_RE.match(name))
        iface["wireless"] = _is_wireless(name)
        iface["speed_mbps"] = _speed_mbps(name)
        iface["stats"] = stats.get(name, {})
        iface["subnets"] = sorted({
            label for a in iface["addresses"]
            if a.get("family") == "inet" and (label := _subnet_label(a["ip"]))
        })

    routes = await _routes() if source == "ip" else []
    ifaces.sort(key=lambda i: (i["virtual"], i["name"]))
    return {
        "source": source,
        # Di quale macchina si parla: il container gira in host-network, quindi
        # e' il nome dell'host. La pagina lo scrive per non far confondere
        # queste interfacce con quelle del router.
        "hostname": socket.gethostname(),
        "ts": int(time.time()),
        "interfaces": ifaces,
        "routes": routes,
    }


def _subnet_label(ip: str) -> str:
    """Etichetta della subnet configurata che contiene l'IP (se c'e')."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for sn in settings.subnets:
        try:
            if addr in ipaddress.ip_network(sn.cidr, strict=False):
                return sn.label
        except ValueError:
            continue
    return ""
