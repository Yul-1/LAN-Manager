"""
Riavvio del servizio dalla dashboard (services/restart.py).

Il backend non si riavvia da se': **esce**, e a farlo ripartire e' la politica
di riavvio del container. Da qui la regola che questi test tengono ferma —
dove non c'e' chi lo riaccenda, il pulsante non si deve offrire, perche'
premerlo lascerebbe la dashboard spenta e irraggiungibile proprio dalla pagina
che si stava usando.
"""
from __future__ import annotations

import pytest

from services import restart

CID = "9f8e7d6c5b4a3210fedcba98765432109876543210fedcba9876543210fedcba"        # id di container: 64 cifre esadecimali
MOUNTINFO = (f"1234 1200 0:52 /docker/containers/{CID}/hostname /etc/hostname "
             "rw,relatime - ext4 /dev/sda1 rw\n")


@pytest.fixture
def dentro_un_container(monkeypatch, tmp_path):
    """Finge il container: /.dockerenv assente, id preso dai mount.

    Con `network_mode: host` (lo scenario reale su homeserver) l'hostname e'
    quello dell'host, quindi l'id si legge solo da /proc/self/mountinfo: e'
    proprio il caso che va coperto.
    """
    finto = tmp_path / "mountinfo"
    finto.write_text(MOUNTINFO)
    vero = restart.Path

    def _path(p):
        if p == "/proc/self/mountinfo":
            return finto
        if p == "/.dockerenv":
            return tmp_path / "non-esiste"
        return vero(p)

    monkeypatch.setattr(restart, "Path", _path)


async def test_fuori_da_un_container_il_pulsante_non_si_offre(monkeypatch):
    monkeypatch.setattr(restart, "in_container", lambda: False)
    info = await restart.stato()
    assert info["available"] is False
    assert "non gira in un container" in info["reason"]


def test_lid_del_container_si_legge_dai_mount(dentro_un_container):
    assert restart.in_container() is True
    assert restart.container_id() == CID


async def test_con_una_politica_buona_il_riavvio_e_disponibile(dentro_un_container, monkeypatch):
    async def _politica(cid):
        return "unless-stopped"
    monkeypatch.setattr(restart, "_politica_di_riavvio", _politica)
    info = await restart.stato()
    assert info["available"] is True
    assert info["policy"] == "unless-stopped"
    assert info["reason"] == "", "quando va tutto bene non c'e' niente da spiegare"


@pytest.mark.parametrize("politica", ["no", "on-failure"])
async def test_senza_chi_lo_riaccenda_il_pulsante_sparisce(dentro_un_container, monkeypatch,
                                                           politica):
    async def _politica(cid):
        return politica
    monkeypatch.setattr(restart, "_politica_di_riavvio", _politica)
    info = await restart.stato()
    assert info["available"] is False
    assert politica in info["reason"]
    assert "unless-stopped" in info["reason"], "va detto anche come si sistema"


async def test_una_politica_non_leggibile_non_si_da_per_buona(dentro_un_container, monkeypatch):
    # Socket Docker non montato: il riavvio resta possibile, ma la pagina lo
    # deve dire invece di promettere che il servizio tornera' su.
    async def _politica(cid):
        return ""
    monkeypatch.setattr(restart, "_politica_di_riavvio", _politica)
    info = await restart.stato()
    assert info["available"] is True
    assert info["policy"] == ""
    assert "non e' stato possibile leggere" in info["reason"]


async def test_un_docker_che_esplode_non_fa_esplodere_la_pagina(dentro_un_container, monkeypatch):
    class _ClientRotto:
        _host = None
        is_local = True

        async def inspect(self, cid):
            raise OSError("socket non montato")

    class _Manager:
        def clients(self):
            return [_ClientRotto()]

    import services.docker_client as dc
    monkeypatch.setattr(dc, "get_docker_manager", lambda: _Manager())
    monkeypatch.setattr(dc, "EngineDockerClient", _ClientRotto)
    assert await restart._politica_di_riavvio("abc123def456") == ""


async def test_linspect_non_sporca_lo_stato_dellhost_docker(monkeypatch):
    # `docker.host_giu` si accende su `last_error` del client. Un
    # inspect fallito non e' un host Docker giu': se lo scrivesse li', il
    # controllo della politica di riavvio farebbe suonare un allarme falso.
    import httpx

    from services.docker_client import EngineDockerClient

    client = EngineDockerClient("locale", socket="/var/run/docker.sock")
    client.last_error = "guasto vero, gia' registrato"

    class _Risposta:
        def raise_for_status(self):
            raise httpx.HTTPError("404 Not Found")

    class _Http:
        async def get(self, *a, **k):
            return _Risposta()

    client._client = _Http()
    with pytest.raises(httpx.HTTPError):
        await client.inspect("non-esiste")
    assert client.last_error == "guasto vero, gia' registrato"
    assert client.ok_once is False, "e nemmeno un inspect riuscito lo dichiara raggiungibile"
