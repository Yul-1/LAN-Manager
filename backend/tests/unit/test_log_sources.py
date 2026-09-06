"""
Sorgenti di log (services/log_sources.py).

Le proprieta' verificate qui sono quelle che rendono la pagina Logs affidabile:
il registro di audit non deve poter restituire un segreto (era gia'
successo), una riga senza timestamp interpretabile non deve sparire dietro un
filtro temporale, e i conteggi per livello devono contare cio' che c'e' e non
cio' che si sta guardando.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

from services import log_sources as LS


ORA_MS = int(time.time() * 1000)


# ── Registro di audit ──────────────────────────────────────────────

def test_la_passphrase_non_esce_mai_dall_audit():
    # Il difetto vero: la riga digitata dopo il prompt di una
    # passphrase finiva in chiaro in audit.log. Chi legge il file non puo' dare
    # per scontato che chi lo ha scritto si sia comportato bene.
    riga = LS.parse_audit_line(
        '2026-09-04T18:22:01+0200 terminal.comando ip=198.51.100.5 '
        'passphrase="chiave segreta" cmd="ls -la"')
    assert "chiave segreta" not in riga["msg"]
    assert "chiave segreta" not in riga["raw"], "nemmeno l'esportazione deve portarselo via"
    assert "passphrase=***" in riga["msg"]
    assert 'cmd="ls -la"' in riga["msg"], "il resto della riga deve restare leggibile"


@pytest.mark.parametrize("chiave", [
    "password", "passwd", "token", "secret", "api_key", "ssh_key", "hash", "cred"])
def test_ogni_chiave_sospetta_viene_oscurata(chiave):
    riga = LS.parse_audit_line(f"2026-09-04T18:22:01+0200 test.evento {chiave}=valore-vero")
    assert "valore-vero" not in riga["raw"]


def test_un_rifiuto_e_un_warning_non_un_info():
    # Un accesso negato annegato fra gli info e' un accesso negato che nessuno
    # vede: e' proprio la riga per cui esiste il registro.
    assert LS.parse_audit_line("2026-09-04T18:22:01+0200 terminal.negata ip=1.2.3.4"
                               )["level"] == "warn"
    assert LS.parse_audit_line("2026-09-04T18:22:01+0200 tool.rifiutato ip=1.2.3.4"
                               )["level"] == "warn"
    assert LS.parse_audit_line("2026-09-04T18:22:01+0200 terminal.aperta ip=1.2.3.4"
                               )["level"] == "info"


def test_una_riga_di_audit_fuori_formato_resta_leggibile():
    riga = LS.parse_audit_line("questa non e' una riga di audit")
    assert riga["msg"] == "questa non e' una riga di audit"
    assert riga["ts_ms"] is None


def test_la_coda_del_file_legge_dalla_fine(tmp_path):
    # Il registro cresce senza rotazione: caricarlo tutto per mostrarne 5 righe
    # e' il modo di far cadere il backend il giorno in cui il file e' grande.
    f = tmp_path / "audit.log"
    f.write_text("".join(f"riga {i}\n" for i in range(5000)))
    righe = LS._coda_file(str(f), 5)
    assert righe == [f"riga {i}" for i in range(4995, 5000)]


# ── Syslog del router: il timestamp senza anno ─────────────────────

def test_il_syslog_con_anno_si_data_esattamente():
    ms = LS._syslog_ts_ms("Sun Aug 16 19:59:00 2026")
    assert datetime.fromtimestamp(ms / 1000).year == 2026


def test_senza_anno_non_si_finisce_nel_futuro():
    # Su alcune build di OpenWrt l'anno non c'e'. Assumere sempre l'anno
    # corrente spedirebbe un log di dicembre visto a gennaio undici mesi
    # avanti, e da li' fuori da ogni finestra temporale.
    ms = LS._syslog_ts_ms(datetime.now().strftime("Sun %b %d %H:%M:%S"))
    assert ms is not None
    assert ms <= LS.ora_ms() + 61_000


def test_un_timestamp_illeggibile_non_fa_saltare_il_parsing():
    assert LS._syslog_ts_ms("non una data") is None
    riga = LS.parse_syslog_line("Sun Aug 16 19:59:00 2026 daemon.warn dnsmasq[12]: pieno")
    assert riga["level"] == "warn" and riga["src"] == "dnsmasq"


# ── journalctl ─────────────────────────────────────────────────────

def test_journal_json_priorita_e_sorgente():
    riga = LS.parse_journal_json(
        '{"__REALTIME_TIMESTAMP":"1788538921000000","PRIORITY":"3",'
        '"MESSAGE":"disco pieno","SYSLOG_IDENTIFIER":"kernel","_PID":"1"}', host="h")
    assert riga["level"] == "error"
    assert riga["src"] == "kernel"
    assert riga["ts_ms"] == 1788538921000
    assert riga["host"] == "h"


def test_journal_messaggio_binario():
    # Il journal restituisce i messaggi non-UTF8 come array di byte: senza
    # questo ramo la riga usciva come "[104, 105]".
    riga = LS.parse_journal_json('{"MESSAGE":[104,105],"PRIORITY":"6"}')
    assert riga["msg"] == "hi"


def test_journal_voci_senza_messaggio_si_saltano():
    assert LS.parse_journal_json('{"__CURSOR":"x"}') is None
    assert LS.parse_journal_json("non json") is None


def test_journal_priorita_illeggibile_non_solleva():
    riga = LS.parse_journal_json('{"MESSAGE":"x","PRIORITY":"boh"}')
    assert riga["level"] == "info"


# ── Filtri, finestra temporale, conteggi ───────────────────────────

def righe(*spec):
    return [LS.riga(ts, lvl, src, msg) for ts, lvl, src, msg in spec]


def test_i_conteggi_si_fanno_prima_del_filtro_per_livello(monkeypatch):
    # Servono a vedere che ci sono errori MENTRE si stanno guardando gli info:
    # contarli dopo il filtro darebbe sempre "0 errori" guardando gli info.
    campione = righe((ORA_MS, "error", "a", "rotto"),
                     (ORA_MS, "info", "b", "tutto bene"),
                     (ORA_MS, "info", "b", "ancora bene"))
    monkeypatch.setattr(LS, "_leggi_audit", lambda lines: _pronto((campione, "")))
    out = asyncio.run(LS.leggi(source="audit", level="info"))
    assert out["levels"]["error"] == 1
    assert out["count"] == 2, "le righe mostrate sono solo gli info"


def test_una_riga_senza_data_sopravvive_al_filtro_temporale(monkeypatch):
    # Le righe fuori formato (stack, continuazioni) sono spesso le piu'
    # interessanti: buttarle in silenzio sarebbe il difetto peggiore.
    campione = righe((None, "error", "", "Traceback (most recent call last):"),
                     (ORA_MS - 86_400_000, "info", "x", "vecchia"),
                     (ORA_MS, "info", "x", "recente"))
    monkeypatch.setattr(LS, "_leggi_audit", lambda lines: _pronto((campione, "")))
    out = asyncio.run(LS.leggi(source="audit", since_ms=ORA_MS - 3600_000))
    msg = [r["msg"] for r in out["lines"]]
    assert "Traceback (most recent call last):" in msg
    assert "recente" in msg
    assert "vecchia" not in msg


def test_escludere_una_sorgente_la_toglie_davvero(monkeypatch):
    campione = righe((ORA_MS, "info", "dropbear", "Child connection"),
                     (ORA_MS, "warn", "dnsmasq", "no address range"))
    monkeypatch.setattr(LS, "_leggi_audit", lambda lines: _pronto((campione, "")))
    out = asyncio.run(LS.leggi(source="audit", escludi=["dropbear"]))
    assert [r["src"] for r in out["lines"]] == ["dnsmasq"]


def test_le_sorgenti_rumorose_escono_ordinate_per_conteggio(monkeypatch):
    campione = righe(*[(ORA_MS, "info", "dropbear", "x")] * 3,
                     *[(ORA_MS, "info", "dnsmasq", "y")])
    monkeypatch.setattr(LS, "_leggi_audit", lambda lines: _pronto((campione, "")))
    out = asyncio.run(LS.leggi(source="audit"))
    assert out["sources"][0] == {"src": "dropbear", "count": 3}


def test_il_tetto_di_righe_non_si_puo_sfondare_dal_client(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "max_lines", 10)
    campione = righe(*[(ORA_MS, "info", "x", f"riga {i}") for i in range(50)])
    monkeypatch.setattr(LS, "_leggi_audit", lambda lines: _pronto((campione, "")))
    out = asyncio.run(LS.leggi(source="audit", lines=9999))
    assert out["count"] == 10
    assert out["lines"][-1]["msg"] == "riga 49", "si tengono le ULTIME, non le prime"


# ── Elenco delle sorgenti ──────────────────────────────────────────

def test_le_sorgenti_sensibili_sono_dichiarate_tali():
    assert LS.is_sensitive("audit") and LS.is_sensitive("backend")
    assert LS.is_sensitive("journal:198.51.100.10")
    assert not LS.is_sensitive("router"), "il syslog del router resta com'era"


def test_il_router_non_e_mai_archiviato():
    voce = next(v for v in LS.elenco_sorgenti() if v["id"] == "router")
    assert voce["persisted"] is False, (
        "il router ha poche risorse: non si archivia il suo log")


def test_l_audit_si_puo_togliere_dalla_pagina(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "audit_enabled", False)
    assert not any(v["id"] == "audit" for v in LS.elenco_sorgenti())


def test_gli_host_del_journal_vengono_dalla_config_non_dal_codice(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings.logs, "journal_hosts", ["10.0.0.9"])
    ids = [v["id"] for v in LS.elenco_sorgenti()]
    assert "journal:10.0.0.9" in ids


def _pronto(valore):
    """Una coroutine gia' risolta, per sostituire un lettore asincrono."""
    async def _f():
        return valore
    return _f()


