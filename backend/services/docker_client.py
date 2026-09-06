"""
services/docker_client.py — Connettore Docker multi-host
========================================================
Monitora i container Docker su PIU' host:
  - host locale (dove gira il backend) -> Engine API via socket Unix
  - host remoti                        -> 'docker ps/stats' via SSH
                                          (riusa l'SSH gia' presente in LAN,
                                           nessuna porta Docker da esporre)
  - opzionale: Engine API via TCP (method=tcp), solo rete trusted

Il `DockerManager` costruisce un client per ogni host (locale + espliciti +
auto-scoperti dagli host SSH della discovery) e aggrega i container,
ognuno etichettato con il proprio host.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import socket
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from config import settings
from services.errors import exc_text, ssh_error

log = logging.getLogger("docker")

_API_VER = "v1.43"


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str          # running | exited | restarting | paused ...
    state: str           # stringa di stato completa
    host: str = "local"  # host Docker di appartenenza
    created: int = 0
    ports: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    cpu_percent: float = 0.0
    mem_usage_mb: float = 0.0
    mem_limit_mb: float = 0.0
    rx_bytes: int = 0
    tx_bytes: int = 0

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    def to_dict(self) -> dict:
        return {
            "id": self.id[:12], "name": self.name.lstrip("/"), "image": self.image,
            "status": self.status, "state": self.state, "host": self.host,
            "created": self.created, "ports": self.ports, "networks": self.networks,
            "cpu_percent": round(self.cpu_percent, 1),
            "mem_usage_mb": round(self.mem_usage_mb, 1), "mem_limit_mb": round(self.mem_limit_mb, 1),
            "rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes, "running": self.is_running,
        }


@dataclass
class NetworkInfo:
    id: str
    name: str
    driver: str
    subnet: str
    gateway: str
    host: str = "local"
    containers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id[:12], "name": self.name, "driver": self.driver,
                "subnet": self.subnet, "gateway": self.gateway, "host": self.host,
                "container_count": len(self.containers)}


# ── Client Engine API (socket Unix locale o TCP) ──────────────────

class EngineDockerClient:
    def __init__(self, name: str, socket: str | None = None,
                 host: str | None = None, port: int = 2375):
        self.name = name
        self._socket = socket
        self._host = host
        self._port = port
        self._client: Optional[httpx.AsyncClient] = None
        # Ultimo errore di comunicazione con questo host. Vuoto = raggiungibile.
        self.last_error = ""
        # Vedi `_traccia`: il log segna i cambi di stato, non ogni tentativo.
        self._errore_loggato = ""
        # Ha mai risposto da quando il servizio e' partito? Un host che non ha
        # mai risposto e' una questione di configurazione, non un guasto.
        self.ok_once = False

    def _ensure(self):
        if self._client is not None:
            return
        if self._host:
            self._client = httpx.AsyncClient(base_url=f"http://{self._host}:{self._port}", timeout=15)
        else:
            transport = httpx.AsyncHTTPTransport(uds=self._socket)
            self._client = httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15)

    async def _get(self, path: str, params: dict | None = None,
                   traccia: bool = True) -> Any:
        """`traccia=False` per le letture accessorie: non devono toccare lo
        stato di raggiungibilita' dell'host, che alimenta l'alert
        `docker.host_giu`. Un inspect fallito non e' un host giu', e uno
        riuscito non e' la prova che lo sia tornato."""
        self._ensure()
        try:
            r = await self._client.get(f"/{_API_VER}{path}", params=params or {})
            r.raise_for_status()
        except Exception as e:
            if traccia:
                self.last_error = exc_text(e)
                self._traccia(self.last_error)
            raise
        if traccia:
            self.last_error = ""
            self.ok_once = True
            self._traccia("")
        return r.json()

    def _traccia(self, errore: str) -> None:
        """Come `SSHDockerClient._traccia`: si scrive al cambio di stato.

        Un Engine irraggiungibile fallisce ad ogni ciclo, e ripeterne l'errore
        riempie l'archivio dei log senza aggiungere niente a quello che
        `last_error` gia' dice alla UI.
        """
        if errore == self._errore_loggato:
            return
        if errore:
            log.error(f"docker {self.name}: {errore}")
        else:
            log.info(f"docker {self.name}: torna a rispondere")
        self._errore_loggato = errore

    @property
    def is_local(self) -> bool:
        """True se parla col Docker della macchina su cui gira il backend
        (socket Unix), non con un Engine remoto via TCP."""
        return not self._host

    async def inspect(self, cid: str) -> dict:
        """Dettagli di un container (`docker inspect`). Serve, fra l'altro, a
        sapere se questo container ha una politica di riavvio."""
        return await self._get(f"/containers/{cid}/json", traccia=False)

    async def list_containers(self, all: bool = True) -> list[ContainerInfo]:
        data = await self._get("/containers/json", {"all": 1 if all else 0})
        out = []
        for c in data:
            ports = []
            for p in c.get("Ports", []):
                ports.append(f"{p['PrivatePort']}/{p['Type']} -> {p.get('IP','0.0.0.0')}:{p['PublicPort']}"
                             if p.get("PublicPort") else f"{p['PrivatePort']}/{p['Type']}")
            out.append(ContainerInfo(
                id=c["Id"], name=c["Names"][0] if c.get("Names") else c["Id"][:12],
                image=c["Image"], status=c["State"], state=c["Status"], host=self.name,
                created=c.get("Created", 0), ports=ports,
                networks=list(c.get("NetworkSettings", {}).get("Networks", {}).keys()),
                labels=c.get("Labels", {}) or {},
            ))
        await self._merge_stats(out)
        return out

    async def _merge_stats(self, containers: list[ContainerInfo]):
        running = [c for c in containers if c.is_running][:15]
        res = await asyncio.gather(*[self._stats(c.id) for c in running], return_exceptions=True)
        for c, s in zip(running, res):
            if isinstance(s, dict):
                c.cpu_percent = s["cpu_percent"]; c.mem_usage_mb = s["mem_usage_mb"]
                c.mem_limit_mb = s["mem_limit_mb"]; c.rx_bytes = s["rx_bytes"]; c.tx_bytes = s["tx_bytes"]

    async def _stats(self, cid: str) -> dict:
        data = await self._get(f"/containers/{cid}/stats", {"stream": 0})
        cpu = 0.0
        try:
            cd = data["cpu_stats"]["cpu_usage"]["total_usage"] - data["precpu_stats"]["cpu_usage"]["total_usage"]
            sd = data["cpu_stats"]["system_cpu_usage"] - data["precpu_stats"]["system_cpu_usage"]
            n = data["cpu_stats"].get("online_cpus", 1)
            if sd > 0:
                cpu = (cd / sd) * n * 100.0
        except (KeyError, ZeroDivisionError) as e:
            # Un container al 0% e uno non misurabile non sono la stessa cosa:
            # almeno resta la traccia di quale delle due si sta guardando.
            log.debug(f"docker stats: cpu non calcolabile ({exc_text(e)})")
        mu = data.get("memory_stats", {}).get("usage", 0)
        ml = data.get("memory_stats", {}).get("limit", 0)
        rx = tx = 0
        for net in data.get("networks", {}).values():
            rx += net.get("rx_bytes", 0); tx += net.get("tx_bytes", 0)
        return {"cpu_percent": round(cpu, 1), "mem_usage_mb": mu / 1_048_576,
                "mem_limit_mb": ml / 1_048_576, "rx_bytes": rx, "tx_bytes": tx}

    async def list_networks(self) -> list[NetworkInfo]:
        data = await self._get("/networks")
        out = []
        for n in data:
            cfg = (n.get("IPAM", {}).get("Config", [{}]) or [{}])
            out.append(NetworkInfo(id=n["Id"], name=n["Name"], driver=n.get("Driver", ""),
                                   subnet=cfg[0].get("Subnet", ""), gateway=cfg[0].get("Gateway", ""),
                                   host=self.name, containers=list(n.get("Containers", {}).keys())))
        return out

    async def container_action(self, cid: str, action: str) -> bool:
        self._ensure()
        r = await self._client.post(f"/{_API_VER}/containers/{cid}/{action}")
        return r.status_code < 400

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── Client Docker via SSH (host remoti) ───────────────────────────

