"""
services/discovery.py — Provider di discovery pluggabili
========================================================
Lo scanner raccoglie i dispositivi da DHCP/ARP (sul router) e poi li
arricchisce con questi provider, ognuno indipendente e a fallimento
isolato. Aggiungere una sorgente = aggiungere una sottoclasse di
DiscoveryProvider e inserirla in build_providers().

Sorgenti incluse:
  - PingSweepProvider   host che rispondono al ping su tutte le subnet (stage fast)
  - NmapProvider        host vivi su tutta la subnet, porte aperte, OS, vendor
  - ReverseDNSProvider  hostname via PTR (anche .lan)
  - SSHFactsProvider    facts dagli host (OS, servizi, docker, interfacce) — host tuoi
  - SNMPProvider        device che parlano SNMP (AP, switch, stampanti)

Ogni provider dichiara uno `stage`:
  - "fast": deve concludersi in pochi secondi. Lo scanner pubblica il
    risultato subito dopo, cosi' un device nuovo compare quasi subito in UI.
  - "slow": arricchimento (porte, hostname, facts). Gira dopo la pubblicazione
    del risultato rapido e ne innesca una seconda a fine ciclo.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time

from config import settings
from services.errors import exc_text, ssh_error
from services.ping import sweep as ping_sweep, terminate_process
from services.registry import DeviceRegistry
from services.ssh_hosts import esegui_powershell, live_ssh_config, risolvi_os

log = logging.getLogger("discovery")


class DiscoveryProvider:
    name = "base"
    stage = "slow"                    # "fast" | "slow"

    async def run(self, registry: DeviceRegistry) -> None:
        raise NotImplementedError


# ── ping sweep ─────────────────────────────────────────────────────

class PingSweepProvider(DiscoveryProvider):
    """Spazzola con ping le subnet configurate con scan=True.

    E' la sorgente piu' rapida e non dipende dal router, che aggiorna DHCP/ARP
    con ritardo: per questo sta nello stage rapido, mentre nmap (che aggiunge
    porte e vendor ma e' piu' lento) sta in quello lento.
    """
    name = "ping"
    stage = "fast"

    async def run(self, registry: DeviceRegistry) -> None:
        cfg = settings.discovery.ping_sweep
        if not cfg.enabled:
            return
        alive = await ping_sweep(settings.scan_subnets,
                                 concurrency=cfg.concurrency, timeout=cfg.timeout)
        now = int(time.time())
        for ip, latency in alive.items():
            dev = registry.upsert(ip=ip, source=self.name)
            # Lo sweep e' gia' una misura di raggiungibilita': evita di ripingare.
            dev.online = True
            dev.latency_ms = latency
            dev.last_seen = now


# ── nmap ───────────────────────────────────────────────────────────

class NmapProvider(DiscoveryProvider):
    """Porte aperte, vendor e OS guess; trova anche host che non rispondono al ping.

    Attenzione a --privileged: nmap decide se puo' usare i raw socket guardando
    l'uid, non le capability del binario. Girando come utente normale si
    dichiarava "not root" e ripiegava su un ping TCP verso la porta 80, quindi
    ogni host che non espone quella porta risultava spento (era il caso di quasi
    tutta la rete). I raw socket funzionano: gli mancava solo il permesso di
    provarci.

    Fissare l'interfaccia con -e, invece, fa perdere host e va usato solo come
    ripiego. Misurato il 2026-08-19 sulle tre subnet (768 indirizzi): senza -e
    nmap trova 7 host, con -e enp1s0f0 ne trova 4 e con -e wlp2s0 ne trova 5.
    Il motivo e' che quell'host ha due schede sulla stessa subnet come fallback
    e le altre subnet stanno dietro il router: e' il routing del kernel a sapere
    da dove uscire per ogni destinazione, mentre una scheda fissa taglia fuori
    tutto quello che risponde dall'altra. La sonda di _pick_interface non se ne
    accorgeva perche' valida una scheda su un singolo host acceso, non su tutte
    le subnet. Quindi: di default nessun -e, e lo si prova solo se uno scan
    torna a mani vuote.
    """
    name = "nmap"

    def __init__(self):
        # Ripiego attivo: vuoto = lascia scegliere al routing del kernel.
        self._iface: str = ""

    async def run(self, registry: DeviceRegistry) -> None:
        cfg = settings.discovery.nmap
        if not shutil.which("nmap"):
            log.warning("nmap non installato: provider nmap saltato")
            return

        args = ["nmap", "-T4", "-oG", "-"]
        if cfg.privileged:
            args.append("--privileged")
        if not cfg.resolve_names:
            args.append("-n")
        iface = cfg.interface or self._iface
        if iface:
            args += ["-e", iface]
        if cfg.ping_scan and not cfg.port_scan:
            args.append("-sn")
        if cfg.port_scan:
            args += ["-sT", "-p", cfg.ports]
        if cfg.os_detection:
            args.append("-O")
        if cfg.extra_args:
            args += cfg.extra_args.split()
        # `--exclude` toglie quegli indirizzi dallo scan **per intero**, ping
        # compreso: per non farli sparire dai dispositivi si ripassa dopo con un
        # ping scan dedicato (`_scan_esclusi`).
        fuori = fuori_dal_port_scan(cfg) if cfg.port_scan else []
        if fuori:
            args += ["--exclude", ",".join(fuori)]
        args += settings.scan_subnets

        started = time.monotonic()
        out = await _run_cmd(args, timeout=300)
        hosts = _parse_nmap_greppable(out)
        # Il ripiego sull'interfaccia si giudica sullo scan vero e proprio: gli
        # esclusi sono pochi indirizzi noti e trovarli non dimostra che il
        # percorso verso il resto della rete funzioni.
        scan_a_vuoto = not hosts
        if fuori:
            hosts += await self._scan_esclusi(cfg, iface, fuori)
        log.info(f"nmap: {len(hosts)} host su {len(settings.scan_subnets)} subnet "
                 f"in {time.monotonic() - started:.1f}s")
        if scan_a_vuoto and not cfg.interface:
            # Scan a vuoto: si alterna fra routing del kernel e una scheda scelta
            # con la sonda, cosi' un percorso rotto non resta bloccato per sempre.
            self._iface = "" if iface else await self._pick_interface(cfg, registry)
        for host in hosts:
            dev = registry.upsert(mac=host.get("mac", ""), ip=host["ip"],
                                  hostname=host.get("hostname", ""), source=self.name)
            self._apply(dev, host)

    async def _scan_esclusi(self, cfg, iface: str, fuori: list[str]) -> list[dict]:
        """Ping scan sugli indirizzi tenuti fuori dal port scan.

        Sono pochi e noti, quindi costa una frazione di secondo. Serve perche'
        senza questo passaggio il router sparirebbe dall'elenco dei dispositivi:
        `--exclude` lo toglie da tutto lo scan, non solo dalle porte.
        """
        args = ["nmap", "-sn", "-oG", "-"]
        if cfg.privileged:
            args.insert(1, "--privileged")
        if not cfg.resolve_names:
            args.append("-n")
        if iface:
            args += ["-e", iface]
        args += fuori
        try:
            return _parse_nmap_greppable(await _run_cmd(args, timeout=60))
        except Exception as e:
            # Un errore qui costa la presenza del router fra i risultati di
            # questo giro, non lo scan: gli altri provider lo trovano comunque.
            log.warning(f"nmap: ping scan degli indirizzi esclusi fallito ({e})")
            return []

    # ── Scelta dell'interfaccia ────────────────────────────────────

    async def _pick_interface(self, cfg, registry: DeviceRegistry) -> str:
        """Scheda da passare a -e quando il routing del kernel non produce nulla.

        Ordine di prova: prima le cablate, poi le wireless. La verifica non e'
        teorica: si scansiona un host che risulta gia' acceso e si guarda se
        nmap lo vede. Vale come ripiego, non come scelta preferita (vedi la
        docstring della classe).
        """
        candidates = await _local_interfaces()
        if len(candidates) < 2:
            return ""                      # una sola strada: quella di default
        local_ips = {ip for _, ip in candidates}
        target = _probe_target(registry, local_ips)
        if not target:
            log.debug("nmap: nessun host acceso da usare come sonda")
            return ""

        for dev in candidates:
            if await self._probe(cfg, dev[0], target):
                log.warning(f"nmap: scan a vuoto col routing di sistema, ripiego "
                            f"sull'interfaccia {dev[0]} (le risposte da {target} "
                            f"rientrano da li')")
                return dev[0]
        log.warning(f"nmap: nessuna interfaccia locale riceve le risposte da {target}; "
                    "uso il default (i risultati possono essere incompleti)")
        return ""

    @staticmethod
    async def _probe(cfg, dev: str, target: str) -> bool:
        args = ["nmap", "-sn", "-n", "-PE", "-e", dev, target]
        if cfg.privileged:
            args.insert(1, "--privileged")
        out = await _run_cmd(args + ["-oG", "-"], timeout=20)
        return any(h["ip"] == target for h in _parse_nmap_greppable(out))

    @staticmethod
    def _apply(dev, host: dict):
        if host.get("vendor") and not dev.vendor:
            dev.vendor = host["vendor"]
        if host.get("os") and not dev.os_guess:
            dev.os_guess = host["os"]
        for p in host.get("ports", []):
            if p not in dev.open_ports:
                dev.open_ports.append(p)
        dev.open_ports.sort()


def fuori_dal_port_scan(cfg) -> list[str]:
    """Indirizzi da cercare col ping ma da NON port-scansionare.

    Il motivo e' il syslog del router: ogni connect TCP verso le sue porte gli
    finisce nel log, e con un buffer da 64 KB il rumore del monitoraggio
    spingeva fuori gli eventi veri in meno di un'ora (misurato il 2026-09-03:
    226 righe su 243 in 50 minuti erano nostre, sulla porta 22 dei suoi sei
    indirizzi; i login SSH veri erano 17). Nasconderle in pagina non serviva a
    niente, perche' il buffer si riempiva lo stesso: si tolgono alla fonte.
    """
    fuori = [x.strip() for x in (cfg.port_scan_exclude or []) if x and x.strip()]
    if cfg.port_scan_skip_router:
        fuori += _indirizzi_router()
    # Senza doppioni e in ordine stabile: `--exclude` li accetterebbe comunque,
    # ma un comando che cambia forma ad ogni giro e' illeggibile nei log.
    visti: set[str] = set()
    unici: list[str] = []
    for x in fuori:
        if x in visti:
            continue
        if not _e_indirizzo(x):
            # Solo indirizzi e CIDR. Un nome che non si risolve non fa saltare
            # l'esclusione: fa **uscire nmap** ("Error resolving name ...
            # QUITTING!"), e con lui l'intera discovery, in silenzio. Il caso
            # non e' teorico: `router.host` puo' essere un hostname, e il DNS
            # che lo risolve spesso e' proprio il router che non risponde.
            log.warning(f"nmap: '{x}' non e' un indirizzo, non lo si puo' escludere "
                        f"dal port scan (serve un IP o un CIDR)")
            continue
        visti.add(x)
        unici.append(x)
    return unici


def _e_indirizzo(x: str) -> bool:
    try:
        ipaddress.ip_network(x, strict=False)
        return True
    except ValueError:
        return False


def _indirizzi_router() -> list[str]:
    """Gli indirizzi del router, letti a runtime.

    Quello di `router.host` piu' quelli che il router stesso dichiara sulle
    proprie interfacce: nessun indirizzo vive nel codice e l'elenco resta giusto
    se la rete cambia. Lo snapshot si legge senza provocarne uno nuovo, perche'
    da qui si sta gia' girando dentro il ciclo del collector; se non c'e' ancora
    (primo scan, router muto) resta il solo indirizzo di configurazione: meglio
    escludere poco che tirare a indovinare.
    """
    out: list[str] = []
    host = (settings.router.host or "").strip()
    if host:
        out.append(host)
    try:
        from services.collector import get_collector
        for riga in get_collector().snapshot_ora().get("interfaces") or []:
            for ip in (riga.get("ip4") or []):
                if (ip or "").strip():
                    out.append(ip.strip())
    except Exception as e:
        log.debug(f"nmap: indirizzi del router non ancora noti ({e})")
    return out


async def _local_interfaces() -> list[tuple[str, str]]:
    """(device, ipv4) delle interfacce locali attive, cablate prima delle wireless.

    Restano fuori loopback, bridge Docker e veth perche' il loro IP non ricade in
    nessuna subnet della config: nessun nome di interfaccia hardcoded nel codice.
    """
    out = await _run_cmd(["nmap", "--iflist"], timeout=20)
    seen: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+\(\S+\)\s+(\d+\.\d+\.\d+\.\d+)/\d+\s+\S+\s+up\b", line)
        if m and m.group(1) not in seen and _subnet_of(m.group(2)):
            seen[m.group(1)] = m.group(2)
    ifaces = list(seen.items())
    ifaces.sort(key=lambda i: _is_wireless(i[0]))     # False (cablata) prima
    return ifaces


def _is_wireless(dev: str) -> bool:
    return os.path.exists(f"/sys/class/net/{dev}/wireless")


def _subnet_of(ip: str) -> str:
    """CIDR configurato che contiene l'IP, "" se nessuno."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for sn in settings.subnets:
        try:
            if addr in ipaddress.ip_network(sn.cidr, strict=False):
                return sn.cidr
        except ValueError:
            continue
    return ""


