"""
Storico persistente su SQLite (services/history_store.py).

Nessun test tocca il database reale: ognuno lavora su un file dentro la
`tmp_path` di pytest. Le proprieta' verificate qui sono quelle su cui poggia
l'onesta' dei grafici — i buchi restano buchi, i punti sono a passo fisso, e
un file corrotto non porta giu' il backend.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from services.history_store import HistoryStore, _voci_servizi


ORA = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def cadenze_realistiche(monkeypatch):
    """La config della suite mette gli intervalli di raccolta a un giorno per
    non far mai partire il collector. Qui servono invece quelli veri, perche'
    e' da loro che lo store deriva la risoluzione dei punti."""
    from config import settings
    monkeypatch.setattr(settings, "collect_interval_fast", 10)
    monkeypatch.setattr(settings.host_metrics, "interval", 60)


def store(tmp_path, **kw) -> HistoryStore:
    return HistoryStore(path=str(tmp_path / "history.db"), **kw)


def punti_traffico(st, quanti=60, passo=10_000, buco=range(0)):
    for i in range(quanti):
        st.add_traffic({
            "t": ORA - (quanti - i) * passo,
            "rx_bps": None if i in buco else 1000.0 * i,
            "tx_bps": None if i in buco else 500.0 * i,
            "latency": None if i in buco else 12.5,
        })


# ── Apertura, schema, permessi ─────────────────────────────────────

def test_crea_il_file_e_lo_schema(tmp_path):
    st = store(tmp_path)
    assert st.enabled and st.path.exists()
    tabelle = {r[0] for r in st._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"traffic", "host_metrics", "device_events", "service_events"} <= tabelle
    st.close()


def test_il_file_e_leggibile_solo_dal_proprietario(tmp_path):
    st = store(tmp_path)
    assert oct(os.stat(st.path).st_mode & 0o777) == "0o600"
    st.close()


def test_spento_non_crea_nulla_e_non_solleva(tmp_path):
    st = store(tmp_path, enabled=False)
    st.add_traffic({"t": ORA, "rx_bps": 1.0, "tx_bps": 1.0, "latency": 1.0})
    st.mark_devices([{"key": "192.0.2.5", "online": True}])
    st.compact()
    assert not st.path.exists()
    assert st.traffic(since_ms=0) == []


def test_file_corrotto_viene_messo_da_parte_e_si_riparte(tmp_path):
    db = tmp_path / "history.db"
    db.write_bytes(b"questo non e' un database sqlite, e' spazzatura")
    st = store(tmp_path)                      # non deve sollevare
    assert st.enabled
    quarantena = list(tmp_path.glob("history.db.corrotto-*"))
    assert len(quarantena) == 1, "il file rotto va conservato, non cancellato"
    st.add_traffic({"t": ORA, "rx_bps": 5.0, "tx_bps": 5.0, "latency": 1.0})
    assert any(p["rx_bps"] == 5.0 for p in st.traffic(since_ms=ORA - 60_000))
    st.close()


# ── Campionamenti: passo fisso e buchi onesti ──────────────────────

def test_i_punti_escono_a_passo_fisso(tmp_path):
    """Il grafico dispone i punti per indice: se il passo non fosse costante
    l'asse dei tempi mentirebbe."""
    st = store(tmp_path)
    punti_traffico(st)
    p = st.traffic(since_ms=ORA - 600_000)
    passi = {p[i + 1]["t"] - p[i]["t"] for i in range(len(p) - 1)}
    assert len(passi) == 1, f"passo non costante: {passi}"
    st.close()


def test_un_buco_resta_un_buco_non_diventa_zero(tmp_path):
    st = store(tmp_path)
    punti_traffico(st, buco=range(20, 30))
    p = st.traffic(since_ms=ORA - 600_000)
    assert any(x["rx_bps"] is None for x in p)
    assert not any(x["rx_bps"] == 0 and x["tx_bps"] == 0 for x in p[25:35])
    st.close()


