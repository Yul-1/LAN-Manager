"""
SNMP (services/snmp.py): parsing dell'output net-snmp, credenziali, banda,
topologia.

Le fixture `snmp_*.txt` sono output VERO di net-snmp 5.9.4 (agente del lab su
WSL, stesse opzioni del client: -m "" -On -Oe -Ot -Ox), ripulito di nome
macchina, IP e MAC. LLDP e FDB sono invece scritti a mano seguendo i MIB:
in casa non c'e' uno switch che li esponga, e lo si dice qui invece di
spacciarli per catture.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from config import SNMPDiscovery, SNMPProfile
from services import snmp
from services.snmp import (IF_TABLE, SnmpAuthError, SnmpClient, SnmpError, SnmpTimeout,
                           SnmpUnavailable, build_fdb, build_interfaces, build_ip_addresses,
                           build_lldp, build_topology, classify_error, parse_output,
                           profile_conf, rate_bps, valid_host, valid_oid)

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _fix(nome: str) -> dict:
    return parse_output((FIX / nome).read_text())


# ── Parsing ────────────────────────────────────────────────────────

def test_system_reale_esadecimale_decodificato():
    v = _fix("snmp_system.txt")
    assert snmp.text(v[snmp.SYS_NAME][1]) == "labhost"
    assert snmp.text(v[snmp.SYS_DESCR][1]).startswith("Linux labhost 5.15")
    assert v[snmp.SYS_OBJECT_ID] == ("OID", "1.3.6.1.4.1.8072.3.2.10")
    # Timeticks con -Ot: numero nudo, senza tipo.
    assert v[snmp.SYS_UPTIME][0] == "Timeticks"
    assert isinstance(v[snmp.SYS_UPTIME][1], int)
    # "No Such Object" non diventa una voce con un valore inventato.
    assert "1.3.6.1.2.1.1.9.9.9.0" not in v


def test_interfacce_reali_fuse_con_ifxtable():
    ifs = build_interfaces(_fix("snmp_iftable.txt"), _fix("snmp_ifxtable.txt"))
    eth0 = next(i for i in ifs if i["name"] == "eth0")
    assert eth0["mac"] == "02:00:00:00:00:01"
    assert eth0["counter_bits"] == 64          # ifHCInOctets presenti
    assert eth0["oper"] == "up" and eth0["admin"] == "up"
    assert eth0["speed_mbps"] == 10000
    assert isinstance(eth0["in_octets"], int)
    lo = next(i for i in ifs if i["index"] == 1)
    assert lo["mac"] == ""                      # loopback senza indirizzo fisico
    assert [i["index"] for i in ifs] == sorted(i["index"] for i in ifs)


def test_senza_ifxtable_si_resta_a_32_bit():
    ifs = build_interfaces(_fix("snmp_iftable.txt"), {})
    eth0 = next(i for i in ifs if i["descr"] == "eth0")
    assert eth0["counter_bits"] == 32
    assert eth0["name"] == "eth0"               # ripiego su ifDescr


def test_indirizzi_per_interfaccia():
    per = build_ip_addresses(_fix("snmp_ipaddr.txt"))
    assert "192.0.2.16" in per[2]
    assert set(per[1]) == {"192.0.2.10", "192.0.2.11"}


def test_valori_e_continuazioni():
    out = parse_output(
        '.1.3.6.1.2.1.1.1.0 = STRING: "tra virgolette"\n'
        ".1.3.6.1.2.1.2.2.1.10.1 = Counter32: 4294967295\n"
        ".1.3.6.1.2.1.31.1.1.1.6.1 = Counter64: 18446744073709551615\n"
        ".1.3.6.1.2.1.1.3.0 = Timeticks: (100) 0:00:01.00\n"
        ".1.3.6.1.2.1.4.20.1.1.1 = IpAddress: 192.0.2.1\n"
        ".1.3.6.1.2.1.1.4.0 = \"\"\n"
        ".1.3.6.1.2.1.1.5.0 = Hex-STRING: 61 62 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F 70 \n"
        "71 72 \n"
        ".1.3.6.1.2.1.1.6.0 = No Such Instance currently exists at this OID\n"
        ".1.3.6.1.2.1.1.7.0 = No more variables left in this MIB View\n")
    assert out["1.3.6.1.2.1.1.1.0"] == ("STRING", b"tra virgolette")
    assert out["1.3.6.1.2.1.2.2.1.10.1"] == ("Counter32", 4294967295)
    assert out["1.3.6.1.2.1.31.1.1.1.6.1"][1] == 2 ** 64 - 1
    assert out["1.3.6.1.2.1.1.3.0"] == ("Timeticks", 100)
    assert out["1.3.6.1.2.1.4.20.1.1.1"] == ("IpAddress", "192.0.2.1")
    assert out["1.3.6.1.2.1.1.4.0"] == ("STRING", b"")
    assert out["1.3.6.1.2.1.1.5.0"] == ("STRING", b"abcdefghijklmnopqr")
    assert "1.3.6.1.2.1.1.6.0" not in out and "1.3.6.1.2.1.1.7.0" not in out


# ── Validazione al confine ─────────────────────────────────────────

@pytest.mark.parametrize("oid", ["1.3.6.1.2.1.1.3.0", ".1.3.6.1.2.1.1.3.0", "1.0.8802.1.1.2"])
def test_oid_validi(oid):
    assert valid_oid(oid) == oid.lstrip(".")


@pytest.mark.parametrize("oid", ["", "sysUpTime.0", "1.3.6 .1", "-c x", "2.3.4", "1"])
def test_oid_non_validi(oid):
    with pytest.raises(ValueError):
        valid_oid(oid)


@pytest.mark.parametrize("host", ["-c", "-Ofoo", "a b", "x;rm", "", "a\nb"])
def test_host_che_sembrano_opzioni_sono_rifiutati(host):
    # Un host "-c..." finirebbe a net-snmp come opzione: iniezione di argomenti.
    with pytest.raises(ValueError):
        valid_host(host)


# ── Credenziali ────────────────────────────────────────────────────

def _segreti(d):
    return lambda kind: d.get(kind, "")


def test_conf_v2c_con_community_quotata():
    conf = profile_conf(SNMPProfile(name="default"), _segreti({"community": 'a"b\\c d'}))
    assert "defVersion 2c" in conf
    assert 'defCommunity "a\\"b\\\\c d"' in conf
    assert "mibs :" in conf


def test_conf_v3_authpriv():
    p = SNMPProfile(name="sw", version="3", user="lanmng", auth_protocol="sha-256")
    conf = profile_conf(p, _segreti({"auth": "a" * 8, "priv": "p" * 8}))
    for riga in ("defVersion 3", 'defSecurityName "lanmng"', "defSecurityLevel authPriv",
                 "defAuthType SHA-256", "defPrivType AES"):
        assert riga in conf


def test_conf_v3_authnopriv_non_chiede_la_priv():
    p = SNMPProfile(name="sw", version="3", user="u", level="authNoPriv")
    conf = profile_conf(p, _segreti({"auth": "a" * 8}))
    assert "defPrivPassphrase" not in conf


@pytest.mark.parametrize("profilo,segreti", [
    (SNMPProfile(name="default"), {}),
    (SNMPProfile(name="s", version="3", user="u"), {"auth": "corta", "priv": "p" * 8}),
    (SNMPProfile(name="s", version="3", user="u"), {"auth": "a" * 8}),
    (SNMPProfile(name="default"), {"community": "con\na capo"}),
])
def test_credenziali_mancanti_o_malformate(profilo, segreti):
    # Senza credenziali si dice subito cosa manca, invece di un timeout che
    # farebbe credere a un host spento.
    with pytest.raises(SnmpUnavailable):
        profile_conf(profilo, _segreti(segreti))


def test_segreti_mai_in_argv_e_file_privato(monkeypatch):
    """La community va nel snmp.conf privato (0600 in una cartella 0700), non
    sulla riga di comando dove la vedrebbe chiunque legga /proc."""
    visti = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'.1.3.6.1.2.1.1.5.0 = Hex-STRING: 61 \n', b""

    async def finto_exec(*args, env=None, **kw):
        visti["args"], visti["env"] = args, env
        return _Proc()

    monkeypatch.setattr(snmp.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", finto_exec)
    cfg = SNMPDiscovery(enabled=True)
    c = SnmpClient(cfg, lambda p, k: "community-segretissima")
    out = asyncio.run(c.get("192.0.2.5", ["1.3.6.1.2.1.1.5.0"]))
    assert out["1.3.6.1.2.1.1.5.0"] == ("STRING", b"a")
    assert not any("community-segretissima" in str(a) for a in visti["args"])
    d = visti["env"]["SNMPCONFPATH"]
    conf = Path(d) / "snmp.conf"
    assert "community-segretissima" in conf.read_text()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(conf).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(Path(d).parent).st_mode) == 0o700
    # Nessun segreto nell'ambiente: solo il percorso del file.
    assert not any("community-segretissima" in v for v in visti["env"].values())


def test_profilo_inesistente():
    c = SnmpClient(SNMPDiscovery(), lambda p, k: "x")
    with pytest.raises(SnmpUnavailable):
        c.check_profile("nessuno")


@pytest.mark.parametrize("stderr,tipo", [
    ("Timeout: No Response from 192.0.2.5.", SnmpTimeout),
    ("snmpget: Authentication failure (incorrect password, community or key)", SnmpAuthError),
    ("snmpget: Unknown user name", SnmpAuthError),
    ("Error: passphrase chosen is below the length requirements", SnmpAuthError),
    ("qualcos'altro", SnmpError),
])
def test_classificazione_errori(stderr, tipo):
    e = classify_error(stderr, "192.0.2.5")
    assert type(e) is tipo
    assert "192.0.2.5" in str(e)


# ── Banda ──────────────────────────────────────────────────────────

def _c(octets, t, uptime=1000, bits=64):
    return {"octets": octets, "t": t, "uptime_s": uptime, "bits": bits}


def test_velocita_normale():
    assert rate_bps(_c(1000, 0), _c(2000, 10, 1010)) == 800.0


def test_prima_lettura_e_contatore_assente():
    assert rate_bps(None, _c(1, 1)) is None
    assert rate_bps(_c(None, 0), _c(1, 1)) is None


def test_giro_del_contatore_a_32_bit():
    prev = _c(2 ** 32 - 100, 0, bits=32)
    assert rate_bps(prev, _c(100, 10, 1010, bits=32)) == 200 * 8 / 10


def test_contatore_a_64_bit_che_torna_indietro_e_un_buco():
    assert rate_bps(_c(5000, 0), _c(100, 10, 1010)) is None


def test_device_riavviato_e_un_buco():
    # Uptime calato: i contatori sono ripartiti da zero, anche a 32 bit.
    assert rate_bps(_c(2 ** 32 - 100, 0, 5000, 32), _c(100, 10, 3, 32)) is None


def test_tempo_non_avanzato():
    assert rate_bps(_c(1, 5), _c(2, 5)) is None


# ── LLDP e FDB (scritti a mano sui MIB, vedi docstring) ────────────

LLDP_LOC = (
    ".1.0.8802.1.1.2.1.3.7.1.3.5 = Hex-STRING: 70 6F 72 74 35 \n"     # port5
    ".1.0.8802.1.1.2.1.3.7.1.4.5 = Hex-STRING: 50 6F 72 74 61 20 35 \n"  # Porta 5
)
LLDP_REM = (
    ".1.0.8802.1.1.2.1.4.1.1.4.0.5.1 = INTEGER: 4\n"
    ".1.0.8802.1.1.2.1.4.1.1.5.0.5.1 = Hex-STRING: 02 00 00 00 00 AA \n"
    ".1.0.8802.1.1.2.1.4.1.1.6.0.5.1 = INTEGER: 5\n"
    ".1.0.8802.1.1.2.1.4.1.1.7.0.5.1 = Hex-STRING: 65 74 68 30 \n"
    ".1.0.8802.1.1.2.1.4.1.1.9.0.5.1 = Hex-STRING: 61 70 2D 73 61 6C 61 \n"
)


def test_vicini_lldp():
    n = build_lldp(parse_output(LLDP_LOC), parse_output(LLDP_REM))
    assert n == [{"local_port": "Porta 5", "chassis": "02:00:00:00:00:AA",
                  "port": "eth0", "port_descr": "", "sys_name": "ap-sala",
                  "sys_descr": ""}]


FDB_BASE = (".1.3.6.1.2.1.17.1.4.1.2.1 = INTEGER: 101\n"
            ".1.3.6.1.2.1.17.1.4.1.2.2 = INTEGER: 102\n")
FDB = (".1.3.6.1.2.1.17.4.3.1.2.2.0.0.0.0.1 = INTEGER: 1\n"
       ".1.3.6.1.2.1.17.4.3.1.3.2.0.0.0.0.1 = INTEGER: 3\n"
       ".1.3.6.1.2.1.17.4.3.1.2.2.0.0.0.0.9 = INTEGER: 2\n"
       ".1.3.6.1.2.1.17.4.3.1.3.2.0.0.0.0.9 = INTEGER: 4\n")   # self: lo switch
QFDB = ".1.3.6.1.2.1.17.7.1.2.2.1.2.10.2.0.0.0.0.2 = INTEGER: 2\n"


def test_fdb_bridge_e_qbridge():
    voci = build_fdb(parse_output(FDB_BASE), parse_output(FDB), parse_output(QFDB),
                     {101: "port1", 102: "port2"})
    assert voci == [{"mac": "02:00:00:00:00:01", "ifindex": 101, "port": "port1"},
                    {"mac": "02:00:00:00:00:02", "ifindex": 102, "port": "port2"}]


# ── Topologia ──────────────────────────────────────────────────────

def _dev(key, mac="", name=""):
    return {"key": key, "ips": [key], "mac": mac, "name": name, "hostname": ""}


def test_topologia_lldp_vince_e_porta_meno_affollata():
    devices = [_dev("192.0.2.2", "02:00:00:00:00:F0", "switch"),
               _dev("192.0.2.3", "02:00:00:00:00:AA", "ap-sala"),
               _dev("192.0.2.10", "02:00:00:00:00:01"),
               _dev("192.0.2.11", "02:00:00:00:00:02")]
    hosts = [{"ip": "192.0.2.2",
              "lldp": [{"local_port": "Porta 5", "chassis": "02:00:00:00:00:AA",
                        "sys_name": "ap-sala"}],
              "fdb": [{"mac": "02:00:00:00:00:AA", "port": "Porta 1"},   # LLDP vince
                      {"mac": "02:00:00:00:00:01", "port": "Porta 2"},
                      {"mac": "02:00:00:00:00:02", "port": "Porta 3"},
                      {"mac": "02:00:00:00:00:01", "port": "Porta 3"}]}]
    links = {l["device"]: l for l in build_topology(hosts, devices)}
    assert links["192.0.2.3"] == {"device": "192.0.2.3", "via": "192.0.2.2",
                                  "port": "Porta 5", "source": "lldp"}
    # .10 compare su due porte: vince quella con meno MAC (la sua).
    assert links["192.0.2.10"]["port"] == "Porta 2"
    assert links["192.0.2.11"]["port"] == "Porta 3"
    assert "192.0.2.2" not in links            # lo switch non sta sotto se stesso


def test_uplink_verso_altro_switch_non_usato_per_fdb():
    devices = [_dev("192.0.2.2", "02:00:00:00:00:F0"), _dev("192.0.2.4", "02:00:00:00:00:F1"),
               _dev("192.0.2.10", "02:00:00:00:00:01")]
    hosts = [{"ip": "192.0.2.2", "lldp": [{"local_port": "up", "chassis": "02:00:00:00:00:F1"}],
              "fdb": [{"mac": "02:00:00:00:00:01", "port": "up"}]},
             {"ip": "192.0.2.4", "lldp": [], "fdb": [{"mac": "02:00:00:00:00:01", "port": "p7"}]}]
    links = {l["device"]: l for l in build_topology(hosts, devices)}
    assert links["192.0.2.10"] == {"device": "192.0.2.10", "via": "192.0.2.4",
                                   "port": "p7", "source": "fdb"}


def test_nessun_anello():
    devices = [_dev("192.0.2.2", "02:00:00:00:00:F0"), _dev("192.0.2.4", "02:00:00:00:00:F1")]
    hosts = [{"ip": "192.0.2.2", "lldp": [{"local_port": "a", "chassis": "02:00:00:00:00:F1"}]},
             {"ip": "192.0.2.4", "lldp": [{"local_port": "b", "chassis": "02:00:00:00:00:F0"}]}]
    links = build_topology(hosts, devices)
    assert len(links) == 1


# ── Poller ─────────────────────────────────────────────────────────

class _ClientFinto:
    def __init__(self):
        self.octets = 1000
        self.uptime = 100_00
        self.errore = None

    async def get(self, host, oids, profile="default", **kw):
        if self.errore:
            raise self.errore
        return {snmp.SYS_UPTIME: ("Timeticks", self.uptime)}

    async def walk(self, host, oid, profile="default"):
        if self.errore:
            raise self.errore
        if oid == IF_TABLE:
            return parse_output(
                f".{IF_TABLE}.2.2 = Hex-STRING: 65 74 68 30 \n"
                f".{IF_TABLE}.8.2 = INTEGER: 1\n"
                f".{IF_TABLE}.10.2 = Counter32: {self.octets}\n"
                f".{IF_TABLE}.16.2 = Counter32: {self.octets * 2}\n")
        return {}


def test_poller_calcola_la_banda_e_dopo_un_errore_riparte_da_capo(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.discovery.snmp, "enabled", True)
    stato = snmp.SnmpState()
    finto = _ClientFinto()
    stato.client = finto
    stato.set_discovered([snmp.SnmpHostData(ip="192.0.2.2", reachable=True)])
    tempo = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0, 30.0, 30.0])
    stato._now = lambda: next(tempo)

    p1 = asyncio.run(stato.poll())
    assert p1["192.0.2.2"][0]["rx_bps"] is None          # prima lettura
    finto.octets, finto.uptime = 2000, 101_00
    p2 = asyncio.run(stato.poll())
    assert p2["192.0.2.2"][0] == {"ifname": "eth0", "rx_bps": 800.0, "tx_bps": 1600.0}

    finto.errore = SnmpTimeout("192.0.2.2: nessuna risposta")
    assert asyncio.run(stato.poll()) == {}
    h = stato.hosts["192.0.2.2"]
    assert not h.reachable and "nessuna risposta" in h.error

    finto.errore, finto.octets, finto.uptime = None, 9000, 103_00
    p4 = asyncio.run(stato.poll())
    # Dopo il buco non si spalma il traffico accumulato: prima lettura di nuovo.
    assert p4["192.0.2.2"][0]["rx_bps"] is None
    assert stato.snapshot()["hosts"][0]["reachable"] is True


# ── Discovery: riassunto di chi ha smesso di rispondere ────────────

def test_device_che_non_risponde_piu_perde_il_riassunto(monkeypatch):
    from config import settings
    from services.discovery import SNMPProvider
    from services.registry import DeviceRegistry
    monkeypatch.setattr(settings.discovery, "snmp",
                        SNMPDiscovery(enabled=True, autodiscover=False))
    monkeypatch.setattr(snmp.shutil, "which", lambda c: "/usr/bin/" + c)
    reg = DeviceRegistry()
    dev = reg.upsert(ip="192.0.2.7")
    dev.snmp = {"name": "vecchio"}          # riportato dallo scan precedente
    asyncio.run(SNMPProvider().run(reg))
    assert dev.snmp == {}


def test_uptime_della_macchina_prima_di_quello_dellagente():
    v = {snmp.SYS_UPTIME: ("Timeticks", 3600), snmp.HR_UPTIME: ("Timeticks", 360000000)}
    assert snmp.host_uptime(v) == 3600000
    assert snmp.host_uptime({snmp.SYS_UPTIME: ("Timeticks", 3600)}) == 36
    assert snmp.host_uptime({}) is None
