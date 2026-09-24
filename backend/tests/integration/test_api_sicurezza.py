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

from services.i18n import t

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
    assert r.json()["detail"] == t("err.nonAutenticato", lingua="en")


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


def test_il_bypass_lan_apre_le_api_a_un_client_delle_reti_fidate(lan_client, monkeypatch):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get("/api/devices/").status_code != 401


def test_il_bypass_lan_non_vale_per_un_privato_fuori_dalle_reti_fidate(
        client_rete_estranea, monkeypatch):
    # Prima bastava un indirizzo privato qualsiasi: subnet segmentate e client
    # VPN entravano senza login (pentest 2026-09-23, dalla subnet delle VM).
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert client_rete_estranea.get("/api/devices/").status_code == 401


def test_senza_reti_esplicite_valgono_le_subnet_configurate(
        client_rete_estranea, monkeypatch):
    from config import SubnetConfig
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    monkeypatch.setattr(settings.auth, "bypass_networks", [])
    monkeypatch.setattr(settings, "subnets", [SubnetConfig(cidr="198.51.100.0/24", label="t")])
    assert client_rete_estranea.get("/api/devices/").status_code == 401
    monkeypatch.setattr(settings, "subnets", [SubnetConfig(cidr="10.99.0.0/24", label="t")])
    assert client_rete_estranea.get("/api/devices/").status_code != 401


def test_il_bootstrap_della_password_resta_nelle_reti_configurate(
        client_rete_estranea, monkeypatch, segreti_su_file_temporaneo):
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH", raising=False)
    monkeypatch.setattr(settings.auth, "password_hash", "")
    r = client_rete_estranea.post("/api/auth/password", json={"password": "prima-password"})
    assert r.status_code == 401
    assert not segreti_su_file_temporaneo.exists()


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
    # La firma copre l'hash admin: il cookie va rifirmato dopo averlo tolto.
    auth_client.cookies.set(COOKIE, make_token(settings.auth.username))
    r = auth_client.get(rotta)
    assert r.status_code == 503
    assert r.json()["detail"] == t("err.servePasswordAdmin", lingua="en")


def test_con_la_sessione_le_rotte_sensibili_rispondono(auth_client):
    assert auth_client.get("/api/tools/").status_code == 200
    assert auth_client.get("/api/terminal/hosts").status_code == 200


# ── Scritture della configurazione: sessione sempre ────────────────
#  Pentest 2026-09-23: con `bypass_lan` un client LAN senza login riscriveva
#  l'hash della password admin da PUT /api/config/secrets.

SCRITTURE_CONFIG = [
    ("put", "/api/config/", {"yaml": "subnets: []\n"}),
    ("put", "/api/config/section/subnets", {"value": []}),
    ("put", "/api/config/secrets", {"values": {"admin_password_hash": "$2b$12$x"}}),
    ("post", "/api/config/backups/config.20260101-000000.bak.yaml/restore", None),
]


@pytest.fixture
def segreti_su_file_temporaneo(tmp_path, monkeypatch):
    percorso = tmp_path / "secrets.env"
    monkeypatch.setenv("LAN_ENV_FILE", str(percorso))
    return percorso


@pytest.mark.parametrize("metodo,rotta,corpo", SCRITTURE_CONFIG)
def test_le_scritture_config_esigono_la_sessione_col_bypass_lan(
        lan_client, monkeypatch, segreti_su_file_temporaneo, metodo, rotta, corpo):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    r = getattr(lan_client, metodo)(rotta, **({"json": corpo} if corpo else {}))
    assert r.status_code == 401
    assert not segreti_su_file_temporaneo.exists()


@pytest.mark.parametrize("metodo,rotta,corpo", SCRITTURE_CONFIG)
def test_le_scritture_config_esigono_la_sessione_ad_auth_spenta(
        client, monkeypatch, segreti_su_file_temporaneo, metodo, rotta, corpo):
    monkeypatch.setattr(settings.auth, "method", "none")
    r = getattr(client, metodo)(rotta, **({"json": corpo} if corpo else {}))
    assert r.status_code == 401
    assert not segreti_su_file_temporaneo.exists()


def test_le_letture_config_restano_sotto_lauth_globale(lan_client, monkeypatch):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get("/api/config/secrets").status_code == 200