def test_intervallo_senza_dati_e_tutto_buchi_non_lista_vuota(tmp_path):
    """Backend spento per un'ora: la finestra deve restare larga quanto e',
    altrimenti il grafico comprime il vuoto e sembra non essere successo nulla."""
    st = store(tmp_path)
    punti_traffico(st, quanti=10)
    p = st.traffic(since_ms=ORA - 3_600_000)
    assert len(p) > 100
    assert all(x["rx_bps"] is None for x in p[:50])
    st.close()


def test_non_supera_mai_il_tetto_di_punti(tmp_path):
    st = store(tmp_path)
    punti_traffico(st, quanti=2000)
    p = st.traffic(since_ms=ORA - 2000 * 10_000, max_points=100)
    assert len(p) <= 101         # +1: l'ultimo bucket puo' includere "adesso"
    st.close()


def test_risorse_separate_per_host(tmp_path):
    st = store(tmp_path)
    for i in range(10):
        t = ORA - (10 - i) * 60_000
        st.add_host_points({
            "192.0.2.10": {"t": t, "cpu": 10.0, "mem": 20.0,
                           "swap": 0.0, "load1": 0.1, "temp": 40.0},
            "192.0.2.11": {"t": t, "cpu": 90.0, "mem": 80.0,
                           "swap": 1.0, "load1": 2.0, "temp": 70.0},
        })
    a = [x["cpu"] for x in st.host_series("192.0.2.10", ORA - 700_000) if x["cpu"] is not None]
    b = [x["cpu"] for x in st.host_series("192.0.2.11", ORA - 700_000) if x["cpu"] is not None]
    assert a and b and max(a) < min(b)
    st.close()


def test_stesso_host_e_istante_non_si_duplica(tmp_path):
    """La raccolta risorse ha un intervallo suo: il collector puo' ripassare
    sullo stesso campione."""
    st = store(tmp_path)
    p = {"t": ORA, "cpu": 5.0, "mem": 1.0, "swap": 0.0, "load1": 0.0, "temp": 30.0}
    st.add_host_points({"192.0.2.10": p})
    st.add_host_points({"192.0.2.10": dict(p, cpu=99.0)})
    n = st._db.execute("SELECT COUNT(*) FROM host_metrics").fetchone()[0]
    assert n == 1
    assert st._db.execute("SELECT cpu FROM host_metrics").fetchone()[0] == 5.0
    st.close()


# ── Compattazione e retention ──────────────────────────────────────

def test_compatta_i_punti_vecchi_in_medie(tmp_path):
    st = store(tmp_path)
    vecchio = ORA - 48 * 3600 * 1000          # oltre le 24h di dettaglio pieno
    for i in range(60):                       # 10 minuti a passo 10s
        st.add_traffic({"t": vecchio + i * 10_000, "rx_bps": 100.0,
                        "tx_bps": 50.0, "latency": 1.0})
    prima = st._db.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
    st.compact()
    righe = st._db.execute("SELECT rx_bps, span FROM traffic").fetchall()
    assert len(righe) < prima, "la compattazione non ha ridotto le righe"
    assert all(r["span"] == 300 for r in righe), "le righe aggregate dichiarano lo span"
    assert all(r["rx_bps"] == 100.0 for r in righe), "la media di valori uguali non cambia"
    st.close()


def test_la_compattazione_e_ripetibile(tmp_path):
    st = store(tmp_path)
    vecchio = ORA - 48 * 3600 * 1000
    for i in range(60):
        st.add_traffic({"t": vecchio + i * 10_000, "rx_bps": 1.0,
                        "tx_bps": 1.0, "latency": 1.0})
    st.compact()
    dopo_uno = st._db.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
    st._last_compact = 0
    st.compact()
    assert st._db.execute("SELECT COUNT(*) FROM traffic").fetchone()[0] == dopo_uno
    st.close()


def test_un_bucket_tutto_vuoto_resta_vuoto_dopo_la_compattazione(tmp_path):
    """AVG ignora i NULL: se pero' sono tutti NULL il buco deve sopravvivere."""
    st = store(tmp_path)
    vecchio = ORA - 48 * 3600 * 1000
    for i in range(30):
        st.add_traffic({"t": vecchio + i * 10_000, "rx_bps": None,
                        "tx_bps": None, "latency": None})
    st.compact()
    righe = st._db.execute("SELECT rx_bps FROM traffic").fetchall()
    assert righe and all(r["rx_bps"] is None for r in righe)
    st.close()


