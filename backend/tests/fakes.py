"""
Connettori finti per i test di integrazione.

Non simulano l'infrastruttura: restituiscono la forma minima che i router si
aspettano, cosi' un endpoint puo' essere esercitato end-to-end senza aprire una
sola connessione. Servono a prendere gli errori di import e di serializzazione,
non a verificare la logica dei connettori (quella sta negli unit test).

I router chiamano le factory nel corpo dell'handler, quindi il modo piu'
economico di iniettarli e' pre-caricare il globale privato del modulo: da quel
momento ogni `get_x()` restituisce il fake, ovunque venga chiamato.
"""
from __future__ import annotations

from services.registry import Device


class FakeRouterClient:
    """services.openwrt.RouterClient (alias get_luci)."""

    async def get_system_info(self):
        from services.openwrt import RouterInfo
        return RouterInfo(hostname="router-di-test", model="Modello", os_version="OpenWrt 23.05",
                          uptime_seconds=3600, load=[0.1, 0.2, 0.3],
                          memory_total=128 * 1024 * 1024, memory_free=64 * 1024 * 1024)

    async def get_interfaces(self):
        return []

    async def get_dhcp_leases(self):
        return []


class FakeSSH:
    """services.openwrt.SSHClient."""

    async def logread(self, lines: int = 100, filters=None, exclude=None):
        return ("Sun Aug 16 19:59:01 2026 daemon.err dnsmasq[1234]: "
                "failed to send packet\n")

    async def ping_host(self, ip: str, count: int = 2):
        return True, 1.5

    async def get_nftables_rules(self):
        return "table inet fw4 {}"


class FakeDockerManager:
    async def list_containers(self):
        return []

    async def list_networks(self):
        return []

    def hosts(self):
        return [{"name": "host-di-test", "reachable": True, "error": "", "seen_ok": True}]

    async def aclose(self):
        pass


class FakeScanner:
    def __init__(self):
        self.chiamate = 0

    def get_cached(self):
        return [Device(ips=["192.0.2.10"], mac="AA:BB:CC:DD:EE:FF",
                       hostname="host.lan", online=True)]

    async def publish_cached(self):
        pass

    async def scan(self):
        self.chiamate += 1


class FakeWireGuard:
    async def get_status(self):
        return []

    async def get_config_file(self):
        return ""

    async def reload(self):
        return True


# ── Connettori che falliscono ──────────────────────────────────────
# I fake qui sopra riescono sempre: il ramo d'errore di ogni router e' quindi
# codice che nessun test ha mai eseguito. Questi sollevano, cosi' si puo'
# verificare cosa arriva al client quando l'infrastruttura non risponde.

class FakeRouterClientRotto:
    async def get_system_info(self):
        raise RuntimeError("router irraggiungibile")

    async def get_interfaces(self):
        raise TimeoutError()

    async def get_dhcp_leases(self):
        raise RuntimeError("ubus non risponde")


class FakeSSHRotto:
    async def logread(self, lines: int = 100, filters=None, exclude=None):
        raise RuntimeError("connessione SSH rifiutata")

    async def run(self, cmd: str):
        raise RuntimeError("connessione SSH rifiutata")


class FakeDockerManagerRotto:
    async def list_containers(self):
        raise RuntimeError("docker.sock non accessibile")

    async def list_networks(self):
        raise RuntimeError("docker.sock non accessibile")

    def hosts(self):
        return []

    async def aclose(self):
        pass
