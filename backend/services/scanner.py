"""
services/scanner.py — Device discovery orchestrator
===================================================
Coordina la discovery dei dispositivi in due stadi, per non far attendere
la UI il completamento delle sorgenti lente:

  Stage RAPIDO (pochi secondi) — pubblicato subito
    1. Catalogo manuale (devices.yaml): device sempre visibili, anche spenti
    2. DHCP leases + tabella ARP dal router
    3. Provider "fast" (ping sweep delle subnet)
    4. Ping per stato online/offline

  Stage LENTO — pubblicato a fine ciclo
    5. Provider di arricchimento (nmap, reverse-DNS, SSH facts, SNMP)

Il risultato e' una lista Device deduplicata (DeviceRegistry).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Callable, Coroutine, Optional

from config import settings
from services.device_store import get_device_store
from services.discovery import build_providers
from services.openwrt import get_luci, get_ssh
from services.ping import ping_host
from services.registry import Device, DeviceRegistry   # re-export Device

__all__ = ["Device", "DeviceScanner", "get_scanner"]

log = logging.getLogger("scanner")


def _subnet_label(ips: list[str]) -> str:
    """Etichetta della subnet (da config.subnets) per il primo IP che combacia."""
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        for sn in settings.subnets:
            try:
                if addr in ipaddress.ip_network(sn.cidr, strict=False):
                    return sn.label
            except ValueError:
                continue
    return ""


class DeviceScanner:

    def __init__(self):
        self.luci = get_luci()
        self.ssh = get_ssh()
        self.store = get_device_store()
        self._registry = DeviceRegistry()
        self._providers = build_providers()
        # Uno scan alla volta: scan manuale e ciclo del collector sovrapposti
        # raddoppiavano nmap/SSH allungando entrambi.
        self._lock = asyncio.Lock()
        # Impostata dal collector: pubblica il risultato appena disponibile
        # (una volta a fine stage rapido, una a fine arricchimento).
        self.on_result: Optional[Callable[[list[Device]], Coroutine]] = None

    def reload_catalog(self):
        self.store.reload()

    # ── Seed dal catalogo manuale ──────────────────────────────────

    def _seed_catalog(self, reg: DeviceRegistry):
        """Inserisce nel registry i dispositivi del catalogo (anche quelli
        aggiunti a mano e attualmente spenti): cosi' restano sempre visibili."""
        for e in self.store.entries():
            mac, ip = e.get("mac") or "", e.get("ip") or ""
            if mac or ip:
                reg.upsert(mac=mac, ip=ip, source="catalogo")

    # ── Sorgenti base (router) ─────────────────────────────────────

    async def _collect_dhcp(self, reg: DeviceRegistry):
        try:
            for lease in await self.luci.get_dhcp_leases():
                reg.upsert(mac=lease.mac, ip=lease.ip, hostname=lease.hostname, source="dhcp")
        except Exception as e:
            log.error(f"DHCP collect error: {e}")

    async def _collect_arp(self, reg: DeviceRegistry):
        try:
            for entry in await self.ssh.get_arp_table():
                reg.upsert(mac=entry.mac, ip=entry.ip, source="arp")
        except Exception as e:
            log.error(f"ARP collect error: {e}")

    # ── Stato online/offline ───────────────────────────────────────

    async def _ping_device(self, dev: Device):
        # Ping locale dal backend (host-net): lo stato online NON dipende dal router.
        for ip in dev.ips:
            online, latency = await ping_host(ip)
            if online:
                dev.online = True
                dev.latency_ms = latency
                dev.last_seen = int(time.time())
                return
        dev.online = False
        dev.latency_ms = 0.0

    async def _ping_all(self, reg: DeviceRegistry):
        sem = asyncio.Semaphore(64)

        async def _bounded(dev: Device):
            async with sem:
                await self._ping_device(dev)

        # Chi ha gia' risposto allo sweep e' online: ripingarlo e' tempo sprecato.
        pending = [d for d in reg.all() if not d.online]
        await asyncio.gather(*[_bounded(d) for d in pending], return_exceptions=True)

    # ── Catalogo + subnet ──────────────────────────────────────────

    def _apply_catalog(self, reg: DeviceRegistry):
        by_mac = self.store.catalog_by_mac()
        by_ip = self.store.catalog_by_ip()
        hidden = self.store.hidden_set()
        for dev in reg.all():
            # Match per indirizzo: il catalogo da' un nome diverso a ciascun IP
            # (es. "host" per la cablata e "host-w" per la wireless). Chi ha un IP
            # si riconosce SOLO dall'IP: il MAC osservato puo' essere quello
            # della scheda dell'host che fa da bridge, e prenderne il nome
            # ribattezzava una VM nuova col nome dell'host (Kali su .2.11).
            # Il MAC identifica solo i device che un indirizzo non ce l'hanno.
            entry = None
            for ip in dev.ips:
                if ip in by_ip:
                    entry = by_ip[ip]; break
            if entry is None and dev.mac and not dev.ips:
                entry = by_mac.get(dev.mac.upper())
            if entry:
                dev.merge_catalog(entry)
            # Nascosto: per IP se ne ha uno, altrimenti per MAC. Stesso motivo
            # di sopra: nascondere una entry solo-MAC non deve far sparire le
            # macchine che quel MAC lo condividono.
            dev.hidden = (any(ip in hidden for ip in dev.ips) if dev.ips
                          else dev.mac.upper() in hidden)
            # subnet/label dalla config (nessuna mappa hardcoded)
            dev.subnet = _subnet_label(dev.ips)

    # ── Memoria fra uno scan e l'altro ─────────────────────────────

    # Dati che una volta scoperti restano validi anche se la sorgente che li ha
    # prodotti non risponde in questo giro (il PTR di un host spento, per dire).
    _RIPORTATI = ("hostname", "vendor", "os_guess")

    def _carry_over(self, reg: DeviceRegistry):
        """Riporta sul nuovo registro l'arricchimento gia' noto per quell'IP.

        Ogni scan riparte da un registro vuoto: senza questo, appena finisce lo
        stage rapido un dispositivo non presente in catalogo torna a chiamarsi
        con l'IP nudo, e riprende il suo nome solo 20-30s dopo, a stage lento
        finito. Il nome di un device nuovo lampeggiava a ogni ciclo."""
        for dev in reg.all():
            precedente = next((self._registry.find_by_ip(ip) for ip in dev.ips
                               if self._registry.find_by_ip(ip)), None)
            if precedente is None:
                continue
            for campo in self._RIPORTATI:
                if not getattr(dev, campo) and getattr(precedente, campo):
                    setattr(dev, campo, getattr(precedente, campo))
            # Le porte aperte le ritrova solo nmap, e solo se abilitato: tenerle
            # evita che la scheda dettagli si svuoti fra uno scan e l'altro.
            if not dev.open_ports and precedente.open_ports:
                dev.open_ports = list(precedente.open_ports)

    # ── Ciclo completo ─────────────────────────────────────────────

    async def scan(self) -> list[Device]:
        """Scan completo. Se uno scan e' gia' in corso attende quello, invece di
        avviarne un secondo in parallelo."""
        if self._lock.locked():
            log.info("Scan gia' in corso: attendo il risultato")
            async with self._lock:
                return self.get_cached()
        async with self._lock:
            return await self._scan()

    async def _scan(self) -> list[Device]:
        started = time.monotonic()
        log.info("Device scan started")
        reg = DeviceRegistry()
        fast = [p for p in self._providers if p.stage == "fast"]
        slow = [p for p in self._providers if p.stage != "fast"]

        # ── Stage rapido: catalogo + router (DHCP/ARP) + provider fast ──
        self._seed_catalog(reg)
        await asyncio.gather(
            self._collect_dhcp(reg),
            self._collect_arp(reg),
            *[self._run_provider(p, reg) for p in fast],
        )
        self._carry_over(reg)
        self._apply_catalog(reg)
        await self._ping_all(reg)
        self._registry = reg
        log.info(f"Scan fast done in {time.monotonic() - started:.1f}s: "
                 f"{len(reg.all())} devices, {sum(1 for d in reg.all() if d.online)} online")
        # Pubblica subito: un IP nuovo compare in UI senza attendere l'arricchimento.
        await self._publish(reg)

        # ── Stage lento: arricchimento (nmap/rdns/ssh/snmp) ─────────────
        for provider in slow:
            await self._run_provider(provider, reg)
        self._apply_catalog(reg)
        self._registry = reg
        result = reg.all()
        log.info(f"Scan done in {time.monotonic() - started:.1f}s: {len(result)} devices, "
                 f"{sum(1 for d in result if d.online)} online "
                 f"(providers: {', '.join(p.name for p in self._providers) or 'none'})")
        await self._publish(reg)
        return result

    async def _run_provider(self, provider, reg: DeviceRegistry):
        """Esegue un provider isolandone i fallimenti dal resto dello scan."""
        try:
            await provider.run(reg)
        except Exception as e:
            log.error(f"discovery provider '{provider.name}' error: {e}")

    async def _publish(self, reg: DeviceRegistry):
        if not self.on_result:
            return
        try:
            await self.on_result(reg.all())
        except Exception as e:
            log.error(f"pubblicazione risultato scan: {e}")

    def get_cached(self) -> list[Device]:
        return self._registry.all()

    async def publish_cached(self):
        """Notifica i client con la cache corrente: usata dopo le modifiche al
        catalogo dalla UI, che devono comparire senza attendere uno scan."""
        await self._publish(self._registry)

    def refresh_catalog(self):
        """Ricarica il catalogo, fa il seed dei device manuali e riapplica
        subito ai device in cache (effetto immediato dopo modifiche dalla UI,
        senza attendere il rescan; un device appena aggiunto compare offline)."""
        self.reload_catalog()
        self._seed_catalog(self._registry)
        self._apply_catalog(self._registry)


_scanner: Optional[DeviceScanner] = None


def get_scanner() -> DeviceScanner:
    global _scanner
    if _scanner is None:
        _scanner = DeviceScanner()
        _scanner.reload_catalog()
    return _scanner
