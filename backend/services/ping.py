"""
services/ping.py — Ping ICMP locale (host singolo e sweep di subnet)
====================================================================
Il ping parte dal backend (rete host), non dal router: lo stato online
resta affidabile anche se il router e' irraggiungibile.

Lo sweep e' la sorgente di discovery piu' affidabile su questa rete:
nmap, con piu' interfacce sulla stessa subnet, puo' non vedere host che
invece rispondono al ping. Vedi services/discovery.py (PingSweepProvider).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time

log = logging.getLogger("ping")

# Oltre questa dimensione una subnet non viene spazzolata: un /16 significherebbe
# 65k ping per ciclo. Le reti grandi vanno ristrette in config (subnets).
MAX_SWEEP_HOSTS = 1024


async def ping_host(ip: str, count: int = 1, timeout: int = 1) -> tuple[bool, float]:
    """Ritorna (online, latenza_ms). Nessuna eccezione propagata al chiamante."""
    args = ["ping", "-c", str(count), "-W", str(timeout), ip]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception as e:
        log.debug(f"ping {ip}: {e}")
        return False, 0.0
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=count * timeout + 2)
    except asyncio.TimeoutError:
        # Senza kill il processo resta vivo e non viene raccolto: uno sweep e'
        # fatto di centinaia di ping e il container ha un tetto di pid.
        await terminate_process(proc)
        log.debug(f"ping {ip}: timeout")
        return False, 0.0
    except Exception as e:
        log.debug(f"ping {ip}: {e}")
        return False, 0.0
    if proc.returncode != 0:
        return False, 0.0
    m = re.search(r"time=([\d.]+)", out.decode(errors="replace"))
    return True, float(m.group(1)) if m else 0.0


async def terminate_process(proc) -> None:
    """Uccide il processo e ne raccoglie l'uscita (niente figli abbandonati)."""
    try:
        proc.kill()
    except ProcessLookupError:
        return                      # gia' uscito da solo
    except OSError as e:
        log.debug(f"kill del processo fallito: {e}")
        return
    try:
        await proc.communicate()
    except Exception as e:
        log.debug(f"raccolta del processo fallita: {e}")


def expand_subnets(cidrs: list[str]) -> list[str]:
    """Host IP delle subnet indicate, escluse quelle troppo grandi."""
    ips: list[str] = []
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            log.warning(f"subnet non valida '{cidr}': {e}")
            continue
        hosts = list(net.hosts())
        if len(hosts) > MAX_SWEEP_HOSTS:
            log.warning(f"subnet {cidr} troppo grande ({len(hosts)} host): sweep saltato")
            continue
        ips += [str(h) for h in hosts]
    return ips


async def sweep(cidrs: list[str], concurrency: int = 128,
                timeout: int = 1) -> dict[str, float]:
    """Ping in parallelo di tutte le subnet indicate -> {ip: latenza_ms}."""
    ips = expand_subnets(cidrs)
    if not ips:
        return {}
    sem = asyncio.Semaphore(max(concurrency, 1))
    alive: dict[str, float] = {}
    started = time.monotonic()

    async def probe(ip: str):
        async with sem:
            online, latency = await ping_host(ip, count=1, timeout=timeout)
        if online:
            alive[ip] = latency

    await asyncio.gather(*[probe(ip) for ip in ips], return_exceptions=True)
    log.info(f"Ping sweep: {len(alive)}/{len(ips)} host attivi "
             f"in {time.monotonic() - started:.1f}s")
    return alive
