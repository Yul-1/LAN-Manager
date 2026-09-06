"""
services/systemd_monitor.py — Stato servizi systemd dell'host
=============================================================
Legge lo stato delle unit systemd (active/inactive/failed) con
`systemctl show`. Legge lo stato sull'host che ospita il backend.

Nota deploy (container): nel container non c'e' systemctl; lo stato del
systemd dell'HOST si legge via SSH verso l'host (vedi SystemdConfig in
config.py, sezione `systemd` di config.yaml).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
from dataclasses import dataclass

from config import settings
from services.errors import exc_text, ssh_error

log = logging.getLogger("systemd")


@dataclass
class UnitStatus:
    unit: str
    label: str
    load_state: str       # loaded | not-found | masked
    active_state: str     # active | inactive | failed | activating ...
    sub_state: str        # running | exited | dead | failed ...
    critical: bool
    available: bool       # systemctl raggiungibile / unit esiste
    host: str = ""        # host su cui gira l'unit (vuoto = host di default)
    dashboard: bool = False   # scelto dal proprietario: compare fra i servizi in evidenza

    @property
    def ok(self) -> bool:
        return self.active_state == "active"

    def to_dict(self) -> dict:
        return {
            "kind": "systemd",
            "name": self.unit,
            "label": self.label or self.unit,
            "load_state": self.load_state,
            "active_state": self.active_state,
            "sub_state": self.sub_state,
            "status": self.active_state,
            "ok": self.ok,
            "critical": self.critical,
            "available": self.available,
            "host": self.host,
            "dashboard": self.dashboard,
        }


class SystemdMonitor:

    async def status(self, units: list[dict]) -> list[UnitStatus]:
        # Deploy in container: il systemd dell'host si legge via SSH (no systemctl locale).
        if settings.systemd.ssh_enabled:
            return await self._status_ssh_multi(units)

        if not shutil.which("systemctl"):
            log.warning("systemctl non disponibile: monitor systemd inattivo")
            return [_unavailable(u) for u in units]

        return await asyncio.gather(*[self._one(u) for u in units])

    # ── systemctl locale ──────────────────────────────────────────
    async def _one(self, unit: dict) -> UnitStatus:
        props = await self._show(unit["unit"])
        return _from_props(unit, props)

    async def _show(self, unit: str) -> dict:
        args = ["systemctl", "show", unit,
                "-p", "LoadState", "-p", "ActiveState", "-p", "SubState"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception as e:
            log.warning(f"systemctl show {unit}: {exc_text(e)}")
            return {}
        return _parse_props(stdout.decode(errors="replace"))

    # ── systemctl via SSH (multi-host) ────────────────────────────
    async def _status_ssh_multi(self, units: list[dict]) -> list[UnitStatus]:
        """Raggruppa le unit per host (campo `host`, vuoto = host di default) e
        interroga ogni host con una singola connessione SSH."""
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for u in units:
            groups[(u.get("host") or "").strip()].append(u)
        results = await asyncio.gather(
            *[self._status_ssh_host(host_id, us) for host_id, us in groups.items()]
        )
        out: list[UnitStatus] = []
        for r in results:
            out.extend(r)
        return out

    async def _status_ssh_host(self, host_id: str, units: list[dict]) -> list[UnitStatus]:
        import asyncssh
        from services.ssh_hosts import resolve_ssh_target
        kwargs = resolve_ssh_target(host_id)
        try:
            # Una sola connessione per host, unit in parallelo sui canali.
            async with asyncssh.connect(**kwargs) as conn:
                props_list = await asyncio.gather(
                    *[self._show_ssh(conn, u["unit"]) for u in units]
                )
        except Exception as e:
            log.warning(
                f"systemd via SSH non disponibile ({kwargs.get('username')}@"
                f"{kwargs.get('host')}): {ssh_error(e)}"
            )
            return [_unavailable(u) for u in units]
        return [_from_props(u, props) for u, props in zip(units, props_list)]

    async def _show_ssh(self, conn, unit: str) -> dict:
        cmd = (f"systemctl show {shlex.quote(unit)} "
               f"-p LoadState -p ActiveState -p SubState")
        try:
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=10)
            return _parse_props(result.stdout or "")
        except Exception as e:
            log.warning(f"systemctl show {unit} (ssh): {exc_text(e)}")
            return {}


def _parse_props(raw: str) -> dict:
    """Parsa l'output `KEY=VALUE` di `systemctl show`."""
    props = {}
    for line in raw.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    return props


def _in_dashboard(unit: dict) -> bool:
    """Chi compare fra i servizi in evidenza della dashboard.

    Quando la chiave manca vale `critical`: e' cio' che la dashboard mostrava
    prima che la scelta diventasse esplicita, quindi un services.yaml gia' in
    esercizio continua a mostrare le stesse unit senza essere modificato.
    """
    return bool(unit.get("dashboard", unit.get("critical", False)))


def _from_props(unit: dict, props: dict) -> UnitStatus:
    return UnitStatus(
        unit=unit["unit"],
        label=unit.get("label", ""),
        load_state=props.get("LoadState", "unknown"),
        active_state=props.get("ActiveState", "unknown"),
        sub_state=props.get("SubState", "unknown"),
        critical=bool(unit.get("critical", False)),
        available=bool(props),
        host=(unit.get("host") or "").strip(),
        dashboard=_in_dashboard(unit),
    )


def _unavailable(unit: dict) -> UnitStatus:
    return UnitStatus(
        unit=unit["unit"], label=unit.get("label", ""),
        load_state="unknown", active_state="unknown", sub_state="unknown",
        critical=bool(unit.get("critical", False)), available=False,
        host=(unit.get("host") or "").strip(),
        dashboard=_in_dashboard(unit),
    )


_monitor: SystemdMonitor | None = None


def get_systemd_monitor() -> SystemdMonitor:
    global _monitor
    if _monitor is None:
        _monitor = SystemdMonitor()
    return _monitor
