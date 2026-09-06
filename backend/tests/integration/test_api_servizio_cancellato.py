"""
La DELETE di un servizio, end-to-end sull'app vera.

Il difetto segnalato dal proprietario il 2026-09-04: cancellando un servizio la
riga spariva e tornava un attimo dopo, perche' lo snapshot del collector — che
il giro veloce ribroadcasta ogni 10 secondi — continuava a contenerlo fino al
giro lento successivo (60s).

Qui si prova che la cancellazione **arriva allo snapshot dentro la richiesta**,
e che una cancellazione a vuoto non si dichiara riuscita.
"""
from __future__ import annotations

import pytest

from services.collector import get_collector
from services.service_store import get_service_store

pytestmark = pytest.mark.integration


@pytest.fixture
def catalogo_di_prova(monkeypatch):
    """Una voce nel catalogo e la stessa voce nello snapshot, come in esercizio."""
    store = get_service_store()
    store.add("systemd", {"unit": "prova.service", "host": ""})
    coll = get_collector()
    coll._snapshot["services"] = {
        "docker": {"containers": [], "summary": {"running": 0, "stopped": 0, "total": 0}},
        "systemd": [{"name": "prova.service", "ok": True},
                    {"name": "ssh.service", "ok": True}],
        "windows_services": [],
        "healthchecks": [],
        "summary": {"total": 2, "ok": 2, "down": 0},
    }
    yield store
    store.remove("systemd", "prova.service")
    coll._snapshot.pop("services", None)


def test_la_cancellazione_arriva_allo_snapshot_dentro_la_richiesta(
        auth_client, catalogo_di_prova):
    r = auth_client.delete("/api/services/config/systemd/prova.service")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    # Nessuna attesa, nessun giro lento: e' gia' fuori.
    servizi = get_collector().snapshot_ora()["services"]
    assert [x["name"] for x in servizi["systemd"]] == ["ssh.service"]
    assert servizi["summary"]["total"] == 1
    # E anche dal catalogo su disco, che era gia' l'unica cosa che funzionava.
    assert "prova.service" not in [u.get("unit") for u in
                                   catalogo_di_prova.read()["systemd"]]


def test_cancellare_qualcosa_che_non_c_e_risponde_404(auth_client, catalogo_di_prova):
    """Prima rispondeva 200 con `count: 0`, e la pagina — che guarda solo lo
    stato — dichiarava riuscita una cancellazione mai avvenuta."""
    r = auth_client.delete("/api/services/config/systemd/mai-esistita.service")
    assert r.status_code == 404
    # Lo snapshot non si tocca: non e' stato tolto niente.
    servizi = get_collector().snapshot_ora()["services"]
    assert len(servizi["systemd"]) == 2


def test_un_kind_sconosciuto_resta_un_400(auth_client):
    r = auth_client.delete("/api/services/config/chissa/qualcosa")
    assert r.status_code == 400
