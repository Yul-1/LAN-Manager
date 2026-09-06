"""
Interfacce di rete per dispositivo, raccolte con i facts SSH.

Prima le interfacce esistevano solo per due cose: il router (via LuCI) e
l'host di LANMng (`ip -j addr show` eseguito in locale). La scheda di un
dispositivo qualunque non poteva mostrarle.

Viaggiano nello **stesso** comando degli altri facts, quindi non aprono una
seconda connessione SSH per ciclo. Dove `ip -j` non esiste (busybox, sistemi
senza iproute2) la riga esce vuota e il dispositivo resta senza interfacce:
e' la verita', non un errore da segnalare.
"""
from __future__ import annotations

import json

from services.discovery import _MAX_IFACES, _FACTS_CMD, _parse_facts, _parse_ifaces
from services.registry import Device

IP_J = json.dumps([
    {"ifname": "lo", "operstate": "UNKNOWN", "mtu": 65536, "address": "00:00:00:00:00:00",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "eth0", "operstate": "UP", "mtu": 1500, "address": "aa:bb:cc:00:00:01",
     "addr_info": [{"family": "inet", "local": "192.0.2.10", "prefixlen": 24},
                   {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
    {"ifname": "docker0", "operstate": "DOWN", "mtu": 1500, "address": "02:42:00:00:00:01",
     "master": "br-1", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
])


def test_il_comando_chiede_le_interfacce_senza_una_connessione_in_piu():
    # Una sola riga aggiunta al comando esistente: la connessione SSH per host
    # e' gia' aperta, e il ciclo lento non deve pagarne una seconda.
    for chiave in ("HOST", "OS", "DOCKER", "SERVICES", "IFACES"):
        assert f"echo {chiave}=" in _FACTS_CMD
    # Un comando solo: nessun `ssh` in piu' e nessuna seconda connessione.
    assert _FACTS_CMD.count(";") == 4
    # 2>/dev/null: su un host senza iproute2 l'errore non deve sporcare i facts.
    assert "ip -j addr show 2>/dev/null" in _FACTS_CMD


def test_le_interfacce_arrivano_intere_attraverso_il_parser_dei_facts():
    """`ip -j` stampa JSON multiriga: il comando lo appiattisce, ma il valore
    contiene `=` e spazi e il parser deve restituirlo tutto."""
    stdout = f"HOST=nas\nOS=Debian 12\nDOCKER=yes\nSERVICES=ssh.service\nIFACES={IP_J}\n"
    facts = _parse_facts(stdout)
    assert facts["OS"] == "Debian 12"
    assert _parse_ifaces(facts["IFACES"])[1]["name"] == "eth0"


def test_forma_normalizzata_uguale_a_quella_dell_host_locale():
    ifaces = _parse_ifaces(IP_J)
    assert [i["name"] for i in ifaces] == ["lo", "eth0", "docker0"]
    eth = ifaces[1]
    assert eth["state"] == "up" and eth["mtu"] == 1500
    assert eth["mac"] == "aa:bb:cc:00:00:01"
    assert eth["addresses"] == [
        {"ip": "192.0.2.10", "prefix": 24, "family": "inet"},
        {"ip": "fe80::1", "prefix": 64, "family": "inet6"},
    ]
    assert ifaces[2]["master"] == "br-1", "l'appartenenza a un bridge e' un dato utile"


def test_un_host_senza_iproute2_non_e_un_errore():
    assert _parse_ifaces("") == []


def test_output_illeggibile_non_fa_cadere_lo_scan():
    """Il provider e' a fallimento isolato: un output strano su un host non
    deve togliere le interfacce a tutti gli altri."""
    assert _parse_ifaces("questo non e' json") == []
    assert _parse_ifaces(json.dumps(["non un oggetto"])) == []


def test_le_veth_di_un_host_docker_non_gonfiano_lo_snapshot():
    """Su un host Docker c'e' una veth per container e lo snapshot viaggia sul
    WebSocket ad ogni ciclo: oltre il tetto si tronca."""
    tante = json.dumps([{"ifname": f"veth{i}", "operstate": "UP", "mtu": 1500,
                         "addr_info": []} for i in range(_MAX_IFACES + 25)])
    assert len(_parse_ifaces(tante)) == _MAX_IFACES


def test_un_dispositivo_senza_ssh_non_ha_interfacce_inventate():
    # Il campo esiste sempre, cosi' il frontend non deve indovinare, ma resta
    # vuoto per il telefono e la stampante: e' un dato che non abbiamo.
    assert Device(ips=["192.0.2.50"]).to_dict()["interfaces"] == []
