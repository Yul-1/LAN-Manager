"""
services/services_overview.py — Vista unificata dei servizi
===========================================================
Combina le quattro fonti in un'unica struttura:
  - Docker     (container auto-scoperti + etichette/URL dal catalogo "pinned")
  - systemd    (unit dell'host elencate in services.yaml)
  - windows    (servizi Windows su host della LAN, elencati in services.yaml)
  - healthcheck(HTTP/TCP definiti in services.yaml)
"""
from __future__ import annotations

import logging

from services.docker_client import get_docker_manager
from services.healthcheck import get_health_checker
from services.service_catalog import load_services_catalog
from services.systemd_monitor import get_systemd_monitor
from services.windows_services import get_windows_services

log = logging.getLogger("services")


async def build_services_overview(containers: list | None = None) -> dict:
    """Vista unificata. `containers` evita di rienumerare Docker quando il
    chiamante (il collector) l'ha gia' fatto: interrogare due volte gli host
    remoti significa il doppio delle connessioni SSH ad ogni ciclo."""
    catalog = load_services_catalog()

    docker = await _docker_section(catalog["docker_pinned"], containers)
    systemd = [u.to_dict() for u in await get_systemd_monitor().status(catalog["systemd"])]
    windows = [w.to_dict() for w in await get_windows_services().status(catalog["windows"])]
    health = [c.to_dict() for c in await get_health_checker().run_all(catalog["http"])]

    # Conteggi complessivi: un servizio e' "ok" se up/active/running.
    docker_ok = docker["summary"]["running"]
    systemd_ok = sum(1 for u in systemd if u["ok"])
    windows_ok = sum(1 for w in windows if w["ok"])
    health_ok = sum(1 for c in health if c["ok"])
    total = docker["summary"]["total"] + len(systemd) + len(windows) + len(health)
    ok = docker_ok + systemd_ok + windows_ok + health_ok

    return {
        "docker": docker,
        "systemd": systemd,
        "windows_services": windows,
        "healthchecks": health,
        # Niente elenco di alert qui: era una lista di soli nomi, senza motivo
        # ne' da quando. Le regole stanno in services/alerts.py.
        "summary": {"total": total, "ok": ok, "down": total - ok},
    }


async def _docker_section(pinned: list[dict], containers: list | None = None) -> dict:
    pin_by_name = {p["name"]: p for p in pinned}
    mgr = get_docker_manager()
    if containers is None:
        containers = await mgr.list_containers()    # multi-host
    items = []
    for c in containers:
        d = c.to_dict()
        name = d["name"]
        pin = pin_by_name.get(name)
        d["label"] = pin.get("label", name) if pin else name
        d["url"] = pin.get("url", "") if pin else ""
        d["pinned"] = pin is not None
        # Assente su un container pinnato = in dashboard: e' quello che la
        # dashboard mostrava prima, quindi un catalogo gia' scritto non cambia.
        # Un container non pinnato non e' mai stato scelto da nessuno.
        d["dashboard"] = bool(pin.get("dashboard", True)) if pin else False
        d["kind"] = "docker"
        items.append(d)
    # Pinned prima, poi per host, poi running, poi nome.
    items.sort(key=lambda x: (not x["pinned"], x["host"], not x["running"], x["name"]))
    # Conteggio per host.
    per_host: dict[str, dict] = {}
    for c in containers:
        ph = per_host.setdefault(c.host, {"running": 0, "total": 0})
        ph["total"] += 1
        if c.is_running:
            ph["running"] += 1
    return {
        "containers": items,
        "hosts": mgr.hosts(),
        "per_host": per_host,
        "summary": {
            "running": sum(1 for c in containers if c.is_running),
            "stopped": sum(1 for c in containers if not c.is_running),
            "total": len(containers),
        },
    }
