"""
services/service_catalog.py — Caricamento di config/services.yaml
=================================================================
Definisce QUALI servizi monitorare (oltre ai container Docker, che sono
auto-scoperti): unit systemd dell'host, servizi Windows su un host della LAN e
healthcheck HTTP/TCP.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from config import settings

log = logging.getLogger("service-catalog")


def load_services_catalog(path: str | None = None) -> dict:
    """
    Ritorna un dict normalizzato:
      {
        "docker_pinned": [ {name, label, url}, ... ],
        "systemd":       [ {unit, label, critical}, ... ],
        "windows":       [ {name, label, host, critical}, ... ],
        "http":          [ {name, type, ...}, ... ],
      }
    """
    p = Path(path or settings.services_catalog)
    if not p.exists():
        example = p.with_name(p.stem + ".example" + p.suffix)
        if example.exists():
            log.info(f"services.yaml assente, uso il template {example.name}")
            p = example
        else:
            log.warning(f"services.yaml non trovato: {p}")
            return {"docker_pinned": [], "systemd": [], "windows": [], "http": []}
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    return {
        "docker_pinned": (data.get("docker", {}) or {}).get("pinned", []) or [],
        "systemd": (data.get("systemd", {}) or {}).get("units", []) or [],
        "windows": (data.get("windows", {}) or {}).get("services", []) or [],
        "http": (data.get("http", {}) or {}).get("checks", []) or [],
    }