class SSHDockerClient:
    def __init__(self, name: str, host: str, port: int = 22,
                 user: str | None = None, key: str | None = None, password: str | None = None):
        self.name = name
        self.host = host
        self.port = port
        self.user = user
        self.key = key
        self.password = password
        # Ultimo errore di comunicazione con questo host. Vuoto = raggiungibile.
        self.last_error = ""
        # Ha mai risposto da quando il servizio e' partito? Un host che non ha
        # mai risposto e' una questione di configurazione, non un guasto.
        self.ok_once = False
        # Vedi `_traccia`: il log segna i cambi di stato, non ogni tentativo.
        self._errore_loggato = ""

    async def _run(self, cmd: str) -> tuple[str, int]:
        import asyncssh
        # Senza tetto sulla connect, un host con la porta 22 filtrata (non
        # rifiutata) blocca la raccolta per l'intero timeout TCP del kernel.
        kw: dict[str, Any] = {"host": self.host, "port": self.port,
                              "username": self.user, "known_hosts": None,
                              "connect_timeout": settings.discovery.ssh.connect_timeout}
        if self.key:
            kw["client_keys"] = [self.key]
        elif self.password:
            kw["password"] = self.password
        try:
            async with asyncssh.connect(**kw) as conn:
                r = await asyncio.wait_for(conn.run(cmd, check=False), timeout=20)
                self.last_error = ""
                self.ok_once = True
                self._traccia("")
                return (r.stdout or ""), (r.exit_status or 0)
        except Exception as e:
            # Il solo log non bastava: `list_containers` ritorna una lista vuota,
            # che a valle e' indistinguibile da un host senza container. I
            # container sparivano dalla dashboard e i contatori calavano senza
            # che nessuno potesse accorgersene dalla UI.
            self.last_error = ssh_error(e)
            self._traccia(self.last_error)
            return "", 1

    def _traccia(self, errore: str) -> None:
        """Scrive nel log solo quando lo stato dell'host cambia.

        Prima si scriveva ad ogni tentativo: sei host spenti, due chiamate a
        testa per ciclo lento, e un terzo di tutto il log del backend era la
        stessa frase ripetuta all'infinito. Con `logs.max_rows` a 50.000 righe
        quel rumore accorciava l'orizzonte dell'archivio a poco piu' di un
        giorno. Lo stato vero resta comunque leggibile in ogni momento in
        `last_error`, che e' quello che alimenta la UI e l'alert
        `docker.host_giu`: il log serve a datare il cambiamento, non a
        ripeterlo.
        """
        if errore == self._errore_loggato:
            return
        if errore:
            log.warning(f"docker/ssh {self.host}: {errore}")
        else:
            log.info(f"docker/ssh {self.host}: torna a rispondere")
        self._errore_loggato = errore

    async def list_containers(self, all: bool = True) -> list[ContainerInfo]:
        flag = "-a" if all else ""
        out, code = await self._run(f"docker ps {flag} --no-trunc --format '{{{{json .}}}}'")
        if code != 0 or not out.strip():
            return []
        containers = []
        for line in out.strip().splitlines():
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            state = (c.get("State") or "").lower() or _state_from_status(c.get("Status", ""))
            containers.append(ContainerInfo(
                id=c.get("ID", ""), name=c.get("Names", ""), image=c.get("Image", ""),
                status=state, state=c.get("Status", ""), host=self.name,
                ports=[p.strip() for p in (c.get("Ports", "") or "").split(",") if p.strip()],
                labels={},
            ))
        await self._merge_stats(containers)
        return containers

    async def _merge_stats(self, containers: list[ContainerInfo]):
        out, code = await self._run("docker stats --no-stream --no-trunc --format '{{json .}}'")
        if code != 0 or not out.strip():
            return
        by_name = {c.name: c for c in containers}
        for line in out.strip().splitlines():
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = by_name.get(s.get("Name", ""))
            if not c:
                continue
            c.cpu_percent = _pct(s.get("CPUPerc"))
            c.mem_usage_mb, c.mem_limit_mb = _mem(s.get("MemUsage"))

    async def list_networks(self) -> list[NetworkInfo]:
        return []   # non essenziale via SSH

    async def container_action(self, cid: str, action: str) -> bool:
        # cid arriva dal path param dell'API: quotare per evitare injection sullo
        # shell dell'host remoto (action e' gia' whitelistata nel manager).
        _, code = await self._run(f"docker {shlex.quote(action)} {shlex.quote(cid)}")
        return code == 0


