"""
services/registry.py — Modello Device e registro di discovery
=============================================================
Il `DeviceRegistry` raccoglie i dispositivi trovati dalle varie sorgenti
di discovery (DHCP, ARP, nmap, SSH, SNMP, ...) e li deduplica.

**L'identita' e' l'indirizzo IP**, non il MAC. Osservazioni diverse sullo
stesso IP (nmap lo vede senza MAC, ARP con il MAC, il DHCP con l'hostname)
confluiscono nello stesso Device; IP diversi restano dispositivi distinti
anche a parita' di MAC.

Il MAC non puo' fare da identita': una scheda di rete condivisa espone lo
stesso MAC per indirizzi che sono dispositivi diversi. Succede con le VM in
bridge su Wi-Fi (VirtualBox non puo' falsificare il MAC su una radio, quindi
host e VM si presentano con quello della scheda fisica) e con un host che
ha piu' indirizzi sulla stessa interfaccia. Il catalogo (`devices.yaml`) e'
del resto gia' indicizzato per IP e da' nomi distinti a ciascun indirizzo.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Device:
    mac: str = ""
    ips: list[str] = field(default_factory=list)
    hostname: str = ""
    # Dal catalogo manuale (devices.yaml)
    name: str = ""
    type: str = "unknown"     # router, desktop, laptop, server, vm, mobile, cloud, ap, printer
    os: str = ""
    subnet: str = ""
    services: list[str] = field(default_factory=list)
    notes: str = ""
    url: str = ""
    # Arricchimenti da discovery
    vendor: str = ""          # OUI / nmap MAC vendor
    os_guess: str = ""        # OS rilevato (nmap/ssh/snmp) se 'os' non noto
    open_ports: list[int] = field(default_factory=list)
    discovered_by: list[str] = field(default_factory=list)
    # Interfacce di rete dell'host, quando raggiungibile via SSH (facts).
    # Vuoto per tutti gli altri: e' un dato che non abbiamo, non uno zero.
    interfaces: list[dict] = field(default_factory=list)
    # Gestione manuale
    hidden: bool = False      # nascosto dall'utente (resta escluso anche se ri-scoperto)
    # Stato live
    online: bool = False
    latency_ms: float = 0.0
    last_seen: int = 0

    @property
    def key(self) -> str:
        """Chiave stabile: l'IP, altrimenti il MAC per i device senza indirizzo.

        E' anche l'identificatore che la UI rimanda alle API per modificare,
        nascondere o rimuovere: deve quindi combaciare con come il catalogo
        indicizza le sue entry (`ip:` o `mac:` in devices.yaml)."""
        if self.ips:
            return self.ips[0]
        return self.mac.upper() if self.mac else "unknown"

    @property
    def display_name(self) -> str:
        """Nome del catalogo, altrimenti l'hostname risolto, altrimenti l'IP.

        Dell'hostname si usa la prima etichetta: il PTR risponde con il nome
        completo (`host-w.lan`) ma in elenco serve il nome corto, come nel
        catalogo. Si taglia il dominio in generale, senza sapere quale sia."""
        if self.name:
            return self.name
        if self.hostname:
            return self.hostname.split(".")[0]
        return self.ips[0] if self.ips else self.mac

    def tag_source(self, source: str):
        if source not in self.discovered_by:
            self.discovered_by.append(source)

    def merge_catalog(self, entry: dict):
        self.name = entry.get("name", self.name)
        self.type = entry.get("type", self.type)
        self.os = entry.get("os", self.os)
        # Copia la lista: non condividere per riferimento la lista del catalogo
        # (una mutazione futura di dev.services non deve toccare il catalogo).
        if "services" in entry:
            self.services = list(entry.get("services") or [])
        self.notes = entry.get("notes", self.notes)
        self.url = entry.get("url", self.url)

    def to_dict(self) -> dict:
        return {
            "mac": self.mac,
            "ips": self.ips,
            "hostname": self.hostname,
            "name": self.display_name,
            "type": self.type,
            "os": self.os or self.os_guess,
            "subnet": self.subnet,
            "services": self.services,
            "notes": self.notes,
            "url": self.url,
            "vendor": self.vendor,
            "open_ports": self.open_ports,
            "discovered_by": self.discovered_by,
            "interfaces": self.interfaces,
            "hidden": self.hidden,
            "key": self.key,
            "online": self.online,
            "latency_ms": self.latency_ms,
            "last_seen": self.last_seen,
        }


class DeviceRegistry:
    """Registro di Device deduplicato per IP (vedi il modulo per il perche')."""

    def __init__(self):
        # _by_mac serve solo alle osservazioni prive di IP: tiene il primo
        # Device visto con quel MAC, non e' un indice di identita'.
        # In particolare non fa da ponte verso gli indirizzi: un MAC in bridge
        # appartiene a piu' dispositivi, quindi un IP nuovo non deve mai
        # atterrare sul Device che quel MAC ha gia'.
        self._by_mac: dict[str, Device] = {}
        self._by_ip: dict[str, Device] = {}

    def upsert(self, mac: str = "", ip: str = "", hostname: str = "",
               source: str = "") -> Device:
        """Inserisce o aggiorna il Device di questo IP (o del MAC, se manca l'IP)."""
        # MAC canonico (upper + separatore ':') per confronti coerenti fra
        # sorgenti (ARP/DHCP usano ':', il catalogo potrebbe usare '-').
        mac = (mac or "").upper().strip().replace("-", ":")
        ip = (ip or "").strip()

        dev: Device | None = None
        if ip:
            # Solo per IP: un indirizzo mai visto e' un dispositivo nuovo anche
            # se il MAC e' gia' noto. Prima l'osservazione veniva assorbita dal
            # Device di una entry di catalogo col solo MAC, e una VM in bridge
            # (che sul filo mostra il MAC della scheda dell'host) ne ereditava
            # nome, OS e note: e' successo davvero con una VM su un altro
            # indirizzo della stessa macchina.
            dev = self._by_ip.get(ip)
        elif mac:
            dev = self._by_mac.get(mac)

        if dev is None:
            dev = Device(mac=mac)

        if mac:
            if not dev.mac:
                dev.mac = mac
            self._by_mac.setdefault(mac, dev)

        if ip:
            if ip not in dev.ips:
                dev.ips.append(ip)
            self._by_ip[ip] = dev
        if hostname and not dev.hostname:
            dev.hostname = hostname
        if source:
            dev.tag_source(source)
        return dev

    def find_by_ip(self, ip: str) -> Device | None:
        return self._by_ip.get(ip.strip())

    def all(self) -> list[Device]:
        """Lista deduplicata (per identita' oggetto)."""
        seen: dict[int, Device] = {}
        for dev in list(self._by_mac.values()) + list(self._by_ip.values()):
            seen[id(dev)] = dev
        return list(seen.values())