def _probe_target(registry: DeviceRegistry, local_ips: set[str]) -> str:
    """Host acceso da usare come sonda per scegliere l'interfaccia.

    Requisiti del bersaglio, in ordine di importanza:
      - non il router: risponde lui stesso alla sonda e aggiorna la sua ARP dal
        frame appena ricevuto, quindi risponde sempre sull'interfaccia da cui e'
        arrivata la richiesta. Non discrimina nulla;
      - in una subnet diversa dalle nostre, cosi' la risposta viene *inoltrata*
        dal router: e' il percorso che usano quasi tutti gli host da scoprire;
      - acceso, ovviamente.
    """
    local_nets = {_subnet_of(ip) for ip in local_ips}
    fallback = ""
    for dev in registry.all():
        if not dev.online or _is_router(dev):
            continue
        for ip in dev.ips:
            net = _subnet_of(ip)
            if ip in local_ips or not net:
                continue
            if net not in local_nets:
                return ip
            fallback = fallback or ip
    return fallback


def _is_router(dev) -> bool:
    """Il router di casa: dal tipo nel catalogo o dall'host in config."""
    return dev.type == "router" or settings.router.host in dev.ips


def _parse_nmap_greppable(text: str) -> list[dict]:
    """Parsa output greppable di nmap (-oG -). Un elemento per host.

    Con la scansione delle porte accesa nmap stampa **due** righe `Host:` per
    ogni indirizzo (una `Status:`, una `Ports:`): vanno fuse, altrimenti la
    funzione restituisce il doppio degli host che ha davvero trovato. Il registry
    a valle deduplica per IP, quindi la dashboard era giusta lo stesso, ma il
    conteggio loggato risultava doppio e ha portato a leggere come 12-14 host una
    rete che ne aveva 7.
    """
    hosts: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.startswith("Host:") or "Status: Down" in line:
            continue
        m_ip = re.search(r"Host:\s+(\S+)", line)
        if not m_ip:
            continue
        host = hosts.setdefault(m_ip.group(1), {"ip": m_ip.group(1), "ports": []})
        m_name = re.search(r"\(([^)]*)\)", line)
        if m_name and m_name.group(1):
            host["hostname"] = m_name.group(1)
        m_mac = re.search(r"MAC:\s+([0-9A-Fa-f:]{17})(?:\s+\(([^)]*)\))?", line)
        if m_mac:
            host["mac"] = m_mac.group(1)
            if m_mac.group(2):
                host["vendor"] = m_mac.group(2)
        m_os = re.search(r"OS:\s+([^\t]+)", line)
        if m_os:
            host["os"] = m_os.group(1).strip()
        for portm in re.finditer(r"(\d+)/open/tcp", line):
            porta = int(portm.group(1))
            if porta not in host["ports"]:
                host["ports"].append(porta)
    return list(hosts.values())