def test_oltre_la_finestra_si_cancella(tmp_path):
    st = store(tmp_path)
    st.add_traffic({"t": ORA - 30 * 86400 * 1000, "rx_bps": 1.0,
                    "tx_bps": 1.0, "latency": 1.0})
    st.add_traffic({"t": ORA, "rx_bps": 2.0, "tx_bps": 2.0, "latency": 2.0})
    st.compact()
    rimasti = [r[0] for r in st._db.execute("SELECT rx_bps FROM traffic")]
    assert rimasti == [2.0]
    st.close()


# ── Eventi: solo le transizioni ────────────────────────────────────

def test_scrive_solo_i_cambi_di_stato(tmp_path):
    st = store(tmp_path)
    acceso = [{"key": "192.0.2.5", "name": "nas", "online": True}]
    st.mark_devices(acceso)
    st.mark_devices(acceso)
    st.mark_devices(acceso)
    assert st._db.execute("SELECT COUNT(*) FROM device_events").fetchone()[0] == 1
    st.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": False}])
    stati = [r[0] for r in st._db.execute("SELECT online FROM device_events ORDER BY id")]
    assert stati == [1, 0]
    st.close()


def test_lo_stato_si_ricarica_alla_riapertura(tmp_path):
    """Senza questo, ogni riavvio del backend direbbe che tutta la rete si e'
    riaccesa insieme all'aggiornamento dell'immagine."""
    st = store(tmp_path)
    st.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": True}])
    st.close()

    st2 = store(tmp_path)
    st2.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": True}])
    assert st2._db.execute("SELECT COUNT(*) FROM device_events").fetchone()[0] == 1
    st2.close()


def test_l_ultimo_evento_sopravvive_alla_retention(tmp_path):
    """"Da quando e' giu'" deve funzionare anche per un servizio caduto piu'
    di retain_days fa: e' proprio il caso in cui la domanda si pone."""
    st = store(tmp_path)
    st.mark_devices([{"key": "192.0.2.5", "name": "nas", "online": False}])
    st._db.execute("UPDATE device_events SET t = ?", (ORA - 60 * 86400 * 1000,))
    st._db.commit()
    st.compact()
    assert st._db.execute("SELECT COUNT(*) FROM device_events").fetchone()[0] == 1
    st.close()


def test_servizi_stesso_nome_su_host_diversi_sono_distinti(tmp_path):
    st = store(tmp_path)
    st.mark_services({"systemd": [
        {"kind": "systemd", "name": "docker", "host": "192.0.2.10", "ok": True},
        {"kind": "systemd", "name": "docker", "host": "192.0.2.11", "ok": False},
    ]})
    righe = st._db.execute("SELECT host, ok FROM service_events ORDER BY host").fetchall()
    assert [(r["host"], r["ok"]) for r in righe] == [("192.0.2.10", 1), ("192.0.2.11", 0)]
    st.close()


def test_un_container_acceso_non_viene_registrato_come_spento():
    """Forma vera di ContainerInfo.to_dict(), dove i nomi ingannano: `running`
    e' il booleano, `status` lo stato macchina e `state` la stringa leggibile.
    Leggere la chiave sbagliata segnerebbe giu' tutti i container accesi."""
    voci = _voci_servizi({"docker": {"containers": [
        {"name": "nginx", "host": "srv", "running": True,
         "status": "running", "state": "Up 2 hours"},
        {"name": "vecchio", "host": "srv", "running": False,
         "status": "exited", "state": "Exited (0) 3 days ago"},
    ]}})
    assert [(v["name"], v["ok"]) for v in voci] == [("nginx", True), ("vecchio", False)]
    assert voci[0]["detail"] == "Up 2 hours"


def test_appiattimento_delle_tre_famiglie_di_servizi():
    voci = _voci_servizi({
        "docker": {"containers": [{"name": "nginx", "host": "srv", "running": True}]},
        "systemd": [{"name": "ssh.service", "host": "srv", "ok": True}],
        "healthchecks": [{"name": "dashboard", "ok": False, "detail": "timeout"}],
        "summary": {},
    })
    assert {v["kind"] for v in voci} == {"docker", "systemd", "healthcheck"}
    assert [v["ok"] for v in voci] == [True, True, False]


