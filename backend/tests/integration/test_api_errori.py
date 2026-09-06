"""
Cosa arriva al client quando qualcosa fallisce davvero.

Prima di questa fase un'eccezione non prevista usciva come "Internal Server
Error" in text/plain, senza una riga di log nostra e senza stack: dal browser
era indistinguibile da un guasto di rete, e da `docker logs` non si risalgeva
alla richiesta. Qui i connettori sollevano sul serio, cosi' il ramo d'errore
di ogni router viene eseguito almeno una volta.
"""
from __future__ import annotations

import logging

import pytest

from tests.fakes import FakeDockerManagerRotto, FakeRouterClientRotto, FakeSSHRotto

pytestmark = pytest.mark.integration


@pytest.fixture
def client_500(app, monkeypatch):
    """Client che NON rilancia l'eccezione del server.

    Il TestClient di default la ripropaga al test: comodo per il debug, ma qui
    l'oggetto in esame e' proprio la risposta che riceve il browser, che
    l'eccezione non la vede mai."""
    import main
    from fastapi.testclient import TestClient
    from config import settings
    from middleware.auth import COOKIE, make_token

    monkeypatch.setattr(main, "_bootstrap_secret_key", lambda: None)
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set(COOKIE, make_token(settings.auth.username))
    return c


@pytest.fixture
def connettori_rotti(monkeypatch):
    """Come `connettori_finti`, ma ogni sorgente solleva."""
    monkeypatch.setattr("services.openwrt._router", FakeRouterClientRotto())
    monkeypatch.setattr("services.openwrt._ssh", FakeSSHRotto())
    monkeypatch.setattr("services.docker_client._manager", FakeDockerManagerRotto())


# ── Handler globale ────────────────────────────────────────────────

def test_un_connettore_che_solleva_produce_un_500_in_json(client_500, connettori_rotti):
    r = client_500.get("/api/system/info")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    corpo = r.json()
    assert "riferimento" in corpo["detail"]
    assert len(corpo["request_id"]) == 8


def test_il_500_non_rivela_l_eccezione_ma_il_log_si(client_500, connettori_rotti, caplog):
    # `/api/logs/` (sorgente router) e' ancora una rotta che propaga il guasto
    # SSH invece di degradare: ora lo fa solo lei, le sorgenti nuove
    # rispondono 200 con il motivo scritto dentro.
    with caplog.at_level(logging.ERROR, logger="lanmng"):
        r = client_500.get("/api/logs/")
    assert r.status_code == 500
    # Il motivo vero non deve uscire verso il client...
    assert "SSH" not in r.text
    # ...ma deve essere nei log, con lo stack e con lo stesso riferimento.
    rid = r.json()["request_id"]
    riga = next(x for x in caplog.records if rid in x.getMessage())
    assert riga.exc_info is not None, "senza stack il log non serve a niente"
    assert "connessione SSH rifiutata" in caplog.text


def test_il_riferimento_cambia_ad_ogni_richiesta(client_500, connettori_rotti):
    a = client_500.get("/api/system/info").json()["request_id"]
    b = client_500.get("/api/system/info").json()["request_id"]
    assert a != b, "un riferimento fisso non permette di isolare una richiesta nei log"


# ── Validazione ────────────────────────────────────────────────────

def test_il_422_ha_un_detail_leggibile_e_la_lista_a_parte(auth_client):
    r = auth_client.post("/api/services/config", json={"method": "tcp", "name": "x",
                                                       "host": "192.0.2.1", "port": "non-un-numero"})
    assert r.status_code == 422
    corpo = r.json()
    # Prima `detail` era una lista: interpolarla nel frontend stampava
    # "[object Object]" al posto del motivo.
    assert isinstance(corpo["detail"], str)
    assert "port" in corpo["detail"]
    assert isinstance(corpo["errors"], list)


# ── Rate limit ─────────────────────────────────────────────────────

def test_il_429_dice_quanto_attendere(auth_client, scanner_finto):
    assert auth_client.post("/api/devices/scan").status_code == 200
    r = auth_client.post("/api/devices/scan")
    assert r.status_code == 429
    # Senza questo header il client puo' solo dire "troppe richieste" e
    # l'utente ritenta a caso.
    assert int(r.headers["Retry-After"]) > 0


# ── WebSocket ──────────────────────────────────────────────────────

def test_il_ws_dice_perche_rifiuta(client, origine_estranea):
    """Origine e sessione producevano lo stesso 1008 muto: il client ritentava
    ogni due secondi anche quando insistere era inutile."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers=origine_estranea):
            pass
    assert e.value.code == 1008
    assert e.value.reason == "origine"


def test_il_ws_non_resta_appeso_se_lo_snapshot_solleva(client, monkeypatch):
    """Il try catturava solo WebSocketDisconnect: un'eccezione dello snapshot
    lasciava il socket in `active` per sempre, e ogni broadcast successivo ci
    sbatteva contro."""
    import main

    async def _esplode():
        raise RuntimeError("snapshot rotto")

    monkeypatch.setattr(main.collector, "get_snapshot", _esplode)
    prima = len(main.manager.active)
    try:
        with client.websocket_connect("/ws"):
            pass
    except Exception:
        pass
    assert len(main.manager.active) == prima