# ── reverse DNS ────────────────────────────────────────────────────

class ReverseDNSProvider(DiscoveryProvider):
    name = "rdns"

    async def run(self, registry: DeviceRegistry) -> None:
        if not settings.discovery.reverse_dns.enabled:
            return
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(16)

        # Le PTR degli IP non risolvibili scadono in ~5s l'una: in sequenza
        # costerebbero piu' dell'intero scan, quindi vanno in parallelo.
        async def resolve(dev):
            for ip in dev.ips:
                async with sem:
                    try:
                        name = await loop.run_in_executor(None, _ptr_lookup, ip)
                    except Exception as e:
                        # Un resolver rotto e un host senza PTR davano lo stesso
                        # risultato vuoto: il primo va detto, il secondo no.
                        log.warning(f"reverse DNS non disponibile per {ip}: {exc_text(e)}")
                        name = ""
                if name:
                    dev.hostname = name
                    dev.tag_source(self.name)
                    return

        # Si rinterroga anche chi un hostname ce l'ha gia': ora viene riportato
        # dallo scan precedente (scanner._carry_over) e senza un nuovo PTR
        # resterebbe congelato per sempre, anche dopo un rename sul DNS. Il
        # costo e' invariato: prima erano comunque tutti senza hostname.
        pending = [d for d in registry.all() if d.ips]
        await asyncio.gather(*[resolve(d) for d in pending], return_exceptions=True)