# ── Manager multi-host ────────────────────────────────────────────

class DockerManager:
    def __init__(self):
        self._clients: list | None = None

    def _build(self) -> list:
        clients: list = []
        seen_hosts: set[str] = set()
        cfg = settings.docker
        local = False
        if cfg.local_enabled:
            clients.append(EngineDockerClient(cfg.local_name, socket=cfg.socket))
            local = True
        # host espliciti
        for hcfg in cfg.hosts:
            if hcfg.method == "tcp" and hcfg.host:
                clients.append(EngineDockerClient(hcfg.name, host=hcfg.host, port=hcfg.port))
            elif hcfg.host:
                clients.append(SSHDockerClient(
                    hcfg.name, hcfg.host, 22,
                    hcfg.user or settings.discovery.ssh.default_user,
                    hcfg.key or settings.discovery.ssh.default_key, hcfg.password))
            if hcfg.host:
                seen_hosts.add(hcfg.host)
        # autodiscovery: interroga gli host SSH della discovery (docker, se presente)
        if cfg.autodiscover and settings.discovery.ssh.enabled:
            for sh in settings.discovery.ssh.hosts:
                if sh.ip in seen_hosts:
                    continue
                # L'host SSH puo' essere la macchina stessa su cui gira il backend
                # (che sta anche in discovery.ssh.hosts): interrogarla di
                # nuovo via SSH duplicherebbe ogni suo container, che il socket
                # locale ha gia' elencato.
                if local and _is_local_address(sh.ip):
                    log.debug(f"docker: {sh.ip} e' questa macchina, gia' coperta dal socket locale")
                    continue
                # Un host dichiarato Windows non entra da solo fra gli host
                # Docker: il comando `docker ps` che si manda qui e' scritto per
                # una shell POSIX (le virgolette del formato non sopravvivono a
                # cmd.exe) e comparirebbe un host sempre vuoto. Chi ha davvero
                # Docker Desktop lo aggiunge a mano in `docker.hosts`, dove gli
                # host espliciti non passano da questo filtro.
                if getattr(sh, "os", "auto") == "windows":
                    log.debug(f"docker: {sh.ip} e' dichiarato Windows, autodiscovery saltata")
                    continue
                clients.append(SSHDockerClient(
                    sh.ip, sh.ip, sh.port,
                    sh.user or settings.discovery.ssh.default_user,
                    sh.key or settings.discovery.ssh.default_key, sh.password))
        return clients

    def clients(self) -> list:
        if self._clients is None:
            self._clients = self._build()
        return self._clients

    async def aclose(self):
        """Chiude i client che detengono risorse (httpx). Chiamato allo shutdown."""
        for c in (self._clients or []):
            closer = getattr(c, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception as e:
                    log.debug(f"docker client close: {e}")

    async def list_containers(self) -> list[ContainerInfo]:
        clients = self.clients()
        results = await asyncio.gather(
            *[c.list_containers(all=True) for c in clients], return_exceptions=True)
        out: list[ContainerInfo] = []
        # Lo stesso container non deve comparire due volte se due client puntano
        # (per configurazione) allo stesso Engine: l'ID e' identico via socket e
        # via `docker ps --no-trunc`, e vince il primo client (quello locale).
        seen_ids: set[str] = set()
        for client, r in zip(clients, results):
            if isinstance(r, list):
                for container in r:
                    if container.id and container.id in seen_ids:
                        log.debug(f"docker: {container.name} gia' visto su un altro host, ignorato")
                        continue
                    seen_ids.add(container.id)
                    out.append(container)
            elif isinstance(r, Exception):
                # Passa dal client, non da un `log.error` qui: cosi' vale la
                # regola del cambio di stato (niente riga per ciclo per host) e
                # nello stesso tempo un'eccezione che il client NON ha gia'
                # tracciato — una risposta malformata, per dire — resta visibile
                # invece di scomparire in un `debug`.
                client._traccia(exc_text(r))
        return out

    async def list_networks(self) -> list[NetworkInfo]:
        clients = self.clients()
        results = await asyncio.gather(
            *[c.list_networks() for c in clients], return_exceptions=True)
        out: list[NetworkInfo] = []
        for client, r in zip(clients, results):
            if isinstance(r, list):
                out.extend(r)
            elif isinstance(r, Exception):
                client._traccia(exc_text(r))
        return out

    async def container_action(self, host: str, cid: str, action: str) -> bool:
        if action not in {"start", "stop", "restart", "pause", "unpause"}:
            raise ValueError(f"Action non permessa: {action}")
        for c in self.clients():
            if c.name == host:
                return await c.container_action(cid, action)
        raise ValueError(f"Host Docker sconosciuto: {host}")

    def hosts(self) -> list[dict]:
        """Host Docker configurati, con la loro raggiungibilita'.

        Non solo i nomi: un host irraggiungibile va dichiarato, altrimenti la
        sua assenza si legge come "non ha container".
        """
        return [{"name": c.name,
                 "reachable": not getattr(c, "last_error", ""),
                 "error": getattr(c, "last_error", ""),
                 # Distingue "e' caduto" da "non ha mai risposto": gli host SSH
                 # arrivano dall'autodiscovery e molti non fanno girare Docker.
                 "seen_ok": bool(getattr(c, "ok_once", False))}
                for c in self.clients()]


# ── Helper ────────────────────────────────────────────────────────

def _is_local_address(ip: str) -> bool:
    """True se l'IP e' di questa macchina.

    Nessun elenco di indirizzi in config: si chiede al kernel. Una connect UDP
    non manda pacchetti, ma sceglie la rotta; se la destinazione e' un indirizzo
    nostro il socket locale risulta legato proprio a quell'indirizzo. Funziona
    perche' il container gira in `network_mode: host` e vede gli IP veri.
    """
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))          # discard port: nessun traffico generato
            return s.getsockname()[0] == ip
    except OSError as e:
        log.debug(f"docker: impossibile stabilire se {ip} e' locale ({e})")
        return False


