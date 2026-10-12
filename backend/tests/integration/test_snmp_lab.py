"""
Client SNMP contro un agente VERO (lab di sviluppo), non contro fake.

Si attiva solo con le variabili del lab (SNMP_LAB_HOST, SNMP_LAB_COMMUNITY,
SNMP_LAB_USER, SNMP_LAB_AUTH, SNMP_LAB_PRIV) e con net-snmp nel PATH; senza,
si salta dicendolo. Le credenziali del lab non stanno nel repo.

    set -a; . ~/.config/lanmng/snmp-lab.env; set +a
    backend/.venv/bin/pytest backend/tests/integration/test_snmp_lab.py
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from config import SNMPDiscovery
from services.snmp import (SnmpAuthError, SnmpClient, SnmpTimeout, read_interfaces,
                           read_system)

LAB = {k: os.environ.get(f"SNMP_LAB_{k}", "") for k in
       ("HOST", "COMMUNITY", "USER", "AUTH", "PRIV")}

pytestmark = pytest.mark.skipif(
    not all(LAB.values()) or not shutil.which("snmpget"),
    reason="lab SNMP non configurato (variabili SNMP_LAB_* o net-snmp assenti)")


def _client(sbagliati: bool = False) -> SnmpClient:
    cfg = SNMPDiscovery(enabled=True, timeout=2, retries=0, profiles=[
        {"name": "default", "version": "2c"},
        {"name": "v3", "version": "3", "user": LAB["USER"]}])
    segreti = {("default", "community"): LAB["COMMUNITY"],
               ("v3", "auth"): LAB["AUTH"], ("v3", "priv"): LAB["PRIV"]}
    if sbagliati:
        segreti = {k: v[::-1] + "x" for k, v in segreti.items()}
    return SnmpClient(cfg, lambda p, k: segreti.get((p, k), ""))


@pytest.mark.parametrize("profilo", ["default", "v3"])
def test_sistema_e_interfacce(profilo):
    c = _client()
    sysinfo = asyncio.run(read_system(c, LAB["HOST"], profilo))
    assert sysinfo["name"] and sysinfo["descr"]
    assert isinstance(sysinfo["uptime_s"], int)
    ifs = asyncio.run(read_interfaces(c, LAB["HOST"], profilo))
    assert any(i["oper"] == "up" and isinstance(i["in_octets"], int) for i in ifs)


def test_community_sbagliata_e_un_timeout_pulito():
    with pytest.raises(SnmpTimeout) as e:
        asyncio.run(read_system(_client(sbagliati=True), LAB["HOST"], "default"))
    assert LAB["COMMUNITY"] not in str(e.value)


def test_passphrase_v3_sbagliata_e_un_errore_di_credenziali():
    with pytest.raises(SnmpAuthError) as e:
        asyncio.run(read_system(_client(sbagliati=True), LAB["HOST"], "v3"))
    assert LAB["AUTH"] not in str(e.value)
