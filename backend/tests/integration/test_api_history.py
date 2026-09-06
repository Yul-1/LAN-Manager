"""
Endpoint dello storico (/api/history/*).

Verificano il contratto su cui si appoggia il frontend: i punti escono a passo
fisso su tutta la finestra, i periodi ammessi sono un elenco chiuso, e nulla
risponde senza sessione.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

ORA = int(time.time() * 1000)

ROTTE = [
    "/api/history/traffic?period=1h",
    "/api/history/resources?host=192.0.2.10&period=1h",
    "/api/history/device?key=192.0.2.5&period=24h",
    "/api/history/service?kind=systemd&name=ssh.service&host=192.0.2.10&period=24h",
]


@pytest.fixture
def storico(monkeypatch):
    """Storico vero (su file temporaneo, via LAN_HISTORY_DB del conftest) con
    qualche punto dentro. Cadenze realistiche: la config della suite mette gli
    intervalli a un giorno per non far partire il collector."""
    from config import settings
    from services.history_store import get_history
    monkeypatch.setattr(settings, "collect_interval_fast", 10)
    monkeypatch.setattr(settings.host_metrics, "interval", 60)
    st = get_history()
    for i in range(30):
        t = ORA - (30 - i) * 10_000
        st.add_traffic({"t": t, "rx_bps": 100.0 * i, "tx_bps": 50.0 * i, "latency": 5.0})
        st.add_host_points({"192.0.2.10": {"t": t, "cpu": 10.0, "mem": 20.0,
                                           "swap": 0.0, "load1": 0.5, "temp": 45.0}})
    st.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": True}])
    st.mark_services({"systemd": [{"kind": "systemd", "name": "ssh.service",
                                   "host": "192.0.2.10", "ok": True}]})
    return st


# ── Accesso ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rotta", ROTTE)
def test_senza_sessione_risponde_401(client, rotta):
    assert client.get(rotta).status_code == 401


@pytest.mark.parametrize("rotta", ROTTE)
def test_con_sessione_risponde_200(auth_client, storico, rotta):
    r = auth_client.get(rotta)
    assert r.status_code == 200, r.text


# ── Contratto dei dati ─────────────────────────────────────────────

def test_i_punti_del_traffico_sono_a_passo_fisso(auth_client, storico):
    p = auth_client.get("/api/history/traffic?period=1h").json()["points"]
    assert len(p) > 1
    passi = {p[i + 1]["t"] - p[i]["t"] for i in range(len(p) - 1)}
    assert len(passi) == 1, f"passo non costante: {passi}"
    assert set(p[0]) == {"t", "rx_bps", "tx_bps", "latency"}


def test_la_finestra_vuota_esce_piena_di_buchi(auth_client, storico):
    """Sette giorni con dati solo negli ultimi cinque minuti: il resto deve
    restare vuoto e visibile, non sparire."""
    p = auth_client.get("/api/history/traffic?period=7d").json()["points"]
    assert len(p) > 100
    assert any(x["rx_bps"] is None for x in p)


def test_le_risorse_escono_nella_forma_dei_punti_vivi(auth_client, storico):
    p = auth_client.get("/api/history/resources?host=192.0.2.10&period=1h").json()["points"]
    assert set(p[0]) == {"t", "cpu", "mem", "swap", "load1", "temp"}


def test_host_sconosciuto_non_e_un_errore_ma_una_serie_vuota(auth_client, storico):
    r = auth_client.get("/api/history/resources?host=198.51.100.99&period=1h")
    assert r.status_code == 200
    assert all(x["cpu"] is None for x in r.json()["points"])


def test_gli_eventi_del_device_sono_booleani(auth_client, storico):
    e = auth_client.get("/api/history/device?key=192.0.2.5&period=24h").json()["events"]
    assert e and e[0]["online"] is True


def test_il_servizio_e_identificato_anche_dall_host(auth_client, storico):
    """Chiedendo lo stesso nome su un altro host non si devono vedere i suoi
    eventi: sono due servizi diversi."""
    altro = auth_client.get("/api/history/service?kind=systemd&name=ssh.service"
                            "&host=198.51.100.1&period=24h").json()
    assert altro["events"] == []


# ── Validazione ────────────────────────────────────────────────────

@pytest.mark.parametrize("periodo", ["30d", "1", "", "99999999", "1h; DROP TABLE traffic"])
def test_periodo_non_ammesso_viene_rifiutato(auth_client, storico, periodo):
    r = auth_client.get(f"/api/history/traffic?period={periodo}")
    assert r.status_code == 400


def test_le_tabelle_sopravvivono_a_un_periodo_malevolo(auth_client, storico):
    auth_client.get("/api/history/traffic?period=1h';DROP TABLE traffic;--")
    assert auth_client.get("/api/history/traffic?period=1h").status_code == 200


def test_host_obbligatorio_sulle_risorse(auth_client, storico):
    assert auth_client.get("/api/history/resources?period=1h").status_code == 422


# ── Stati correnti ("da quando") ───────────────────────────────────

def test_stati_richiede_la_sessione(client):
    assert client.get("/api/history/states").status_code == 401


def test_stati_riporta_ultima_transizione_di_ognuno(auth_client, storico):
    d = auth_client.get("/api/history/states").json()
    dev = {x["key"]: x for x in d["devices"]}
    assert dev["192.0.2.5"]["online"] is True
    assert dev["192.0.2.5"]["since"] > 0
    svc = [x for x in d["services"] if x["name"] == "ssh.service"]
    assert len(svc) == 1 and svc[0]["ok"] is True and svc[0]["host"] == "192.0.2.10"


def test_stati_vede_anche_transizioni_fuori_finestra(auth_client, storico):
    """E' il caso interessante: "giu' da tre settimane" non deve sparire solo
    perche' l'evento e' piu' vecchio di qualunque periodo selezionabile."""
    storico.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": False}])
    storico._db.execute("UPDATE device_events SET t = ? WHERE online = 0",
                        (ORA - 21 * 86400 * 1000,))
    storico._db.commit()
    dev = {x["key"]: x for x in auth_client.get("/api/history/states").json()["devices"]}
    assert dev["192.0.2.5"]["online"] is False
    assert dev["192.0.2.5"]["since"] < ORA - 20 * 86400 * 1000
