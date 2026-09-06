"""
services/collector.py — Background data collector
==================================================
Raccoglie dati da tutte le sorgenti a intervalli (fast/slow), mantiene
uno snapshot in memoria per servire le API velocemente, e notifica i
client WebSocket ad ogni aggiornamento.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional

from config import settings
from middleware.auth import security_warnings
from services.alerts import get_alerts
from services.alerts import summary as alerts_summary
from services.host_inspector import get_host_network
from services.openwrt import get_luci
from services.wireguard import get_wireguard_service
from services.docker_client import get_docker_manager
from services.errors import exc_text
from services.history_store import get_history
from services.host_metrics import get_host_metrics
from services.scanner import get_scanner
from services.services_overview import build_services_overview

log = logging.getLogger("collector")

_MB = 1_048_576


class DataCollector:

    def __init__(self):
        self._snapshot: dict[str, Any] = {}
        self._last_slow: float = 0
        # La raccolta lenta gira in un task a parte: se restasse inline
        # bloccherebbe anche gli update fast per tutta la durata dello scan.
        self._slow_task: Optional[asyncio.Task] = None
        # Ultima lettura dei contatori WAN, per derivare i bit/s.
        self._last_wan: Optional[dict] = None
        # Serializza la raccolta iniziale: piu' client WS che si connettono
        # prima che lo snapshot sia popolato non devono innescare raccolte
        # concorrenti (SSH multipli, scritture concorrenti sul dict).
        self._collect_lock = asyncio.Lock()
        # Serializza gli aggiornamenti manuali della sezione monitoraggio:
        # due click ravvicinati aprirebbero due giri di SSH verso gli stessi host.
        self._monitor_lock = asyncio.Lock()
        # Impostata da main.py -> broadcast WebSocket
        self.on_update: Optional[Callable[[dict], Coroutine]] = None

    async def get_snapshot(self) -> dict:
        if not self._snapshot:
            async with self._collect_lock:
                if not self._snapshot:      # ricontrolla: un altro ha gia' raccolto
                    await self._collect_fast()
                    # La raccolta lenta parte in background: il primo client non
                    # aspetta lo scan della rete, i dati arrivano via WebSocket.
                    self._start_slow()
                    self._snapshot["collector"] = self._collector_state()
        return self._snapshot

    def dimentica_servizio(self, kind: str, ident: str) -> bool:
        """Toglie subito un servizio cancellato dallo snapshot.

        Serve perche' `services` si ricalcola **solo nel giro lento** (60s),
        mentre il giro veloce ribroadcasta lo snapshot ogni 10s: senza questa
        potatura il client toglieva la riga, riceveva un attimo dopo lo snapshot
        ancora vecchio e **la riga tornava** — difetto segnalato dal proprietario
        il 2026-09-04. Ricalcolare qui, invece, vorrebbe dire fare SSH e probe
        dentro la richiesta di cancellazione.

        Di una rimozione si sa esattamente cosa sparisce, quindi non si inventa
        niente: e' l'unica operazione del catalogo che si puo' applicare allo
        snapshot senza interrogare nulla (aggiunta e modifica no, di un servizio
        nuovo non si conosce lo stato).

        Ritorna True se ha tolto qualcosa. Non solleva mai su uno snapshot
        vuoto: la pagina puo' essere aperta prima del primo giro lento, e li'
        non c'e' semplicemente niente da togliere.
        """
        svc = self._snapshot.get("services") or {}
        if not svc:
            return False
        if kind == "docker":
            # Il container resta: e' scoperto da solo, non e' il catalogo a
            # farlo esistere. Sparisce il pin, cioe' etichetta, URL e la scelta
            # di mostrarlo in dashboard. I conteggi non cambiano.
            for c in (svc.get("docker") or {}).get("containers") or []:
                if c.get("name") == ident:
                    c["pinned"] = False
                    c["dashboard"] = False
                    c["url"] = ""
                    c["label"] = c.get("name") or ""
                    return True
            return False

        chiave = {"systemd": "systemd", "windows_service": "windows_services",
                  "http": "healthchecks"}.get(kind)
        lista = svc.get(chiave) if chiave else None
        if not lista:
            return False
        # `name` e' il campo che tutte e tre le liste espongono (systemd_monitor,
        # windows_services, healthcheck): per systemd e' la unit.
        resta = [x for x in lista if x.get("name") != ident]
        if len(resta) == len(lista):
            return False
        tolto_ok = any(x.get("ok") for x in lista if x.get("name") == ident)
        svc[chiave] = resta
        sommario = svc.get("summary")
        if sommario:
            sommario["total"] = max(0, sommario.get("total", 0) - 1)
            campo = "ok" if tolto_ok else "down"
            sommario[campo] = max(0, sommario.get(campo, 0) - 1)
        return True

    def snapshot_ora(self) -> dict:
        """Lo snapshot com'e' adesso, senza farne uno nuovo.

        `get_snapshot()` raccoglie quando e' vuoto, e chi la chiamasse mentre
        una raccolta e' gia' in corso (la discovery gira dentro il ciclo lento)
        rientrerebbe nel collector. Qui si legge e basta: un dizionario vuoto e'
        una risposta valida, e chi legge deve saperci fare.
        """
        return self._snapshot

    def _collector_state(self) -> dict:
        """Cadenza della raccolta, per il conto alla rovescia in dashboard.

        Va nello snapshot (non hardcodata nel frontend) perche' gli intervalli
        si cambiano dalle Impostazioni: la UI deve dire quando arriva davvero il
        prossimo giro, non una cadenza presunta."""
        slow = settings.collect_interval_slow
        return {
            "fast_interval": settings.collect_interval_fast,
            "slow_interval": slow,
            "last_slow_ts": int(self._last_slow),
            "next_slow_ts": int(self._last_slow + slow),
            # Con la raccolta ancora in corso il prossimo giro non parte: la UI
            # dice "in corso" invece di mostrare un countdown gia' scaduto.
            "slow_running": bool(self._slow_task and not self._slow_task.done()),
        }

    async def refresh_monitor(self) -> dict:
        """Aggiorna subito la sezione monitoraggio (Docker, servizi, risorse).

        Non tocca la discovery dei dispositivi: quella ha il suo pulsante e uno
        scan nmap qui farebbe attendere decine di secondi un comando che deve
        rispondere. Le risorse si raccolgono ignorando il loro intervallo: e'
        una richiesta esplicita, non il ciclo automatico."""
        if self._slow_task and not self._slow_task.done():
            return {"status": "busy", "reason": "raccolta lenta in corso"}
        if self._monitor_lock.locked():
            return {"status": "busy", "reason": "aggiornamento gia' in corso"}
        async with self._monitor_lock:
            await self._collect_docker_and_services()
            await self._collect_host_resources(force=True)
            self._aggiorna_alert()
            now = int(time.time())
            self._snapshot["_ts"] = now
            if self.on_update:
                await self.on_update({"type": "update", "data": self._snapshot, "ts": now})
        return {"status": "ok", "ts": now}

    # ── Stato delle sorgenti ───────────────────────────────────────

    def _sorgente_ok(self, nome: str) -> None:
        self._snapshot.setdefault("sources", {})[nome] = {
            "ok": True, "ts": int(time.time()),
        }

    def _sorgente_ko(self, nome: str, e: BaseException) -> None:
        """Una sorgente giu' e' un dato, non solo una riga di log.

        Prima il fallimento finiva solo in `log.error` e la sezione restava
        quella del giro precedente: la dashboard mostrava numeri vecchi come
        se fossero freschi, e nessuno poteva accorgersene dalla UI. `ts` resta
        l'ora dell'ultima raccolta **riuscita**, `since` dice da quando dura
        il guasto."""
        prec = self._snapshot.setdefault("sources", {}).get(nome, {})
        self._snapshot["sources"][nome] = {
            "ok": False,
            "ts": prec.get("ts"),
            "error": exc_text(e),
            "since": prec.get("since") or int(time.time()),
            "fails": (prec.get("fails") or 0) + 1,
        }

    # ── Alert ──────────────────────────────────────────────────────

    def _aggiorna_alert(self) -> None:
        """Rivaluta le regole sullo snapshot appena raccolto.

        Sta qui e non nei router perche' gli alert devono esistere anche mentre
        nessuno guarda la pagina che li possiede: il contatore in sidebar e
        l'indice in dashboard leggono lo stesso snapshot di tutto il resto.
        """
        try:
            # Rivalutata ad ogni ciclo, non solo all'avvio: la password admin si
            # imposta dalla UI mentre il servizio gira, e il banner deve
            # spegnersi da solo quando la falla e' chiusa.
            self._snapshot["security"] = security_warnings()
        except Exception as e:
            log.error(f"avvisi di sicurezza: {exc_text(e)}")
        try:
            alerts = get_alerts().build(self._snapshot)
            self._snapshot["alerts"] = alerts
            self._snapshot["alerts_summary"] = alerts_summary(alerts)
        except Exception as e:
            log.error(f"alert: {exc_text(e)}")

    # ── Routine di raccolta ────────────────────────────────────────

    async def _collect_system(self):
        try:
            info = await get_luci().get_system_info()
            self._snapshot["system"] = {
                "hostname": info.hostname,
                "model": info.model,
                "os_version": info.os_version,
                "uptime_seconds": info.uptime_seconds,
                "uptime_human": _fmt_uptime(info.uptime_seconds),
                "load": info.load,
                "memory_total_mb": round(info.memory_total / _MB, 1),
                "memory_free_mb": round(info.memory_free / _MB, 1),
                "memory_used_pct": round((1 - info.memory_free / max(info.memory_total, 1)) * 100, 1),
                "ts": int(time.time()),
            }
            self._sorgente_ok("system")
        except Exception as e:
            log.error(f"collect_system: {exc_text(e)}")
            self._sorgente_ko("system", e)

    async def _collect_interfaces(self):
        try:
            ifaces = await get_luci().get_interfaces()
            rows = [
                {
                    "name": i.name, "ifname": i.ifname, "up": i.up, "ip4": i.ip4,
                    "rx_bytes": i.rx_bytes, "tx_bytes": i.tx_bytes,
                    "rx_mb": round(i.rx_bytes / _MB, 2), "tx_mb": round(i.tx_bytes / _MB, 2),
                }
                for i in ifaces
            ]
            _mark_shared_devices(rows)
            self._snapshot["interfaces"] = rows
            self._sorgente_ok("interfaces")
        except Exception as e:
            log.error(f"collect_interfaces: {exc_text(e)}")
            self._sorgente_ko("interfaces", e)

    async def publish_devices(self, devices: list) -> None:
        """Scrive i device nello snapshot e notifica subito i client.

        Chiamata dallo scanner a fine stage rapido e a fine arricchimento: un
        device nuovo compare in UI senza attendere il ciclo slow successivo.
        Vale anche per lo scan manuale lanciato dalla pagina Dispositivi.
        """
        self._store_devices(devices)
        if self.on_update:
            await self.on_update({"type": "update", "data": self._snapshot,
                                  "ts": int(time.time())})

    def _store_devices(self, devices: list):
        self._snapshot["devices"] = [d.to_dict() for d in devices]
        # Presenza storica: passa da qui perche' e' l'unico punto attraversato
        # sia dal ciclo lento sia dallo scan manuale. La scrittura e' sincrona
        # ma tocca il disco solo quando qualcosa e' davvero cambiato.
        try:
            get_history().mark_devices(self._snapshot["devices"])
        except Exception as e:
            log.error(f"storico presenza device: {exc_text(e)}")
        self._snapshot["devices_summary"] = {
            "total": len(devices),
            "online": sum(1 for d in devices if d.online),
            "offline": sum(1 for d in devices if not d.online),
            "ts": int(time.time()),
        }

    async def _collect_devices(self):
        try:
            self._store_devices(await get_scanner().scan())
            self._sorgente_ok("devices")
        except Exception as e:
            log.error(f"collect_devices: {exc_text(e)}")
            self._sorgente_ko("devices", e)

    async def _collect_host_network(self):
        """Interfacce e rotte dell'host che ospita LANMng.

        Nel ciclo lento e non solo su richiesta della pagina Host: senza il dato
        nello snapshot le regole sulla rete dell'host esisterebbero solo mentre
        qualcuno guarda quella pagina, quindi niente contatore in sidebar e
        niente indice in dashboard. Costa due comandi locali (`ip -j addr`,
        `ip -j route`) ogni giro: nessun SSH, nessuna rete.
        """
        try:
            self._snapshot["host_network"] = await get_host_network()
            self._sorgente_ok("host_network")
        except Exception as e:
            log.error(f"collect_host_network: {exc_text(e)}")
            self._sorgente_ko("host_network", e)

    async def _collect_wireguard(self):
        try:
            ifaces = await get_wireguard_service().get_status()
            self._snapshot["wireguard"] = {
                "interfaces": [i.to_dict() for i in ifaces],
                "total_peers": sum(i.total_peers for i in ifaces),
                "active_peers": sum(i.active_peers for i in ifaces),
                "relay": settings.wireguard.relay_host,
                "ts": int(time.time()),
            }
            self._sorgente_ok("wireguard")
        except Exception as e:
            log.error(f"collect_wireguard: {exc_text(e)}")
            self._sorgente_ko("wireguard", e)

    async def _collect_docker(self) -> Optional[list]:
        """Aggiorna la sezione docker e ritorna i container raccolti, cosi' la
        vista servizi li riusa invece di interrogare gli host una seconda volta.
        Ritorna None se la raccolta e' fallita (il chiamante ritentera' da se')."""
        try:
            mgr = get_docker_manager()
            containers = await mgr.list_containers()   # multi-host, stats gia' incluse
            networks = await mgr.list_networks()
            self._snapshot["docker"] = {
                "containers": [c.to_dict() for c in containers],
                "networks": [n.to_dict() for n in networks],
                "hosts": mgr.hosts(),
                "summary": {
                    "running": sum(1 for c in containers if c.is_running),
                    "stopped": sum(1 for c in containers if not c.is_running),
                    "total": len(containers),
                },
                "ts": int(time.time()),
            }
            self._sorgente_ok("docker")
            return containers
        except Exception as e:
            log.error(f"collect_docker: {exc_text(e)}")
            self._sorgente_ko("docker", e)
            return None

    async def _collect_services(self, containers: Optional[list] = None):
        try:
            self._snapshot["services"] = await build_services_overview(containers)
            await asyncio.to_thread(get_history().mark_services,
                                    self._snapshot["services"])
            self._sorgente_ok("services")
        except Exception as e:
            log.error(f"collect_services: {exc_text(e)}")
            self._sorgente_ko("services", e)

    async def _collect_docker_and_services(self):
        """In sequenza, non in parallelo: la vista servizi riusa i container
        appena raccolti. Interrogare Docker due volte per ciclo raddoppiava le
        connessioni SSH verso ogni host remoto (e i relativi timeout)."""
        await self._collect_services(await self._collect_docker())

    async def _collect_host_resources(self, force: bool = False):
        """Risorse (CPU/RAM/dischi/temperature) degli host della LAN via SSH.

        Ha un intervallo proprio, indipendente dal ciclo lento: e' anche la
        finestra su cui si media la CPU, quindi accorciarla per sbaglio
        renderebbe la misura piu' nervosa senza renderla piu' vera. `force`
        salta l'intervallo per l'aggiornamento chiesto a mano dalla UI.
        """
        metrics = get_host_metrics()
        if not force and not metrics.due():
            return
        try:
            # Gli host gia' visti spenti da questo ciclo si saltano: la connect
            # verso una macchina spenta costa il timeout intero, per nulla.
            online = {ip for d in self._snapshot.get("devices", [])
                      if d.get("online") for ip in (d.get("ips") or [])}
            self._snapshot["resources"] = await metrics.collect(online or None)
            await self._storicizza_risorse(self._snapshot["resources"])
            self._sorgente_ok("resources")
        except Exception as e:
            log.error(f"collect_host_resources: {exc_text(e)}")
            self._sorgente_ko("resources", e)

    async def _storicizza_risorse(self, risorse: dict):
        """Manda allo storico l'ultimo punto di ogni host raggiungibile.

        Si legge da qui e non da `host_metrics._push_series` di proposito: il
        modulo delle risorse resta ignaro di dove finiscono i suoi dati, e il
        punto e' esattamente lo stesso che vede la UI, non una seconda versione
        da tenere allineata a mano.
        """
        punti = {}
        for host in (risorse or {}).get("hosts") or []:
            serie = host.get("series") or []
            if host.get("reachable") and serie:
                punti[host.get("host")] = serie[-1]
        if punti:
            await asyncio.to_thread(get_history().add_host_points, punti)

    async def _collect_traffic_point(self):
        """Aggiunge un punto alla serie temporale del traffico WAN.

        I contatori dell'interfaccia sono cumulativi dall'avvio: da soli
        disegnano solo una riga che sale. Quello che interessa e' la velocita',
        quindi qui si derivano i bit/s sull'intervallo fra due letture.
        """
        try:
            ifaces = self._snapshot.get("interfaces", [])
            wan = _pick_wan_interface(ifaces)
            # Nome dell'interfaccia di uplink risolta: il frontend la usa per
            # mostrare la WAN senza indovinare nomi (niente hardcoded lato UI).
            self._snapshot["wan_interface"] = wan.get("name") if wan else None
            rx_bps, tx_bps = self._wan_rates(wan)
            point = {
                "t": int(time.time() * 1000),
                # bit/s sull'intervallo; null quando la velocita' non e'
                # calcolabile (prima lettura, cambio interfaccia, reboot router)
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                # contatori cumulativi, in MB: utili come totale, non come serie
                "rx_total_mb": round(wan["rx_mb"], 2) if wan else 0,
                "tx_total_mb": round(wan["tx_mb"], 2) if wan else 0,
                "latency": await _measure_latency(),
            }
            series = self._snapshot.setdefault("traffic_series", [])
            series.append(point)
            if len(series) > 120:
                series.pop(0)
            # La lista qui sopra e' solo la finestra "viva": vive in memoria e
            # si azzera ad ogni riavvio. Lo storico su disco e' quello che
            # sopravvive. In un thread perche' sqlite e' sincrono e una fsync
            # sull'event loop fermerebbe anche i WebSocket.
            await asyncio.to_thread(get_history().add_traffic, point)
            self._sorgente_ok("traffic")
        except Exception as e:
            log.error(f"collect_traffic: {exc_text(e)}")
            self._sorgente_ko("traffic", e)

    def _wan_rates(self, wan: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
        """Velocita' in bit/s fra questa lettura e la precedente.

        Ritorna (None, None) quando il dato non e' confrontabile: prima lettura,
        interfaccia di uplink cambiata o contatori azzerati (riavvio del router).
        In quel caso il grafico mostra un buco, non uno zero inventato.
        """
        prev, self._last_wan = self._last_wan, None
        if not wan:
            return None, None
        now = time.monotonic()
        rx, tx = wan.get("rx_bytes", 0), wan.get("tx_bytes", 0)
        self._last_wan = {"name": wan.get("name"), "rx": rx, "tx": tx, "t": now}
        if not prev or prev["name"] != wan.get("name"):
            return None, None
        elapsed = now - prev["t"]
        if elapsed <= 0 or rx < prev["rx"] or tx < prev["tx"]:
            return None, None
        return (round((rx - prev["rx"]) * 8 / elapsed, 1),
                round((tx - prev["tx"]) * 8 / elapsed, 1))

    async def _collect_fast(self):
        # meta statico (nomi/colori subnet, nome router) per la UI: nessun hardcoded lato frontend.
        self._snapshot["meta"] = _build_meta()
        await asyncio.gather(
            self._collect_system(),
            self._collect_interfaces(),
            self._collect_wireguard(),
            return_exceptions=True,
        )
        await self._collect_traffic_point()
        await self._archivia_log()
        self._aggiorna_alert()

    async def _archivia_log(self):
        """Travasa nel database le righe di log e di audit accumulate.

        Sta qui, sul ciclo veloce, e non dentro l'handler di logging: se ogni
        `log.info()` scrivesse su sqlite, un disco lento diventerebbe un freno
        su qualunque punto del backend. Il syslog del router non passa da qui:
        non si archivia (il router ha poche risorse ed e' gia' lui a tenerlo).
        """
        if not settings.logs.persist:
            return
        try:
            from services.audit import coda_da_archiviare
            from services.log_buffer import get_log_buffer
            from services.log_sources import parse_audit_line

            righe = get_log_buffer().drain()
            audit = [parse_audit_line(l) for l in coda_da_archiviare()]
            if righe:
                await asyncio.to_thread(get_history().add_log_lines, "backend", righe)
            if audit:
                await asyncio.to_thread(get_history().add_log_lines, "audit", audit)
        except Exception as e:
            # L'archiviazione dei log non deve poter fermare il monitoraggio:
            # stessa regola dello storico delle serie.
            log.error(f"archiviazione log: {exc_text(e)}")

    async def _collect_slow(self):
        # In sequenza, non in parallelo. La CPU dei container e' un campione di
        # un secondo: prendendolo mentre gira lo scan (centinaia di ping in
        # parallelo piu' nmap) LANMng misurava sempre il proprio picco e si
        # dichiarava al 100% di una CPU, quando a riposo sta sotto l'1%.
        await self._collect_host_network()
        await self._collect_devices()
        await self._collect_docker_and_services()
        # Ultima della fila, sempre in sequenza: apre una connessione SSH per
        # host e sommarla allo scan significherebbe misurare (e causare) picchi.
        await self._collect_host_resources()
        await self._compatta_storico()

    async def _compatta_storico(self):
        """Aggrega i punti vecchi e cancella quelli oltre la finestra.

        Si appoggia al ciclo lento invece di avere un task suo: il lavoro vero
        gira comunque al massimo una volta l'ora (`due()`), e un secondo task
        di background sarebbe una cosa in piu' da avviare, fermare e ricordarsi.
        """
        try:
            storico = get_history()
            if storico.due():
                await asyncio.to_thread(storico.compact)
        except Exception as e:
            log.error(f"compattazione storico: {e}")

    # ── Loop principale ────────────────────────────────────────────

    def _start_slow(self):
        """Avvia la raccolta lenta in background, se non ne gira gia' una."""
        if self._slow_task and not self._slow_task.done():
            log.debug("Raccolta lenta ancora in corso: ciclo saltato")
            return
        self._last_slow = time.time()
        self._slow_task = asyncio.create_task(self._collect_slow())
        self._slow_task.add_done_callback(_log_task_error)

    async def run(self):
        log.info(f"Collector started: fast={settings.collect_interval_fast}s "
                 f"slow={settings.collect_interval_slow}s "
                 f"(debug={settings.debug})")
        # Lo scanner pubblica da solo i risultati (anche quelli parziali e quelli
        # degli scan manuali): senza questo aggancio la UI li vedrebbe solo al
        # ciclo slow successivo.
        get_scanner().on_result = self.publish_devices
        while True:
            now = time.time()
            try:
                await self._collect_fast()
                if now - self._last_slow >= settings.collect_interval_slow:
                    self._start_slow()
                self._snapshot["_ts"] = int(now)
                # Dopo l'eventuale avvio della raccolta lenta: cosi' il
                # countdown pubblicato e' quello del giro appena programmato.
                self._snapshot["collector"] = self._collector_state()
                if self.on_update:
                    await self.on_update({"type": "update", "data": self._snapshot, "ts": int(now)})
            except Exception as e:
                log.error(f"Collector loop error: {e}")
            await asyncio.sleep(settings.collect_interval_fast)


# ── Helpers ────────────────────────────────────────────────────────

def _log_task_error(task: asyncio.Task):
    """Un task in background che muore non deve farlo in silenzio."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error(f"Raccolta lenta fallita: {exc}")


def _mark_shared_devices(rows: list[dict]) -> None:
    """Segna le reti logiche che condividono lo stesso dispositivo di rete.

    I contatori rx/tx sono del **dispositivo**, non della singola rete: qui piu'
    reti (lan, lanPC2, lanVM) stanno sullo stesso bridge, quindi riportavano tutte
    e tre gli stessi byte, dando l'impressione di tre misure separate che non
    tornavano mai. Il kernel non conta il traffico per subnet, quindi non c'e'
    modo di separarle: si dichiara la condivisione e si attribuiscono i byte a
    una riga sola (`counters_own`), invece di ripeterli.
    """
    per_device: dict[str, list[dict]] = {}
    for row in rows:
        if row["ifname"]:
            per_device.setdefault(row["ifname"], []).append(row)
    for device, group in per_device.items():
        altri = [r["name"] for r in group]
        for i, row in enumerate(group):
            # I byte restano su tutte le righe (servono ai calcoli), ma solo la
            # prima li rivendica come propri: la UI mostra le altre come rimando.
            row["counters_own"] = (i == 0)
            row["shared_with"] = [n for n in altri if n != row["name"]] if len(group) > 1 else []


def _pick_wan_interface(ifaces: list[dict]) -> Optional[dict]:
    """Sceglie l'interfaccia di uplink per la serie traffico:
    1. quella indicata in config (router.wan_interface), se presente;
    2. altrimenti il primo candidato noto (wan/tethering/usb0/wwan);
    3. altrimenti la prima interfaccia con traffico in ingresso."""
    if not ifaces:
        return None
    configured = settings.router.wan_interface
    if configured:
        match = next((i for i in ifaces if i.get("name") == configured), None)
        if match:
            return match
    candidates = settings.router.wan_candidates
    match = next((i for i in ifaces if i.get("name") in candidates), None)
    if match:
        return match
    return next((i for i in ifaces if i.get("rx_bytes", 0) > 0), None)


def _build_meta() -> dict:
    """Metadati statici di configurazione per la UI (niente valori hardcoded
    nel frontend): nome del router e subnet con label/colore."""
    return {
        "router_name": settings.router.name or settings.router.host or "router",
        # Bersaglio del ping di latenza: la pagina Stats scrive "verso X" e
        # non deve indovinarlo ne' tenerselo scritto nel codice.
        "internet_probe": settings.router.internet_probe,
        # Nomi che valgono come uplink: quando la WAN non viene riconosciuta la
        # pagina deve poter dire QUALI nomi cerca, senza tenerseli scritti nel
        # codice del frontend.
        "wan_candidates": list(settings.router.wan_candidates or []),
        "subnets": [{"cidr": s.cidr, "label": s.label, "color": s.color}
                    for s in settings.subnets],
        # Lingua predefinita del servizio: la SPA la applica solo a chi non ne
        # ha gia' scelta una nel proprio browser.
        "default_language": settings.ui.default_language or "",
    }


def _fmt_uptime(seconds: int) -> str:
    d, h, m = seconds // 86400, (seconds % 86400) // 3600, (seconds % 3600) // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


async def _measure_latency() -> Optional[float]:
    """Latenza internet (verso il probe configurato) misurata localmente dal
    backend: non dipende dalla raggiungibilita' del router.

    `None` e non 0.0 quando il probe non risponde. Zero significa "latenza
    perfetta" e sul grafico e' un tuffo verso il basso: il contrario di quello
    che e' successo davvero. Il buco resta un buco, come per i contatori del
    traffico (vedi `_wan_rates`) e come lo storico si aspetta (add_traffic
    conserva i None apposta)."""
    from services.ping import ping_host
    online, latency = await ping_host(settings.router.internet_probe, count=1)
    return latency if online else None


# Istanza unica: la usano main.py (loop + WebSocket) e i router che devono
# chiedere un aggiornamento immediato, senza importare main (dipendenza ciclica).
_collector: Optional[DataCollector] = None


def get_collector() -> DataCollector:
    global _collector
    if _collector is None:
        _collector = DataCollector()
    return _collector