def test_un_segreto_sconosciuto_e_rifiutato_senza_scrivere(
        auth_client, segreti_su_file_temporaneo):
    # Il nome della variabile d'ambiente non e' l'id: prima rispondeva 200
    # `changed: []` e chi chiamava credeva di aver salvato.
    r = auth_client.put("/api/config/secrets",
                        json={"values": {"LAN_AUTH__PASSWORD_HASH": "$2b$12$x",
                                         "router_password": "p"}})
    assert r.status_code == 400
    assert "LAN_AUTH__PASSWORD_HASH" in r.json()["detail"]
    assert not segreti_su_file_temporaneo.exists()


def test_con_la_sessione_il_segreto_si_salva(segreti_su_file_temporaneo, auth_client):
    r = auth_client.put("/api/config/secrets", json={"values": {"router_password": "p"}})
    assert r.status_code == 200
    assert r.json()["changed"] == ["router_password"]
    assert "LAN_ROUTER__PASSWORD" in segreti_su_file_temporaneo.read_text()


# ── Cambio password admin: sessione vera ───────────────────────────

def test_col_bypass_lan_la_password_non_si_cambia_senza_sessione(
        lan_client, monkeypatch, segreti_su_file_temporaneo):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    r = lan_client.post("/api/auth/password", json={"password": "attaccante"})
    assert r.status_code == 401
    assert not segreti_su_file_temporaneo.exists()


def test_ad_auth_spenta_la_password_non_si_cambia_senza_sessione(
        client, monkeypatch, segreti_su_file_temporaneo):
    monkeypatch.setattr(settings.auth, "method", "none")
    r = client.post("/api/auth/password", json={"password": "attaccante"})
    assert r.status_code == 401
    assert not segreti_su_file_temporaneo.exists()


def test_con_la_sessione_la_password_si_cambia(segreti_su_file_temporaneo, auth_client):
    # Prima il file temporaneo, poi il client: il cookie di `auth_client` si
    # firma con l'hash admin, e leggerlo crea il secrets store sul percorso
    # corrente di LAN_ENV_FILE.
    r = auth_client.post("/api/auth/password", json={"password": "nuova-password"})
    assert r.status_code == 200
    assert "LAN_AUTH__PASSWORD_HASH" in segreti_su_file_temporaneo.read_text()


def test_cambiare_la_password_revoca_i_cookie_esistenti(
        segreti_su_file_temporaneo, client, monkeypatch):
    # Pentest 2026-09-24 (G9): un cookie rubato restava valido per 7 giorni
    # anche dopo il cambio password. Senza la variabile d'ambiente, che
    # vincerebbe sul file, l'hash corrente e' quello scritto dal cambio.
    from middleware.auth import valid_token
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH")
    vecchio = make_token(settings.auth.username)
    client.cookies.set(COOKIE, vecchio)
    r = client.post("/api/auth/password", json={"password": "nuova-password"})
    assert r.status_code == 200
    nuovo = r.cookies.get(COOKIE)
    assert nuovo and nuovo != vecchio, "chi cambia la password riceve un cookie nuovo"
    assert valid_token(vecchio) is False
    assert valid_token(nuovo) is True


def test_senza_password_il_bootstrap_dalla_lan_resta_possibile(
        lan_client, monkeypatch, segreti_su_file_temporaneo):
    # Primo avvio: non esiste ancora una password con cui aprire una sessione.
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH", raising=False)
    monkeypatch.setattr(settings.auth, "password_hash", "")
    r = lan_client.post("/api/auth/password", json={"password": "prima-password"})
    assert r.status_code == 200


# ── Azioni sull'infrastruttura: sessione sempre ────────────────────

AZIONI = [
    ("/api/docker/containers/abc123/action", {"action": "stop", "host": "local"}),
    ("/api/wireguard/reload", None),
]


@pytest.mark.parametrize("rotta,corpo", AZIONI)
def test_le_azioni_esigono_la_sessione_col_bypass_lan(lan_client, monkeypatch, rotta, corpo):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    r = lan_client.post(rotta, **({"json": corpo} if corpo else {}))
    assert r.status_code == 401


@pytest.mark.parametrize("rotta,corpo", AZIONI)
def test_le_azioni_esigono_la_sessione_ad_auth_spenta(client, monkeypatch, rotta, corpo):
    monkeypatch.setattr(settings.auth, "method", "none")
    r = client.post(rotta, **({"json": corpo} if corpo else {}))
    assert r.status_code == 401