# ── Helper parsing 'docker stats' ─────────────────────────────────

def _state_from_status(status: str) -> str:
    s = status.lower()
    if s.startswith("up"):
        return "running"
    if s.startswith("exited"):
        return "exited"
    if s.startswith("paused"):
        return "paused"
    return s.split()[0] if s else "unknown"


def _pct(v: str | None) -> float:
    if not v:
        return 0.0
    try:
        return round(float(str(v).replace("%", "").strip()), 1)
    except ValueError:
        return 0.0


_UNIT = {"B": 1 / 1_048_576, "KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "TIB": 1024 * 1024,
         "KB": 1 / 1000 / 1.048576, "MB": 1000 / 1024, "GB": 1000 * 1000 / 1_048_576}


def _to_mb(token: str) -> float:
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", token.strip())
    if not m:
        return 0.0
    val, unit = float(m.group(1)), m.group(2).upper()
    return round(val * _UNIT.get(unit, 0), 1)


def _mem(usage: str | None) -> tuple[float, float]:
    """'40MiB / 2GiB' -> (40.0, 2048.0)."""
    if not usage or "/" not in usage:
        return 0.0, 0.0
    used, limit = usage.split("/", 1)
    return _to_mb(used), _to_mb(limit)


_manager: DockerManager | None = None


def get_docker_manager() -> DockerManager:
    global _manager
    if _manager is None:
        _manager = DockerManager()
    return _manager
