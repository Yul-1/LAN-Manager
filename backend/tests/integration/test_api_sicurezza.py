"""
Proprieta' di sicurezza delle API, con TestClient sull'app vera.

Chiude, come test ripetibili, la remediation del pentest 2026-08-17: prima di
allora config, dispositivi, snapshot, servizi, rete e Docker rispondevano 200
senza credenziali, e il finding era sopravvissuto un mese perche' lo stato
insicuro non era visibile da nessuna parte.

Nessuna di queste rotte arriva al connettore: le risposte 401/403/429/503 le
produce il middleware o la dependency, prima del corpo dell'handler.
"""
from __future__ import annotations

import pytest

from config import settings
from middleware.auth import COOKIE, make_token

pytestmark = pytest.mark.integration

# Un rappresentante per gruppo di endpoint.
ROTTE_PROTETTE = [
    "/api/system/info",
    "/api/devices/",
    "/api/services/",
    "/api/docker/containers",
    "/api/wan/status",
    "/api/logs/",
    "/api/wireguard/status",
    "/api/config/",
    "/api/host/network",
    "/api/alerts/rules",
    "/api/snapshot",
]

# Rotte con `Depends(require_session)`: sessione sempre richiesta, senza deroghe.
ROTTE_A_SESSIONE = ["/api/tools/", "/api/terminal/hosts"]


# ── Rotte aperte per necessita' ────────────────────────────────────

def test_health_risponde_senza_credenziali(client):
    # E' il gancio dello smoke post-deploy: deve restare raggiungibile.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"]


def test_auth_status_risponde_senza_credenziali(client):
    # La pagina di login deve poter sapere se serve il login.
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["auth_required"] is True
    assert corpo["authenticated"] is False
    assert corpo["session"] is False


def test_auth_status_non_espone_l_hash_della_password(client):
    corpo = client.get("/api/auth/status").json()
    assert corpo["password_set"] is True
    assert "$2b$" not in str(corpo)


def test_in_configurazione_chiusa_non_ci_sono_avvisi(client):
    assert client.get("/api/auth/status").json()["insecure"] == []


def test_una_configurazione_permissiva_si_vede_nello_stato(client, monkeypatch):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert client.get("/api/auth/status").json()["insecure"] != []


# ── 401 senza sessione ─────────────────────────────────────────────

@pytest.mark.parametrize("rotta", ROTTE_PROTETTE)
def test_senza_cookie_ogni_gruppo_risponde_401(client, rotta):
    r = client.get(rotta)
    assert r.status_code == 401, f"{rotta} risponde {r.status_code}"
    assert r.json()["detail"] == "non autenticato"


def test_un_cookie_non_firmato_non_basta(client):
    client.cookies.set(COOKIE, "admin.9999999999.firma-inventata")
    assert client.get("/api/devices/").status_code == 401


def test_con_un_cookie_valido_lo_snapshot_risponde(auth_client):
    r = auth_client.get("/api/snapshot")
    assert r.status_code == 200
    assert r.json()["meta"] == {"origine": "test"}      # lo snapshot iniettato


def test_le_scritture_senza_sessione_sono_respinte(client):
    assert client.post("/api/devices/scan").status_code == 401
    assert client.put("/api/config/", json={}).status_code == 401
    assert client.delete("/api/devices/192.0.2.10").status_code == 401


# ── bypass_lan ─────────────────────────────────────────────────────

def test_il_bypass_lan_non_scatta_per_un_client_non_ip(client, monkeypatch):
    # Sotto TestClient l'host e' la stringa "testclient": is_lan la rifiuta.
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert client.get("/api/devices/").status_code == 401


def test_il_bypass_lan_apre_le_api_a_un_client_privato(lan_client, monkeypatch):
    # Il motivo per cui resta spento in produzione: la "LAN" comprende subnet
    # segmentate e client VPN, come ha dimostrato il pentest dalla Kali.
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get("/api/devices/").status_code != 401


# ── require_session: nessuna deroga ────────────────────────────────

@pytest.mark.parametrize("rotta", ROTTE_A_SESSIONE)
def test_le_rotte_sensibili_esigono_la_sessione(client, rotta):
    r = client.get(rotta)
    assert r.status_code == 401


@pytest.mark.parametrize("rotta", ROTTE_A_SESSIONE)
def test_le_rotte_sensibili_esigono_la_sessione_anche_ad_auth_spenta(
        client, monkeypatch, rotta):
    # Dare a chiunque sia in LAN una shell SSH non e' come mostrargli i device.
    monkeypatch.setattr(settings.auth, "method", "none")
    assert client.get("/api/devices/").status_code == 200      # auth globale spenta
    assert client.get(rotta).status_code == 401                 # ma qui no


@pytest.mark.parametrize("rotta", ROTTE_A_SESSIONE)
def test_le_rotte_sensibili_esigono_la_sessione_anche_col_bypass_lan(
        lan_client, monkeypatch, rotta):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get(rotta).status_code == 401


@pytest.mark.parametrize("rotta", ROTTE_A_SESSIONE)
def test_senza_password_admin_le_rotte_sensibili_rispondono_503(
        auth_client, monkeypatch, rotta):
    # Serve il cookie: senza, il middleware risponde 401 prima che
    # require_session possa arrivare al suo 503.
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH", raising=False)
    monkeypatch.setattr(settings.auth, "password_hash", "")
    r = auth_client.get(rotta)
    assert r.status_code == 503
    assert "password admin" in r.json()["detail"]


def test_con_la_sessione_le_rotte_sensibili_rispondono(auth_client):
    assert auth_client.get("/api/tools/").status_code == 200
    assert auth_client.get("/api/terminal/hosts").status_code == 200


# ── CSRF ───────────────────────────────────────────────────────────

def test_una_origine_estranea_sulle_scritture_e_respinta(auth_client, origine_estranea):
    r = auth_client.post("/api/devices/scan", headers=origine_estranea)
    assert r.status_code == 403
    assert r.json()["detail"] == "origine non consentita"


def test_lorigine_estranea_e_respinta_anche_ad_auth_spenta(
        client, monkeypatch, origine_estranea):
    monkeypatch.setattr(settings.auth, "method", "none")
    assert client.post("/api/devices/scan", headers=origine_estranea).status_code == 403


@pytest.mark.parametrize("metodo", ["post", "put", "patch", "delete"])
def test_tutti_i_metodi_mutanti_sono_coperti(auth_client, origine_estranea, metodo):
    r = getattr(auth_client, metodo)("/api/config/", headers=origine_estranea)
    assert r.status_code == 403


def test_le_letture_non_sono_bloccate_dallorigine(auth_client, origine_estranea):
    # CSRF riguarda gli effetti collaterali: una GET non ne ha.
    assert auth_client.get("/api/snapshot", headers=origine_estranea).status_code == 200


def test_lorigine_dello_stesso_host_passa(auth_client, origine_valida, scanner_finto):
    r = auth_client.post("/api/devices/scan", headers=origine_valida)
    assert r.status_code != 403


def test_senza_header_origin_la_scrittura_passa(auth_client, scanner_finto):
    # Comportamento voluto: un client non-browser (curl, lo smoke script) non e'
    # un vettore CSRF. Fissato qui perche' e' controintuitivo e "correggerlo"
    # romperebbe lo smoke post-deploy.
    assert auth_client.post("/api/devices/scan").status_code != 403
