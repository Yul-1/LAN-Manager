"""
Flusso di login, difesa dal brute-force e WebSocket.

Il rate limit sul login e' l'unica barriera contro un attacco a dizionario: la
password admin e' una sola e il servizio, pur essendo in LAN, e' raggiungibile
anche da subnet segmentate e da client VPN.
"""
from __future__ import annotations

import pytest

from services.i18n import t

from config import settings
from middleware.auth import COOKIE
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.integration


def _login(client, password=TEST_PASSWORD, username="admin"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ── Login ──────────────────────────────────────────────────────────

def test_login_corretto_rilascia_il_cookie(client):
    r = _login(client)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert COOKIE in r.cookies


def test_il_cookie_e_httponly_e_samesite_lax(client):
    # httponly: non leggibile da JavaScript, quindi una XSS non ruba la sessione.
    intestazione = _login(client).headers["set-cookie"].lower()
    assert "httponly" in intestazione
    assert "samesite=lax" in intestazione


def test_dopo_il_login_le_api_rispondono(client):
    assert client.get("/api/devices/").status_code == 401
    _login(client)
    assert client.get("/api/snapshot").status_code == 200


def test_lo_stato_riflette_la_sessione(client):
    _login(client)
    corpo = client.get("/api/auth/status").json()
    assert corpo["authenticated"] is True and corpo["session"] is True


def test_password_sbagliata_respinta(client):
    r = _login(client, password="non-e-questa")
    assert r.status_code == 401
    assert r.json()["detail"] == t("err.credenziali", lingua="en")
    assert COOKIE not in r.cookies


def test_utente_sbagliato_respinto(client):
    assert _login(client, username="root").status_code == 401


def test_password_vuota_respinta(client):
    assert _login(client, password="").status_code == 401


def test_il_messaggio_di_errore_non_distingue_utente_da_password(client):
    # Distinguerli direbbe a un attaccante quale meta' ha indovinato.
    a = _login(client, username="root", password=TEST_PASSWORD).json()["detail"]
    b = _login(client, username="admin", password="sbagliata").json()["detail"]
    assert a == b


def test_logout_toglie_la_sessione(client):
    _login(client)
    assert client.get("/api/snapshot").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/snapshot").status_code == 401


# ── Rate limit ─────────────────────────────────────────────────────

def test_dopo_cinque_tentativi_falliti_il_sesto_e_429(client):
    for i in range(5):
        assert _login(client, password=f"sbagliata-{i}").status_code == 401
    r = _login(client, password="sbagliata-6")
    assert r.status_code == 429
    assert r.json()["detail"] == t("err.troppiTentativi", lingua="en")


def test_il_limite_blocca_anche_la_password_giusta(client):
    # Altrimenti basterebbe indovinare per uscire dal blocco.
    for i in range(5):
        _login(client, password=f"sbagliata-{i}")
    assert _login(client).status_code == 429


def test_un_login_riuscito_riapre_la_finestra(client):
    for i in range(3):
        _login(client, password=f"sbagliata-{i}")
    assert _login(client).status_code == 200
    for i in range(5):
        assert _login(client, password=f"ancora-{i}").status_code == 401


def test_il_limite_non_perdura_fra_i_test(client):
    # Se questo fallisce, il reset dei RateLimiter nel conftest ha smesso di
    # funzionare e i test si influenzano a vicenda.
    assert _login(client, password="sbagliata").status_code == 401


def test_lo_scan_ha_un_cooldown(auth_client, scanner_finto):
    assert auth_client.post("/api/devices/scan").status_code == 200
    assert auth_client.post("/api/devices/scan").status_code == 429
    assert scanner_finto.chiamate == 1


# ── WebSocket ──────────────────────────────────────────────────────

def test_il_websocket_richiede_la_sessione(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_il_websocket_rifiuta_unorigine_estranea(client, origine_estranea):
    # Il middleware non vede i WebSocket: il controllo e' fatto a mano nel handler.
    _login(client)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers=origine_estranea):
            pass


def test_con_la_sessione_il_websocket_manda_lo_snapshot(client):
    _login(client)
    with client.websocket_connect("/ws") as ws:
        messaggio = ws.receive_json()
    assert messaggio["type"] == "snapshot"
    assert messaggio["data"]["meta"] == {"origine": "test"}


def test_il_websocket_del_terminale_richiede_la_sessione(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/terminal"):
            pass
