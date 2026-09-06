"""
Identita' dei dispositivi (services/registry.py).

Regressione 0.1.28: il registro indicizzava per MAC, quindi una scheda in bridge
su Wi-Fi — che espone lo stesso MAC per l'host e per le VM — faceva collassare
piu' dispositivi in uno solo. La regola oggi e' che **l'IP e' l'identita'**:
indirizzi diversi restano dispositivi distinti anche a parita' di MAC.
"""
from __future__ import annotations

from services.registry import Device, DeviceRegistry


# ── La regressione ─────────────────────────────────────────────────

def test_stesso_mac_su_due_ip_resta_due_dispositivi():
    reg = DeviceRegistry()
    a = reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.10", source="arp")
    b = reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.11", source="arp")
    assert a is not b, "una scheda in bridge non deve fondere host e VM"
    assert len(reg.all()) == 2
    assert {d.key for d in reg.all()} == {"192.0.2.10", "192.0.2.11"}


def test_stesso_ip_da_sorgenti_diverse_resta_un_dispositivo():
    reg = DeviceRegistry()
    reg.upsert(ip="192.0.2.10", source="nmap")                       # nmap: senza MAC
    reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.10", source="arp")
    reg.upsert(ip="192.0.2.10", hostname="nas.lan", source="dhcp")
    assert len(reg.all()) == 1
    dev = reg.all()[0]
    assert dev.mac == "AA:BB:CC:DD:EE:FF"
    assert dev.hostname == "nas.lan"
    assert set(dev.discovered_by) == {"nmap", "arp", "dhcp"}


def test_all_non_duplica_un_device_presente_in_entrambi_gli_indici():
    reg = DeviceRegistry()
    reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.10", source="arp")
    # Presente sia in _by_ip sia in _by_mac: all() deduplica per identita' oggetto.
    assert len(reg.all()) == 1


# ── Normalizzazione e chiavi ───────────────────────────────────────

def test_il_mac_viene_normalizzato():
    reg = DeviceRegistry()
    a = reg.upsert(mac="aa-bb-cc-dd-ee-ff", ip="192.0.2.10")
    assert a.mac == "AA:BB:CC:DD:EE:FF"
    # Stesso MAC scritto in due modi, stesso IP: un solo dispositivo.
    b = reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.10")
    assert a is b


def test_la_chiave_e_il_primo_ip_poi_il_mac():
    assert Device(ips=["192.0.2.10"], mac="AA:BB:CC:DD:EE:FF").key == "192.0.2.10"
    assert Device(mac="aa:bb:cc:dd:ee:ff").key == "AA:BB:CC:DD:EE:FF"
    assert Device().key == "unknown"


def test_device_senza_ip_e_indicizzato_per_mac():
    reg = DeviceRegistry()
    a = reg.upsert(mac="AA:BB:CC:DD:EE:FF", source="dhcp")
    b = reg.upsert(mac="AA:BB:CC:DD:EE:FF", source="arp")
    assert a is b and len(reg.all()) == 1


def test_find_by_ip_ignora_gli_spazi():
    reg = DeviceRegistry()
    dev = reg.upsert(ip="192.0.2.10")
    assert reg.find_by_ip("  192.0.2.10  ") is dev
    assert reg.find_by_ip("192.0.2.99") is None


# ── Nome mostrato e arricchimento ──────────────────────────────────

def test_display_name_segue_la_scala_nome_hostname_ip_mac():
    assert Device(name="NAS", hostname="nas.lan", ips=["192.0.2.10"]).display_name == "NAS"
    assert Device(hostname="nas.lan", ips=["192.0.2.10"]).display_name == "nas"   # senza dominio
    assert Device(ips=["192.0.2.10"]).display_name == "192.0.2.10"
    assert Device(mac="AA:BB:CC:DD:EE:FF").display_name == "AA:BB:CC:DD:EE:FF"


def test_un_upsert_a_campi_vuoti_non_cancella_quello_che_si_sa_gia():
    reg = DeviceRegistry()
    reg.upsert(mac="AA:BB:CC:DD:EE:FF", ip="192.0.2.10", hostname="nas.lan", source="dhcp")
    reg.upsert(ip="192.0.2.10", source="ping")        # il ping sweep non sa altro
    dev = reg.find_by_ip("192.0.2.10")
    assert dev.hostname == "nas.lan" and dev.mac == "AA:BB:CC:DD:EE:FF"


def test_tag_source_non_ripete_la_stessa_sorgente():
    dev = Device(ips=["192.0.2.10"])
    dev.tag_source("nmap")
    dev.tag_source("nmap")
    assert dev.discovered_by == ["nmap"]


def test_merge_catalog_non_aliasa_la_lista_del_catalogo():
    # Se la lista fosse condivisa, arricchire un device muterebbe devices.yaml
    # in memoria e il cambiamento si propagherebbe a ogni altro device.
    catalogo = {"name": "NAS", "services": ["smb", "nfs"]}
    dev = Device(ips=["192.0.2.10"])
    dev.merge_catalog(catalogo)
    dev.services.append("iscsi")
    assert catalogo["services"] == ["smb", "nfs"]


def test_to_dict_espone_il_nome_mostrato_e_ripiega_su_os_guess():
    dev = Device(ips=["192.0.2.10"], hostname="nas.lan", os_guess="Linux 5.X")
    out = dev.to_dict()
    assert out["name"] == "nas"
    assert out["os"] == "Linux 5.X"          # os vuoto -> os_guess
    dev.os = "Debian 12"
    assert dev.to_dict()["os"] == "Debian 12"
