"""
Comando `ubus call` costruito da services/openwrt.py.

Pentest 2026-09-24 (G11): i parametri JSON erano racchiusi fra apici a mano,
quindi un `'` in un valore chiudeva la stringa e il resto passava alla shell
del router. Oggi i valori arrivano da un dump ubus precedente, non dall'API:
il test fissa la regola prima che qualcuno la cambi.
"""
from __future__ import annotations

import json
import shlex

from services.openwrt import RouterClient


class _SSHFinto:
    def __init__(self):
        self.comandi: list[str] = []

    async def run(self, cmd):
        self.comandi.append(cmd)
        return "{}", ""


def _client():
    c = RouterClient.__new__(RouterClient)     # il costruttore vero apre l'SSH
    c.ssh = _SSHFinto()
    return c


async def test_un_apice_nei_parametri_resta_dentro_un_solo_argomento():
    c = _client()
    params = {"name": "eth0'; reboot; echo '"}
    await c._ubus("network.device", "status", params)
    argv = shlex.split(c.ssh.comandi[0])
    assert argv[:4] == ["ubus", "call", "network.device", "status"]
    assert len(argv) == 5
    assert json.loads(argv[4]) == params


async def test_senza_parametri_il_comando_non_cambia():
    c = _client()
    await c._ubus("system", "info")
    assert c.ssh.comandi == ["ubus call system info"]
