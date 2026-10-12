"""
SNMP: configurazione (profili, host), segreti per profilo, condizioni dei
servizi `snmp`, storico della banda per interfaccia.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import SNMPDiscovery, settings


# ── Config ─────────────────────────────────────────────────────────

def test_host_come_ip_o_oggetto_e_profilo_default_implicito():
    s = SNMPDiscovery(hosts=["192.0.2.2", {"ip": "192.0.2.3", "profile": "sw"}],
                      profiles=[{"name": "sw", "version": 3, "user": "u"}])
    assert [(h.ip, h.profile) for h in s.hosts] == [("192.0.2.2", "default"),
                                                    ("192.0.2.3", "sw")]
    assert [p.name for p in s.all_profiles()] == ["default", "sw"]
    assert s.profile("default").version == "2c"


def test_default_dai_campi_storici():
    assert SNMPDiscovery(version=1).profile("default").version == "1"


@pytest.mark.parametrize("cfg", [
    {"hosts": ["non-un-ip"]},
    {"hosts": [{"ip": "192.0.2.1", "profile": "inesistente"}]},
    {"version": "3"},                                   # la v3 vuole un profilo
    {"profiles": [{"name": "Con Spazi"}]},
    {"profiles": [{"name": "v3", "version": "3"}]},     # v3 senza utente
    {"profiles": [{"name": "a"}, {"name": "a"}]},
    {"profiles": [{"name": "a", "version": "3", "user": "u", "auth_protocol": "SHA1"}]},
    {"profiles": [{"name": "a", "version": "4"}]},
    {"traffic_interval": 1},
])
def test_config_non_valide(cfg):
    with pytest.raises(ValidationError):
        SNMPDiscovery(**cfg)


# ── Segreti per profilo ────────────────────────────────────────────

def _con_profili(monkeypatch, **kw):
    monkeypatch.setattr(settings.discovery, "snmp", SNMPDiscovery(**kw))


def test_voci_dei_segreti_seguono_i_profili(monkeypatch):
    from services.secrets_store import get_secrets_store
    _con_profili(monkeypatch, profiles=[
        {"name": "sw", "version": "3", "user": "u"},
        {"name": "ap", "version": "3", "user": "u", "level": "authNoPriv"}])
    ids = {s["id"] for s in get_secrets_store().status()}
    assert {"snmp_default_community", "snmp_sw_auth", "snmp_sw_priv", "snmp_ap_auth"} <= ids
    assert "snmp_ap_priv" not in ids and "snmp_sw_community" not in ids


def test_segreto_snmp_scritto_e_mai_riletto_via_status(monkeypatch):
    from services.secrets_store import get_secrets_store, snmp_secret
    _con_profili(monkeypatch)
    st = get_secrets_store()
    st.update({"snmp_default_community": "comunita-privata"})
    try:
        assert snmp_secret("default", "community") == "comunita-privata"
        voce = next(s for s in st.status() if s["id"] == "snmp_default_community")
        assert voce["set"] is True and "comunita-privata" not in str(st.status())
        with pytest.raises(ValueError):
            st.update({"snmp_altro_community": "x"})   # profilo non configurato
    finally:
        st.update({"snmp_default_community": "__CLEAR__"})


def test_community_storica_in_yaml_resta_valida_con_avviso(monkeypatch):
    from services.secrets_store import snmp_secret
    from services.snmp import community_in_yaml_warning
    _con_profili(monkeypatch, enabled=True, community="vecchia")
    assert snmp_secret("default", "community") == "vecchia"
    assert "segreti" in community_in_yaml_warning()


def test_passphrase_v3_mascherate_se_finiscono_in_yaml():
    from services.config_store import MASK, _redact
    fuori = _redact({"auth_password": "a", "priv_password": "b"})
    assert set(fuori.values()) == {MASK}


# ── Condizioni dei servizi snmp ────────────────────────────────────

@pytest.mark.parametrize("valore,op,atteso,ok", [
    (b"up", "responds", "", True),
    (1, "eq", "1", True),
    (2, "eq", "1", False),
    (2, "ne", "1", True),
    (75, "gt", "70", True),
    (10, "gt", "9", True),          # numerico, non "10" < "9" fra testi
    (5, "lt", "3", False),
    (b"Printer ready", "contains", "ready", True),
    (b"testo", "gt", "3", False),   # confronto numerico impossibile
])
def test_condizioni(valore, op, atteso, ok):
    from services.healthcheck import snmp_condition
    assert snmp_condition(valore, op, atteso)[0] is ok


def test_check_snmp_con_host_non_valido_non_esegue_nulla():
    import asyncio
    from services.healthcheck import HealthChecker
    r = asyncio.run(HealthChecker()._run_one(
        {"name": "x", "type": "snmp", "host": "-c", "oid": "1.3.6.1.2.1.1.3.0"}))
    assert not r.ok and "host non valido" in r.detail


# ── Storico banda per interfaccia ──────────────────────────────────

def test_storico_interfacce_scrive_legge_e_compatta(tmp_path, monkeypatch):
    import time
    from services.history_store import HistoryStore
    st = HistoryStore(path=str(tmp_path / "h.db"), enabled=True)
    st.add_iface_points({"192.0.2.2": [{"ifname": "port1", "rx_bps": 800.0, "tx_bps": None}]})
    punti = st.iface_series("192.0.2.2", "port1", int(time.time() * 1000) - 3_600_000)
    pieni = [p for p in punti if p["rx_bps"] is not None]
    assert pieni and pieni[-1]["rx_bps"] == 800.0 and pieni[-1]["tx_bps"] is None
    # Un'altra interfaccia dello stesso host non eredita i punti della prima.
    altra = st.iface_series("192.0.2.2", "port2", int(time.time() * 1000) - 3_600_000)
    assert all(p["rx_bps"] is None for p in altra)
    monkeypatch.setattr(settings.history, "full_hours", 0)
    st.compact()                    # non deve sollevare con la tabella nuova
    st.close()
