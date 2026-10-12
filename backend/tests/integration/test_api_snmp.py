"""
API toccate dalla Fase 23 (SNMP): servizi `snmp`, storico per interfaccia,
editor della sezione `discovery_snmp`, segreti per profilo.
"""
from __future__ import annotations

import pytest

from config import settings


def _snmp_body(**kw):
    corpo = {"kind": "http", "type": "snmp", "name": "stampante", "host": "192.0.2.50",
             "oid": "1.3.6.1.2.1.25.3.5.1.1.1", "op": "eq", "value": "3"}
    corpo.update(kw)
    return corpo


def test_servizio_snmp_si_salva(auth_client):
    from services.service_store import get_service_store
    r = auth_client.post("/api/services/config", json=_snmp_body())
    try:
        assert r.status_code == 200, r.text
        e = r.json()["entry"]
        assert e == {"name": "stampante", "type": "snmp", "host": "192.0.2.50",
                     "oid": "1.3.6.1.2.1.25.3.5.1.1.1", "op": "eq", "value": "3",
                     "dashboard": True}
    finally:
        get_service_store().remove("http", "stampante")


@pytest.mark.parametrize("modifica,parola", [
    ({"oid": "sysUpTime.0"}, "OID"),
    ({"host": "-c"}, "host"),
    ({"op": "boh"}, "condizione"),
    ({"op": "gt", "value": "tanto"}, "numero"),
    ({"op": "eq", "value": ""}, "valore"),
    ({"profile": "inesistente"}, "profilo"),
])
def test_servizio_snmp_non_valido(auth_client, modifica, parola):
    from services.service_store import get_service_store
    r = auth_client.post("/api/services/config", json=_snmp_body(**modifica))
    assert r.status_code == 400
    assert parola.lower() in r.json()["detail"].lower()
    assert "stampante" not in str(get_service_store().read())


def test_servizio_snmp_esige_la_sessione(client):
    assert client.post("/api/services/config", json=_snmp_body()).status_code == 401


def test_storico_interfaccia(auth_client):
    r = auth_client.get("/api/history/interface",
                        params={"host": "192.0.2.2", "ifname": "port1", "period": "1h"})
    assert r.status_code == 200
    d = r.json()
    assert d["host"] == "192.0.2.2" and d["ifname"] == "port1"
    assert isinstance(d["points"], list)
    assert auth_client.get("/api/history/interface",
                           params={"host": "x", "ifname": "y", "period": "3y"}).status_code == 400


def test_sezione_snmp_validata_e_mascherata(auth_client):
    r = auth_client.put("/api/config/section/discovery_snmp",
                        json={"value": {"enabled": True,
                                        "hosts": [{"ip": "192.0.2.2", "profile": "nessuno"}]}})
    assert r.status_code == 400
    r = auth_client.put("/api/config/section/discovery_snmp",
                        json={"value": {"enabled": False, "community": "in-chiaro",
                                        "profiles": [{"name": "sw", "version": "3",
                                                      "user": "lanmng"}],
                                        "hosts": ["192.0.2.2",
                                                  {"ip": "192.0.2.3", "profile": "sw"}]}})
    assert r.status_code == 200, r.text
    letto = auth_client.get("/api/config/section/discovery_snmp").json()["value"]
    assert letto["community"] == "********"
    assert letto["profiles"][0]["name"] == "sw"


def test_segreti_snmp_nello_stato(auth_client):
    ids = {s["id"] for s in auth_client.get("/api/config/secrets").json()["secrets"]}
    assert "snmp_default_community" in ids


def test_storico_interfaccia_esige_la_sessione(client):
    r = client.get("/api/history/interface", params={"host": "192.0.2.2", "ifname": "p1"})
    assert r.status_code == 401