def test_voci_senza_nome_si_scartano():
    assert _voci_servizi({"systemd": [{"name": "", "ok": True}]}) == []


# ── Log archiviati ─────────────────────────────────────────────────
# Si archivia solo cio' che altrimenti si perderebbe: il log del backend (vive
# in memoria e in `docker logs`, che sparisce con l'immagine) e il registro di
# audit. Il syslog del ROUTER non entra mai qui.

def righe_log(quanti=5, base_ms=None, level="info"):
    base = base_ms if base_ms is not None else ORA
    return [{"ts_ms": base + i, "level": level, "src": "prova", "msg": f"riga {i}"}
            for i in range(quanti)]


def test_le_righe_di_log_si_rileggono_in_ordine(tmp_path):
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(5))
    righe = st.log_lines("backend")
    assert [r["msg"] for r in righe] == [f"riga {i}" for i in range(5)]


def test_il_limite_prende_le_ultime_righe_non_le_prime(tmp_path):
    # Tagliare dall'inizio darebbe le piu' vecchie, cioe' l'esatto contrario di
    # quello che serve guardando un log.
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(50))
    righe = st.log_lines("backend", limit=3)
    assert [r["msg"] for r in righe] == ["riga 47", "riga 48", "riga 49"]


def test_le_sorgenti_non_si_mescolano(tmp_path):
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(2))
    st.add_log_lines("audit", [{"ts_ms": ORA, "level": "warn", "src": "terminal.negata",
                                "msg": "ip=1.2.3.4"}])
    assert len(st.log_lines("backend")) == 2
    assert len(st.log_lines("audit")) == 1


def test_con_persist_false_non_si_archivia_niente(tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "persist", False)
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(5))
    assert st.log_lines("backend") == []


def test_l_audit_si_tiene_piu_a_lungo_del_log_del_backend(tmp_path, monkeypatch):
    # Il sospetto arriva mesi dopo: un audit potato insieme al log del backend
    # sparirebbe proprio prima di servire.
    from config import settings
    monkeypatch.setattr(settings.logs, "retain_days", 7)
    monkeypatch.setattr(settings.logs, "audit_retain_days", 90)
    vecchio = ORA - 30 * 86_400_000
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(3, base_ms=vecchio))
    st.add_log_lines("audit", righe_log(3, base_ms=vecchio))
    st.compact()
    assert st.log_lines("backend") == [], "30 giorni sono oltre i 7 del backend"
    assert len(st.log_lines("audit")) == 3, "ma dentro i 90 dell'audit"


def test_la_finestra_temporale_filtra_le_righe(tmp_path):
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(3, base_ms=ORA - 86_400_000))
    st.add_log_lines("backend", righe_log(3, base_ms=ORA))
    assert len(st.log_lines("backend", since_ms=ORA - 3600_000)) == 3


def test_il_tetto_sulle_righe_tiene_le_piu_recenti(tmp_path, monkeypatch):
    # La retention a giorni non limita un log di cui non si controlla il ritmo:
    # sull'istanza vera sette giorni valevano 46 MB. Il tetto e' l'unica cosa
    # che da' un ingombro prevedibile.
    from config import settings
    monkeypatch.setattr(settings.logs, "max_rows", 10)
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(40))
    st.compact()
    righe = st.log_lines("backend")
    assert len(righe) == 10
    assert righe[-1]["msg"] == "riga 39", "si tengono le ultime, non le prime"


def test_il_tetto_non_tocca_l_audit(tmp_path, monkeypatch):
    # L'audit e' poche righe al giorno ed e' quello che conta: un tetto pensato
    # per il rumore del backend non deve poterlo potare.
    from config import settings
    monkeypatch.setattr(settings.logs, "max_rows", 5)
    st = store(tmp_path)
    st.add_log_lines("audit", righe_log(30))
    st.compact()
    assert len(st.log_lines("audit")) == 30


def test_tetto_a_zero_significa_nessun_tetto(tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "max_rows", 0)
    st = store(tmp_path)
    st.add_log_lines("backend", righe_log(30))
    st.compact()
    assert len(st.log_lines("backend")) == 30
