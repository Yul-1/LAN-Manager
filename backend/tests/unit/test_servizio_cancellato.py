"""
Un servizio cancellato deve sparire subito (services/collector.py, routers/services.py).

Il difetto, segnalato dal proprietario il 2026-09-04: cancellando un servizio
dalla pagina, la riga spariva e **tornava un attimo dopo**.

La causa non era nella cancellazione — il catalogo su disco veniva riscritto
bene — ma nel fatto che nessuno lo diceva allo snapshot. `services` si ricalcola
solo nel giro lento (60s), mentre quello veloce ribroadcasta lo snapshot ogni
10s: il client toglieva la riga in via ottimistica, riceveva subito dopo la
vista ancora vecchia e la rimetteva.

`dimentica_servizio` pota lo snapshot dentro la richiesta di cancellazione. Non
ricalcola: di una rimozione si sa esattamente cosa sparisce, e un ricalcolo
vorrebbe dire SSH e probe dentro una DELETE.
"""
from __future__ import annotations

import pytest

from services.collector import DataCollector


def vista():
    """Uno snapshot come quello vero (services/services_overview.py:42-50)."""
    return {
        "docker": {
            "containers": [
                {"name": "nginx", "label": "Reverse proxy", "url": "http://x",
                 "pinned": True, "dashboard": True, "running": True, "host": "srv"},
                {"name": "redis", "label": "redis", "url": "", "pinned": False,
                 "dashboard": False, "running": True, "host": "srv"},
            ],
            "summary": {"running": 2, "stopped": 0, "total": 2},
        },
        "systemd": [{"name": "ssh.service", "ok": True},
                    {"name": "cron.service", "ok": False}],
        "windows_services": [{"name": "Spooler", "ok": True}],
        "healthchecks": [{"name": "Router LuCI", "ok": True}],
        "summary": {"total": 6, "ok": 4, "down": 2},
    }


@pytest.fixture
def collector():
    c = DataCollector()
    c._snapshot["services"] = vista()
    return c


# ── Quello per cui esiste ──────────────────────────────────────────

def test_una_unit_systemd_sparisce_subito_dallo_snapshot(collector):
    assert collector.dimentica_servizio("systemd", "cron.service") is True
    nomi = [x["name"] for x in collector.snapshot_ora()["services"]["systemd"]]
    assert nomi == ["ssh.service"], "e' questa la riga che tornava dopo un attimo"


def test_i_conteggi_seguono_la_rimozione(collector):
    collector.dimentica_servizio("systemd", "cron.service")     # era down
    s = collector.snapshot_ora()["services"]["summary"]
    assert (s["total"], s["ok"], s["down"]) == (5, 4, 1)

    collector.dimentica_servizio("http", "Router LuCI")         # era ok
    s = collector.snapshot_ora()["services"]["summary"]
    assert (s["total"], s["ok"], s["down"]) == (4, 3, 1)


def test_anche_un_servizio_windows(collector):
    assert collector.dimentica_servizio("windows_service", "Spooler") is True
    assert collector.snapshot_ora()["services"]["windows_services"] == []


def test_togliere_il_pin_non_toglie_il_container(collector):
    """Il container e' scoperto da solo: non e' il catalogo a farlo esistere.
    Sparisce il pin — etichetta, URL, la scelta di mostrarlo in dashboard."""
    assert collector.dimentica_servizio("docker", "nginx") is True
    conts = collector.snapshot_ora()["services"]["docker"]["containers"]
    assert [c["name"] for c in conts] == ["nginx", "redis"], "il container non si tocca"
    nginx = conts[0]
    assert (nginx["pinned"], nginx["dashboard"], nginx["url"]) == (False, False, "")
    assert nginx["label"] == "nginx", "senza pin l'etichetta torna il nome vero"
    # I conteggi Docker contano i container, non i pin: non devono muoversi.
    assert collector.snapshot_ora()["services"]["docker"]["summary"]["total"] == 2


# ── Quello che non deve rompersi ───────────────────────────────────

def test_su_uno_snapshot_vuoto_non_solleva():
    """La pagina si puo' aprire prima del primo giro lento: li' non c'e'
    semplicemente niente da togliere, e non e' un errore."""
    c = DataCollector()
    assert c.dimentica_servizio("systemd", "cron.service") is False


def test_un_nome_che_non_c_e_non_cambia_niente(collector):
    prima = vista()
    assert collector.dimentica_servizio("systemd", "mai-esistita.service") is False
    assert collector.snapshot_ora()["services"] == prima


def test_un_kind_sconosciuto_non_solleva(collector):
    assert collector.dimentica_servizio("chissa", "x") is False


def test_i_conteggi_non_vanno_sotto_zero(collector):
    """Uno snapshot incoerente (summary a zero) non deve produrre numeri
    negativi in pagina: si tocca il pavimento e basta."""
    collector._snapshot["services"]["summary"] = {"total": 0, "ok": 0, "down": 0}
    collector.dimentica_servizio("systemd", "cron.service")
    s = collector.snapshot_ora()["services"]["summary"]
    assert (s["total"], s["ok"], s["down"]) == (0, 0, 0)
