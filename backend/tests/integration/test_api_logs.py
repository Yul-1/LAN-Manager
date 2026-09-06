"""
Endpoint dei log (/api/logs/*).

Il punto che conta di piu' qui sono i **permessi**. Il syslog del router e' una
cosa; il registro di audit — che dice chi ha aperto una shell e quando — e il
log del backend sono un'altra: quelle vogliono una sessione vera, senza le
deroghe di `auth.method` o `bypass_lan`, esattamente come il terminale e i tool
di rete: e' una regola di progetto.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.fakes import FakeSSH

pytestmark = pytest.mark.integration

ORA = int(time.time() * 1000)

SENSIBILI = ["/api/logs/?source=audit", "/api/logs/?source=backend",
             "/api/logs/?source=journal:192.0.2.10"]


@pytest.fixture(autouse=True)
def niente_ssh_vero(monkeypatch):
    """Nessun test di questo file deve poter aprire una connessione vera: senza
    questo, la sorgente `router` va a bussare all'SSH configurato e il giro di
    test resta appeso finche' non scade il timeout."""
    monkeypatch.setattr("services.openwrt._ssh", FakeSSH())


@pytest.fixture
def audit_finto(tmp_path, monkeypatch):
    """Un registro di audit vero su file temporaneo, con dentro una riga che
    contiene un segreto: e' il caso da cui l'endpoint deve difendere."""
    from config import settings
    f = tmp_path / "audit.log"
    f.write_text(
        "2026-09-04T18:22:01+0200 terminal.aperta ip=198.51.100.5 utente=user\n"
        '2026-09-04T18:22:09+0200 terminal.comando ip=198.51.100.5 passphrase="segreto vero"\n'
        "2026-09-04T18:23:00+0200 terminal.negata ip=203.0.113.9 motivo=sessione\n")
    monkeypatch.setattr(settings, "audit_log", str(f))
    return f


# ── Permessi ───────────────────────────────────────────────────────

@pytest.mark.parametrize("rotta", SENSIBILI)
def test_le_sorgenti_sensibili_vogliono_una_sessione(client, rotta):
    assert client.get(rotta).status_code == 401


@pytest.mark.parametrize("rotta", SENSIBILI)
def test_nemmeno_dalla_lan_si_leggono_senza_sessione(lan_client, monkeypatch, rotta):
    # `bypass_lan` apre le API a chi sta in LAN, ma la "LAN" comprende subnet
    # segmentate e client VPN (pentest 2026-08-17): l'audit no.
    from config import settings
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    assert lan_client.get(rotta).status_code == 401


def test_il_syslog_del_router_resta_com_era(auth_client):
    # Nessuna regressione sulla sorgente che c'era gia'.
    r = auth_client.get("/api/logs/")
    assert r.status_code == 200
    assert r.json()["source"] == "router"


def test_l_elenco_delle_sorgenti_richiede_la_sola_auth_normale(auth_client):
    r = auth_client.get("/api/logs/sources")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["sources"]]
    assert "router" in ids and "backend" in ids


# ── Audit: il contenuto non deve tradire segreti ───────────────────

def test_la_passphrase_non_esce_dall_endpoint(auth_client, audit_finto):
    r = auth_client.get("/api/logs/?source=audit")
    assert r.status_code == 200, r.text
    assert "segreto vero" not in r.text, "e' successo davvero"
    assert "passphrase=***" in r.text


def test_l_audit_si_legge_e_i_rifiuti_sono_warning(auth_client, audit_finto):
    dati = auth_client.get("/api/logs/?source=audit").json()
    assert dati["count"] == 3
    assert dati["levels"]["warn"] == 1
    assert [l["src"] for l in dati["lines"]][-1] == "terminal.negata"


def test_un_registro_inesistente_lo_dice_invece_di_fingere_il_vuoto(auth_client,
                                                                    monkeypatch, tmp_path):
    from config import settings
    monkeypatch.setattr(settings, "audit_log", str(tmp_path / "mai-scritto.log"))
    dati = auth_client.get("/api/logs/?source=audit").json()
    assert dati["count"] == 0
    assert "non esiste ancora" in dati["warning"]


# ── Log del backend ────────────────────────────────────────────────

def test_il_log_del_backend_arriva_dal_buffer(auth_client):
    import logging
    from services.log_buffer import get_log_buffer, install, uninstall
    root = logging.getLogger()
    livello = root.level
    root.setLevel(logging.INFO)
    install()
    try:
        logging.getLogger("prova").warning("una riga da mostrare")
        dati = auth_client.get("/api/logs/?source=backend").json()
    finally:
        uninstall()
        get_log_buffer().clear()
        root.setLevel(livello)
    assert any(l["msg"] == "una riga da mostrare" for l in dati["lines"])
    assert dati["levels"]["warn"] >= 1


# ── Validazione ────────────────────────────────────────────────────

@pytest.mark.parametrize("periodo", ["30d", "1", "abc", "1h; DROP TABLE log_lines"])
def test_periodo_non_ammesso_viene_rifiutato(auth_client, periodo):
    assert auth_client.get(f"/api/logs/?period={periodo}").status_code == 400


def test_una_sorgente_sconosciuta_ricade_sul_router(auth_client):
    # Meglio il syslog che un 500: la sorgente arriva da un query param e un
    # segnalibro vecchio non deve rompere la pagina.
    assert auth_client.get("/api/logs/?source=inventata").json()["source"] == "router"


# ── Stream NDJSON ──────────────────────────────────────────────────

def test_lo_stream_dichiara_di_non_essere_bufferizzato(auth_client, audit_finto, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "tail_interval", 1)
    monkeypatch.setattr(settings.logs, "tail_interval_min", 1)
    # Il flusso ha un tetto: qui lo si accorcia, cosi' il test finisce da solo
    # invece di dipendere dal fatto che il client riesca a farsi sentire.
    monkeypatch.setattr(settings.logs, "tail_max_seconds", 1)
    with auth_client.stream("GET", "/api/logs/stream?source=audit") as r:
        assert r.status_code == 200
        # nginx bufferizza le risposte proxate: senza questo header le righe
        # arriverebbero tutte insieme alla fine, cioe' mai.
        assert r.headers["x-accel-buffering"] == "no"
        assert r.headers["content-type"].startswith("application/x-ndjson")
        righe = [json.loads(l) for l in r.iter_lines() if l.strip()]
    assert righe[0]["type"] == "start" and righe[0]["source"] == "audit"
    assert righe[-1]["type"] == "end", "un flusso scaduto deve dirlo"


def test_lo_stream_dell_audit_vuole_la_sessione(client):
    assert client.get("/api/logs/stream?source=audit").status_code == 401


# ── La sezione `logs` di config.yaml deve esistere davvero ─────────

def test_la_sezione_logs_non_viene_scartata_in_silenzio():
    """`Settings` ha `extra="ignore"`: una sezione non dichiarata come campo
    viene buttata via senza dire niente. E' gia' successo con `alerts`,
    quindi il test esiste apposta."""
    from config import Settings
    s = Settings(logs={"buffer_lines": 42, "audit_retain_days": 5})
    assert s.logs.buffer_lines == 42
    assert s.logs.audit_retain_days == 5
