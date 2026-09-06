"""
services/service_store.py — Gestione persistente di services.yaml
=================================================================
Permette dalla UI di aggiungere/rimuovere i servizi da monitorare,
scegliendo il metodo per ciascuno:
  - docker  : container "pinnato" (etichetta/URL su un container scoperto)
  - systemd : unit dell'host (stato via SSH)
  - windows : servizio Windows su un host della LAN (stato via SSH/PowerShell)
  - http    : healthcheck con type http | tcp | ping

Struttura del file:
    docker:  { pinned:   [ {name, label, url}, ... ] }
    systemd: { units:    [ {unit, label, critical}, ... ] }
    windows: { services: [ {name, label, host, critical}, ... ] }
    http:    { checks:   [ {name, type, ...}, ... ] }

Scrittura atomica con backup .bak. Se il file reale non esiste, parte
dal template .example (se presente).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import yaml

from config import settings

log = logging.getLogger("service-store")

# kind -> (chiave di sezione, lista interna, campo identificativo)
# Il token del kind e' gia' scollegato dal nome della sezione YAML: qui
# "windows_service" e' l'unico nome che gira nel codice, nel router, nello
# storico e nel frontend, mentre nel file la sezione resta leggibile.
_SECTIONS = {
    "docker":          ("docker", "pinned", "name"),
    "systemd":         ("systemd", "units", "unit"),
    "windows_service": ("windows", "services", "name"),
    "http":            ("http", "checks", "name"),
}


class ServiceStore:

    def __init__(self):
        self.path = Path(settings.services_catalog)
        self._data: dict = {"docker": {"pinned": []}, "systemd": {"units": []},
                            "windows": {"services": []}, "http": {"checks": []}}
        self.reload()

    def reload(self):
        src = self.path
        if not src.exists():
            example = src.with_name(src.stem + ".example" + src.suffix)
            src = example if example.exists() else None
        data = {}
        if src and src.exists():
            with open(src) as f:
                data = yaml.safe_load(f) or {}
        self._data = {
            "docker":  {"pinned": (data.get("docker") or {}).get("pinned", []) or []},
            "systemd": {"units":  (data.get("systemd") or {}).get("units", []) or []},
            "windows": {"services": (data.get("windows") or {}).get("services", []) or []},
            "http":    {"checks": (data.get("http") or {}).get("checks", []) or []},
        }

    def read(self) -> dict:
        """Catalogo editabile per la UI."""
        return {
            "docker": self._data["docker"]["pinned"],
            "systemd": self._data["systemd"]["units"],
            "windows": self._data["windows"]["services"],
            "http": self._data["http"]["checks"],
        }

    def _list(self, kind: str):
        if kind not in _SECTIONS:
            raise ValueError(f"kind sconosciuto: {kind}")
        section, inner, idfield = _SECTIONS[kind]
        return self._data[section][inner], idfield

    def add(self, kind: str, entry: dict) -> dict:
        """Aggiunge (o sostituisce per identificativo) un servizio."""
        lst, idfield = self._list(kind)
        ident = str(entry.get(idfield) or "").strip()
        if not ident:
            raise ValueError(f"campo '{idfield}' obbligatorio")
        existing = next((e for e in lst if str(e.get(idfield) or "") == ident), None)
        if existing:
            existing.clear()
            existing.update(entry)
        else:
            lst.append(entry)
        self._save()
        return entry

    def remove(self, kind: str, ident: str) -> int:
        """Rimuove i servizi con quell'identificativo. Ritorna quanti rimossi."""
        lst, idfield = self._list(kind)
        before = len(lst)
        lst[:] = [e for e in lst if str(e.get(idfield) or "") != ident]
        removed = before - len(lst)
        if removed:
            self._save()
        return removed

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        header = ("# services.yaml — servizi monitorati da LANMng\n"
                  "# Gestito anche dalla UI (Servizi). Backup automatico in services.yaml.bak\n\n")
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(header)
                yaml.safe_dump(self._data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, self.path)
            log.info("services.yaml salvato")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


_store: ServiceStore | None = None


def get_service_store() -> ServiceStore:
    global _store
    if _store is None:
        _store = ServiceStore()
    return _store