# ── Modifiche di dispositivi, servizi e alert: sessione sempre ─────
#  Pentest 2026-09-24 (G1): coperte solo dal middleware, tornavano scrivibili
#  senza login appena si riattivava `bypass_lan`. Le letture di stato restano
#  aperte al bypass, che serve proprio a quello; il firewall del router no.

MODIFICHE = [
    ("post", "/api/devices/", {"ip": "192.0.2.50", "name": "x"}),
    ("post", "/api/devices/scan", None),
    ("put", "/api/devices/192.0.2.50", {"name": "x"}),
    ("post", "/api/devices/192.0.2.50/hide", None),
    ("post", "/api/devices/192.0.2.50/unhide", None),
    ("delete", "/api/devices/192.0.2.50", None),
    ("post", "/api/services/refresh", None),
    ("post", "/api/services/config", {"kind": "http", "name": "x", "url": "http://192.0.2.9"}),
    ("delete", "/api/services/config/http/x", None),
    ("post", "/api/alerts/silence", {"rule": "x", "reason": "y"}),
    ("get", "/api/wan/firewall", None),
]


@pytest.mark.parametrize("metodo,rotta,corpo", MODIFICHE)
def test_le_modifiche_esigono_la_sessione_col_bypass_lan(lan_client, monkeypatch,
                                                         metodo, rotta, corpo):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    r = lan_client.request(metodo.upper(), rotta, **({"json": corpo} if corpo else {}))
    assert r.status_code == 401


def test_col_bypass_lan_le_letture_di_stato_restano_aperte(lan_client, monkeypatch):
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get("/api/devices/").status_code == 200


# ── Catalogo servizi: SSH solo verso host configurati ──────────────

@pytest.mark.parametrize("corpo", [
    {"kind": "systemd", "unit": "prova-ssh.service", "host": "203.0.113.66"},
    {"kind": "windows_service", "name": "Spooler", "host": "203.0.113.66"},
])
def test_un_servizio_su_un_host_non_configurato_e_rifiutato(auth_client, corpo):
    from services.service_store import get_service_store
    r = auth_client.post("/api/services/config", json=corpo)
    assert r.status_code == 400
    assert "203.0.113.66" in r.json()["detail"]
    assert "203.0.113.66" not in str(get_service_store().read())


def test_un_servizio_su_un_host_configurato_si_salva(auth_client, monkeypatch):
    from services.service_store import get_service_store
    monkeypatch.setattr(settings.router, "host", "198.51.100.1")
    r = auth_client.post("/api/services/config",
                         json={"kind": "systemd", "unit": "prova-ssh.service",
                               "host": "198.51.100.1"})
    try:
        assert r.status_code == 200
    finally:
        get_service_store().remove("systemd", "prova-ssh.service")


# ── Terminale: host e chiavi aggiungibili dalla UI ─────────────────
#  Pentest 2026-09-24: G2 (loopback accettato come host) e G3 (messaggi
#  diversi per file assente / illeggibile = oracolo di esistenza).

def test_il_terminale_non_accetta_host_locali(auth_client):
    from services.ssh_hosts import live_ssh_config
    r = auth_client.post("/api/terminal/hosts", json={"host": "127.0.0.1", "port": 22})
    assert r.status_code == 400
    assert not any(h.ip == "127.0.0.1" for h in live_ssh_config().hosts)


def test_la_chiave_del_terminale_non_rivela_quali_file_esistono(auth_client):
    risposte = [auth_client.post("/api/terminal/hosts",
                                 json={"host": "192.0.2.77", "key": k})
                for k in ("/etc/shadow", "/non/esiste/xyz", "/etc/hostname")]
    assert {r.status_code for r in risposte} == {400}
    assert len({r.json()["detail"] for r in risposte}) == 1


# ── CSRF ───────────────────────────────────────────────────────────

def test_una_origine_estranea_sulle_scritture_e_respinta(auth_client, origine_estranea):
    r = auth_client.post("/api/devices/scan", headers=origine_estranea)
    assert r.status_code == 403
    assert r.json()["detail"] == t("err.origineNonConsentita", lingua="en")


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
