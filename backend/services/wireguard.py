"""
services/wireguard.py — Connettore WireGuard
=============================================
Legge lo stato dei peer dal router via SSH (`wg show all dump`) e ne fa
il parsing (formato TSV). WireGuard gira sul router, non sull'host del
backend: la sorgente e' sempre la connessione SSH al router.
"""
from __future__ import annotations

import logging
import shlex
import time
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from services.openwrt import get_ssh

log = logging.getLogger("wireguard")

# Quanto un handshake resta "recente". WireGuard ne fa uno almeno ogni due
# minuti finche' il tunnel e' su (rekey + keepalive), quindi tre minuti di
# silenzio vogliono dire che non passa piu' niente. La stessa soglia la usa la
# regola di alert sul relay (services/alerts.py): deve essere una sola.
HANDSHAKE_ATTIVO = 180


@dataclass
class WGPeer:
    public_key: str
    preshared_key: str
    endpoint: str
    allowed_ips: list[str]
    last_handshake: int       # timestamp unix (0 = mai)
    rx_bytes: int
    tx_bytes: int
    name: str = ""

    @property
    def last_handshake_ago(self) -> str:
        if self.last_handshake == 0:
            return "never"
        delta = int(time.time()) - self.last_handshake
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        return f"{delta // 3600}h ago"

    @property
    def is_active(self) -> bool:
        """Attivo se l'ultimo handshake e' piu' recente di HANDSHAKE_ATTIVO."""
        if self.last_handshake == 0:
            return False
        return (int(time.time()) - self.last_handshake) < HANDSHAKE_ATTIVO

    def to_dict(self) -> dict:
        return {
            "name": self.name or self.public_key[:16] + "…",
            "public_key": self.public_key,
            "endpoint": self.endpoint,
            "allowed_ips": self.allowed_ips,
            "last_handshake": self.last_handshake,
            "last_handshake_ago": self.last_handshake_ago,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "rx_mb": round(self.rx_bytes / 1_048_576, 2),
            "tx_mb": round(self.tx_bytes / 1_048_576, 2),
            "status": "active" if self.is_active else "idle",
        }


@dataclass
class WGInterface:
    name: str
    public_key: str
    listen_port: int
    peers: list[WGPeer] = field(default_factory=list)

    @property
    def active_peers(self) -> int:
        return sum(1 for p in self.peers if p.is_active)

    @property
    def total_peers(self) -> int:
        return len(self.peers)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "public_key": self.public_key,
            "listen_port": self.listen_port,
            "peers": [p.to_dict() for p in self.peers],
            "active_peers": self.active_peers,
            "total_peers": self.total_peers,
        }


def parse_wg_dump(output: str, peer_catalog: dict[str, str] | None = None) -> list[WGInterface]:
    """
    Parsa `wg show all dump` (TSV).
      Riga iface: iface  priv  pub  listen_port  fwmark        (5 campi)
      Riga peer:  iface  pub   psk  endpoint  allowed  hs  rx  tx  keepalive  (9 campi)
    peer_catalog: dict pubkey -> nome leggibile.
    """
    catalog = peer_catalog or {}
    ifaces: dict[str, WGInterface] = {}
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            name, _priv, pub, port, _fwmark = parts
            ifaces[name] = WGInterface(
                name=name, public_key=pub,
                listen_port=int(port) if port.isdigit() else 0,
            )
        elif len(parts) == 9:
            name, pub, psk, endpoint, allowed, hs, rx, tx, _ka = parts
            peer = WGPeer(
                public_key=pub,
                preshared_key=psk,
                endpoint=endpoint if endpoint != "(none)" else "",
                allowed_ips=[a.strip() for a in allowed.split(",") if a.strip()],
                last_handshake=int(hs) if hs.isdigit() else 0,
                rx_bytes=int(rx) if rx.isdigit() else 0,
                tx_bytes=int(tx) if tx.isdigit() else 0,
                name=catalog.get(pub, ""),
            )
            ifaces.setdefault(name, WGInterface(name=name, public_key="", listen_port=0)).peers.append(peer)
    return list(ifaces.values())


class WireGuardService:

    def __init__(self):
        self.ssh = get_ssh()
        self._catalog: dict[str, str] = dict(settings.wireguard.peer_names)

    def load_catalog(self, catalog: dict[str, str]):
        self._catalog = catalog

    async def get_status(self) -> list[WGInterface]:
        # `wg show all dump` produce righe a 5 campi (iface) e 9 (peer), con il
        # nome interfaccia in prima colonna: e' il formato atteso da
        # parse_wg_dump. La variante `wg show <iface> dump` omette la colonna
        # nome (4/8 campi) e non verrebbe parsata.
        stdout, stderr = await self.ssh.run("wg show all dump 2>/dev/null")
        if not stdout.strip():
            log.warning(f"wg show returned empty (stderr: {stderr})")
            return []
        return parse_wg_dump(stdout, self._catalog)

    async def get_config_file(self) -> str:
        """Legge il file di config WG oscurando ogni materiale segreto:
        PrivateKey e PresharedKey (qualsiasi riga con una chiave)."""
        stdout, _ = await self.ssh.run(
            f"cat {shlex.quote(settings.wireguard.config_file)} 2>/dev/null")
        lines = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if "=" in stripped and stripped.split("=", 1)[0].strip() in (
                    "PrivateKey", "PresharedKey"):
                key = stripped.split("=", 1)[0].strip()
                lines.append(f"{key} = [REDACTED]")
            else:
                lines.append(line)
        return "\n".join(lines)

    async def reload(self) -> bool:
        # Su OpenWrt c'e' BusyBox /bin/sh che NON supporta la process
        # substitution `<(...)`. Usiamo una pipe esplicita: legge da stdin
        # via `/dev/stdin` cosi' funziona anche con sh POSIX puro.
        iface = shlex.quote(settings.wireguard.interface)
        _, stderr = await self.ssh.run(
            f"wg-quick strip {iface} | wg syncconf {iface} /dev/stdin"
        )
        return "error" not in stderr.lower()


_wg_service: Optional[WireGuardService] = None


def get_wireguard_service() -> WireGuardService:
    global _wg_service
    if _wg_service is None:
        _wg_service = WireGuardService()
    return _wg_service