def test_una_riga_sul_confine_fra_archivio_e_buffer_non_esce_due_volte(monkeypatch):
    # La lettura dell'archivio e' inclusiva su entrambi gli estremi: senza il
    # taglio esclusivo la riga esattamente sul confine uscirebbe due volte, e
    # un errore singolo sembrerebbe doppio.
    from config import settings
    from services import log_buffer as LB
    from services import history_store as HS

    confine = ORA_MS - 10_000
    LB.get_log_buffer().clear()
    LB.get_log_buffer().add({"ts_ms": confine, "level": "error", "src": "x",
                             "msg": "sul confine"})
    monkeypatch.setattr(settings.logs, "persist", True)

    class _Storico:
        def log_lines(self, kind, since_ms=0, until_ms=None, limit=0):
            righe = [{"t": confine - 5000, "level": "info", "src": "x", "msg": "prima",
                      "host": ""},
                     {"t": confine, "level": "error", "src": "x", "msg": "sul confine",
                      "host": ""}]
            return [r for r in righe if until_ms is None or r["t"] <= until_ms]

    monkeypatch.setattr(HS, "get_history", lambda: _Storico())
    try:
        out = asyncio.run(LS.leggi(source="backend", since_ms=ORA_MS - 3600_000))
    finally:
        LB.get_log_buffer().clear()
    msg = [r["msg"] for r in out["lines"]]
    assert msg.count("sul confine") == 1, msg
    assert "prima" in msg, "il resto dell'archivio deve comunque uscire"