def _ptr_lookup(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""


# ── SSH facts ──────────────────────────────────────────────────────

# Le interfacce viaggiano nello STESSO comando degli altri facts: la
# connessione SSH per host c'e' gia' (una per ciclo lento), e aggiungere qui
# una riga non ne apre una seconda. `ip -j` manca su busybox e sui sistemi
# senza iproute2: li' la riga esce vuota e il dispositivo semplicemente non
# avra' interfacce, che e' la verita', non un errore.
_FACTS_CMD = (
    "echo HOST=$(hostname); "
    "echo OS=$(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\" || uname -sr); "
    "echo DOCKER=$(command -v docker >/dev/null 2>&1 && echo yes || echo no); "
    "echo SERVICES=$(systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null "
    "| awk '{print $1}' | head -n 12 | paste -sd, -); "
    "echo IFACES=$(ip -j addr show 2>/dev/null | tr -d '\\n')"
)

# Il gemello Windows: stessa uscita `CHIAVE=VALORE`, compreso `IFACES` nella
# forma di `ip -j addr`, cosi' `_parse_facts` e `_parse_ifaces` restano quelli e
# il frontend continua a disegnare un tipo solo di interfaccia.
#
# Gli adattatori si leggono **una volta sola** e si indicizzano per indice:
# chiamare Get-NetAdapter dentro il ciclo degli indirizzi raddoppiava il tempo
# del comando (4,1s contro 2,4s misurati sulla stessa macchina).
_FACTS_CMD_WIN = "\n".join([
    "$ErrorActionPreference = 'SilentlyContinue'",
    "$ProgressPreference = 'SilentlyContinue'",
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false",
    "$os = Get-CimInstance Win32_OperatingSystem",
    '"HOST=$env:COMPUTERNAME"',
    '"OS=$([string]$os.Caption) $([string]$os.Version)"',
    'if (Get-Command docker.exe) { "DOCKER=yes" } else { "DOCKER=no" }',
    '"SERVICES=$((@(Get-Service | Where-Object { $_.Status -eq \'Running\' }'
    " | Select-Object -First 12 -ExpandProperty Name)) -join ',')\"",
    "$ad = @{}",
    "foreach ($a in Get-NetAdapter) { $ad[[string]$a.InterfaceIndex] = $a }",
    "$ifs = @(Get-NetIPAddress | Group-Object -Property InterfaceIndex | ForEach-Object {",
    "  $a = $ad[[string]$_.Name]",
    "  @{",
    "    ifname = [string]$_.Group[0].InterfaceAlias",
    "    address = [string]$a.MacAddress",
    "    mtu = [int]$a.MtuSize",
    "    operstate = $(if ($a.Status -eq 'Up') { 'up' } else { 'down' })",
    "    addr_info = @($_.Group | ForEach-Object { @{ local = [string]$_.IPAddress;"
    " prefixlen = [int]$_.PrefixLength;"
    " family = $(if ($_.AddressFamily -eq 'IPv6') { 'inet6' } else { 'inet' }) } })",
    "  }",
    "})",
    '"IFACES=$(ConvertTo-Json -InputObject $ifs -Compress -Depth 5)"',
])

# Tetto al numero di interfacce per host: su un host Docker le `veth` sono una
# per container e finirebbero tutte nello snapshot, che viaggia sul WebSocket
# ad ogni ciclo. Oltre questa soglia si tiene il primo blocco e basta.
_MAX_IFACES = 40


def _parse_ifaces(raw: str) -> list[dict]:
    """Normalizza `ip -j addr show` nella stessa forma di host_inspector.

    Una forma sola per tutte le interfacce, da qualunque host arrivino: il
    frontend ne disegna un tipo solo."""
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning(f"facts SSH: 'ip -j addr' non interpretabile: {e}")
        return []
    out = []
    for entry in entries[:_MAX_IFACES]:
        if not isinstance(entry, dict):
            continue
        out.append({
            "name": entry.get("ifname", ""),
            "mac": entry.get("address", ""),
            "mtu": entry.get("mtu"),
            "state": (entry.get("operstate") or "").lower(),
            "master": entry.get("master", ""),
            "addresses": [
                {"ip": a.get("local", ""), "prefix": a.get("prefixlen"),
                 "family": a.get("family", "")}
                for a in entry.get("addr_info", []) or [] if a.get("local")
            ],
        })
    return out


class SSHFactsProvider(DiscoveryProvider):
    name = "ssh"

    async def run(self, registry: DeviceRegistry) -> None:
        # Config riletta dal file: un host aggiunto dalla UI viene interrogato
        # dal ciclo successivo, senza aspettare un riavvio.
        cfg = live_ssh_config()
        if not cfg.enabled:
            return

        async def one(host):
            dev = registry.find_by_ip(host.ip) or registry.upsert(ip=host.ip, source=self.name)
            # Un host spento costa una connect fino a scadenza: se il ping di
            # questo ciclo dice offline, non ha senso tentare.
            if cfg.skip_offline and not dev.online:
                log.debug(f"SSH facts {host.ip}: host offline, saltato")
                return
            try:
                # Tetto complessivo: connect + comando non devono trascinare lo scan.
                facts = await asyncio.wait_for(self._collect(host),
                                               timeout=cfg.connect_timeout + 20)
            except asyncio.TimeoutError:
                log.warning(f"SSH facts {host.ip}: timeout")
                return
            if facts:
                self._apply(dev, facts)
                dev.tag_source(self.name)

        # In sequenza gli host irraggiungibili sommavano i rispettivi timeout.
        await asyncio.gather(*[one(h) for h in cfg.hosts], return_exceptions=True)

    async def _collect(self, host) -> dict:
        import asyncssh
        cfg = live_ssh_config()
        kwargs = {
            "host": host.ip,
            "port": host.port,
            "username": host.user or cfg.default_user,
            "known_hosts": None,
            "connect_timeout": cfg.connect_timeout,
        }
        key = host.key or cfg.default_key
        if key:
            kwargs["client_keys"] = [key]
        elif host.password:
            kwargs["password"] = host.password
        try:
            async with asyncssh.connect(**kwargs) as conn:
                so = await risolvi_os(host.ip, conn)
                if so == "windows":
                    # Il comando Windows e' piu' lento (Get-Service e la lettura
                    # degli adattatori): 20s invece di 15, misurati a 2,4s.
                    return _parse_facts(await esegui_powershell(conn, _FACTS_CMD_WIN,
                                                                timeout=20))
                if so != "linux":
                    # risolvi_os ha gia' loggato il perche': mandare comandi a
                    # caso a un host sconosciuto riempirebbe la sua scheda di
                    # facts sbagliati, che poi restano.
                    return {}
                res = await asyncio.wait_for(conn.run(_FACTS_CMD, check=False), timeout=15)
                return _parse_facts(res.stdout or "")
        except Exception as e:
            log.warning(f"SSH facts {host.ip}: {ssh_error(e)}")
            return {}

    @staticmethod
    def _apply(dev, facts: dict):
        if facts.get("HOST") and not dev.hostname:
            dev.hostname = facts["HOST"]
        if facts.get("OS"):
            dev.os = facts["OS"]
        svcs = [s for s in facts.get("SERVICES", "").split(",") if s]
        for s in svcs:
            if s not in dev.services:
                dev.services.append(s)
        if facts.get("DOCKER") == "yes" and "docker" not in dev.services:
            dev.services.append("docker")
        ifaces = _parse_ifaces(facts.get("IFACES", ""))
        if ifaces:
            dev.interfaces = ifaces


def _parse_facts(stdout: str) -> dict:
    facts: dict = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            facts[k.strip()] = v.strip()
    return facts


# ── SNMP ───────────────────────────────────────────────────────────

class SNMPProvider(DiscoveryProvider):
    name = "snmp"

    async def run(self, registry: DeviceRegistry) -> None:
        cfg = settings.discovery.snmp
        if not cfg.enabled:
            return
        if not shutil.which("snmpget"):
            log.warning("snmpget (net-snmp) non installato: provider snmp saltato")
            return

        for ip in cfg.hosts:
            info = await self._query(ip, cfg)
            if info:
                self._apply(registry, ip, info)

    async def _query(self, ip: str, cfg) -> dict:
        # sysName.0 e sysDescr.0
        args = ["snmpget", "-v", cfg.version, "-c", cfg.community, "-Ovq",
                ip, "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.1.0"]
        out = await _run_cmd(args, timeout=10)
        lines = [l.strip().strip('"') for l in out.splitlines() if l.strip()]
        if not lines:
            return {}
        return {"name": lines[0] if lines else "",
                "descr": lines[1] if len(lines) > 1 else ""}

    @staticmethod
    def _apply(registry: DeviceRegistry, ip: str, info: dict):
        dev = registry.find_by_ip(ip) or registry.upsert(ip=ip, source="snmp")
        if info.get("name") and not dev.hostname:
            dev.hostname = info["name"]
        if info.get("descr") and not dev.os_guess:
            dev.os_guess = info["descr"][:80]
        dev.tag_source("snmp")


# ── Helper ─────────────────────────────────────────────────────────

async def _run_cmd(args: list[str], timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except Exception as e:
        log.error(f"cmd error {' '.join(args[:2])}: {e}")
        return ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        # Un nmap lasciato andare continua a scansionare la rete per conto suo
        # e occupa un pid del container fino alla fine dei suoi giorni.
        log.error(f"timeout: {' '.join(args[:2])}")
        await terminate_process(proc)
        return ""
    except Exception as e:
        log.error(f"cmd error {' '.join(args[:2])}: {e}")
        return ""


def build_providers() -> list[DiscoveryProvider]:
    """Costruisce la lista dei provider abilitati (ordine = ordine di esecuzione).

    Lo scanner li divide per `stage`: prima i "fast", poi i "slow".
    """
    providers: list[DiscoveryProvider] = []
    if settings.discovery.ping_sweep.enabled:
        providers.append(PingSweepProvider())
    if settings.discovery.nmap.enabled:
        providers.append(NmapProvider())
    if settings.discovery.reverse_dns.enabled:
        providers.append(ReverseDNSProvider())
    if settings.discovery.ssh.enabled:
        providers.append(SSHFactsProvider())
    if settings.discovery.snmp.enabled:
        providers.append(SNMPProvider())
    return providers
