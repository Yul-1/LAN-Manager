"""
Chi compare fra i "servizi in evidenza" della dashboard.

Prima la scelta non esisteva: la dashboard mostrava i container **pinnati** piu'
le unit **critiche**, cioe' due flag nati per altro (il pin ordina la tabella,
`critical` alza l'alert `services.systemd_giu`) usati come se dicessero "voglio
vederlo in prima pagina". Gli healthcheck non comparivano mai.

Ora c'e' un campo suo, `dashboard`, in tutte le famiglie di
`config/services.yaml`. Il rischio che questi test coprono e' uno solo: il
`services.yaml` gia' in esercizio su homeserver **non ha quella chiave**, e un
default sbagliato svuoterebbe la dashboard del proprietario al primo
aggiornamento, senza che nessuno abbia chiesto niente.
"""
from __future__ import annotations

import pytest

from routers.services import ServiceEntry, _entry_for_kind
from services.docker_client import ContainerInfo
from services.healthcheck import _result
from services.services_overview import _docker_section
from services.systemd_monitor import _from_props, _unavailable
from services.windows_services import _base as _voce_windows

PROPS_ATTIVA = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running"}


def _container(name: str, running: bool = True) -> ContainerInfo:
    return ContainerInfo(id="a" * 12, name=name, image="img:1",
                         status="running" if running else "exited",
                         state="Up 3 days", host="homeserver")


async def _sezione_docker(pinned, containers):
    return await _docker_section(pinned, containers)


# ── Il catalogo scritto prima che la scelta esistesse ──────────────

def test_unit_senza_la_chiave_segue_critical():
    """E' cio' che la dashboard mostrava prima: solo le unit critiche."""
    critica = _from_props({"unit": "docker.service", "critical": True}, PROPS_ATTIVA)
    normale = _from_props({"unit": "cron.service", "critical": False}, PROPS_ATTIVA)
    assert critica.to_dict()["dashboard"] is True
    assert normale.to_dict()["dashboard"] is False


def test_unit_irraggiungibile_conserva_la_scelta():
    """Una unit che non risponde deve restare in pagina: sparire proprio quando
    e' caduta e' l'esatto contrario di quello che serve."""
    assert _unavailable({"unit": "x.service", "critical": True}).to_dict()["dashboard"] is True


def test_healthcheck_senza_la_chiave_resta_fuori():
    """Prima non ne compariva nessuno: il default non deve aggiungerne."""
    r = _result({"name": "Router LuCI", "type": "http", "url": "http://192.0.2.1"},
                ok=True, latency_ms=3.0, detail="HTTP 200")
    assert r.to_dict()["dashboard"] is False


@pytest.mark.asyncio
async def test_container_pinnato_senza_la_chiave_resta_in_dashboard():
    sez = await _sezione_docker([{"name": "portainer", "label": "Portainer"}],
                                [_container("portainer"), _container("altro")])
    per_nome = {c["name"]: c for c in sez["containers"]}
    assert per_nome["portainer"]["dashboard"] is True
    # Un container mai pinnato non e' mai stato scelto da nessuno.
    assert per_nome["altro"]["dashboard"] is False


# ── La scelta esplicita vince sul default ──────────────────────────

@pytest.mark.asyncio
async def test_pinnato_ma_escluso_a_mano():
    """`pinned` e `dashboard` restano due cose distinte: il pin ordina la
    tabella di Monitoraggio, la spunta decide la prima pagina."""
    sez = await _sezione_docker([{"name": "portainer", "dashboard": False}],
                                [_container("portainer")])
    c = sez["containers"][0]
    assert c["pinned"] is True and c["dashboard"] is False


def test_unit_non_critica_ma_scelta():
    """E il contrario: si vuole vedere senza che suoni un allarme se cade."""
    u = _from_props({"unit": "cron.service", "critical": False, "dashboard": True},
                    PROPS_ATTIVA).to_dict()
    assert u["dashboard"] is True and u["critical"] is False


def test_healthcheck_scelto():
    r = _result({"name": "DNS interno", "type": "tcp", "host": "192.0.2.1", "port": 53,
                 "dashboard": True}, ok=True, latency_ms=1.0, detail="connect ok")
    assert r.to_dict()["dashboard"] is True


def test_un_servizio_windows_critico_non_eredita_la_dashboard():
    """Al contrario di systemd: li' la chiave assente vale `critical` per non
    cambiare un catalogo gia' in esercizio, qui non ce n'e' nessuno da
    rispettare, e un servizio critico non deve invitarsi in prima pagina."""
    critico = _voce_windows({"name": "Spooler", "host": "192.0.2.12", "critical": True})
    assert critico.critical is True
    assert critico.to_dict()["dashboard"] is False


def test_un_servizio_windows_scelto():
    scelto = _voce_windows({"name": "Spooler", "host": "192.0.2.12", "dashboard": True})
    assert scelto.to_dict()["dashboard"] is True


# ── La scrittura dalla UI ──────────────────────────────────────────

@pytest.mark.parametrize("body", [
    ServiceEntry(kind="docker", name="portainer", dashboard=False),
    ServiceEntry(kind="systemd", unit="cron.service", dashboard=False),
    ServiceEntry(kind="windows_service", name="Spooler", host="192.0.2.12", dashboard=False),
    ServiceEntry(kind="http", name="LuCI", type="http", url="http://192.0.2.1", dashboard=False),
])
def test_la_spunta_tolta_viene_persistita(body):
    """`_entry_for_kind` scarta i campi vuoti prima di salvare: se `False`
    finisse nello scarto, togliere la spunta non avrebbe alcun effetto e il
    servizio ricadrebbe nel default tornando in dashboard da solo."""
    assert _entry_for_kind(body)["dashboard"] is False


def test_la_spunta_messa_viene_persistita():
    e = _entry_for_kind(ServiceEntry(kind="systemd", unit="cron.service", dashboard=True))
    assert e["dashboard"] is True
