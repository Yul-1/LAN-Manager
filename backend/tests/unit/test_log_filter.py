"""
Le regole di scarto dei log (services/log_filter.py).

Il filtro nasce da una misura: con `httpx` gia' zitto il log del backend cresce
di ~26 righe al minuto e tocca il tetto di `logs.max_rows` in circa 32 ore.
Poterne buttare via una frase ripetuta e' quello che allunga l'orizzonte.

Qui si prova soprattutto quello che NON deve succedere: che una regola scritta
male svuoti la pagina, e che l'audit sia filtrabile. Sono le due strade con cui
un filtro dei log smette di essere una comodita' e diventa un buco.
"""
from __future__ import annotations

import logging

import pytest

from config import LogExclude, settings
from services import log_filter
from services.log_filter import pattern_per_grep, regole, scarta


@pytest.fixture
def con_regole(monkeypatch):
    """Installa regole e ricompila; a fine test si torna a nessuna regola."""
    def installa(*voci: LogExclude):
        monkeypatch.setattr(settings.logs, "exclude", list(voci))
        log_filter.reset()
    yield installa
    log_filter.reset()


def riga(msg: str, src: str = "docker-client", raw: str = "") -> dict:
    return {"ts_ms": 0, "level": "warn", "src": src, "msg": msg, "raw": raw or msg}


# ── Cosa scarta, e cosa no ─────────────────────────────────────────

def test_il_pattern_scarta_solo_le_righe_che_lo_contengono(con_regole):
    con_regole(LogExclude(pattern="docker/ssh"))
    assert scarta(riga("docker/ssh 192.0.2.3: timeout"))
    assert not scarta(riga("container nginx riavviato"))


def test_il_pattern_non_distingue_maiuscole(con_regole):
    con_regole(LogExclude(pattern="TIMEOUT"))
    assert scarta(riga("docker/ssh 192.0.2.3: timeout dopo 5s"))


def test_il_solo_src_scarta_tutto_quel_logger(con_regole):
    con_regole(LogExclude(src="httpx"))
    assert scarta(riga("HTTP Request: GET /containers/json", src="httpx"))
    assert not scarta(riga("HTTP Request: GET /containers/json", src="collector"))


def test_pattern_e_src_valgono_in_and(con_regole):
    con_regole(LogExclude(pattern="timeout", src="docker-client"))
    assert scarta(riga("timeout", src="docker-client"))
    # Stesso testo, altro mittente: la regola non parlava di lui.
    assert not scarta(riga("timeout", src="host-metrics"))


def test_quando_manca_msg_si_guarda_la_riga_grezza(con_regole):
    """Il syslog fuori formato ha solo `raw`: un filtro deve poterci agire."""
    con_regole(LogExclude(pattern="dropbear"))
    assert scarta({"src": "", "msg": "", "raw": "Jan  1 00:00:00 dropbear[1]: exit"})


# ── Le due regole di sicurezza ─────────────────────────────────────

def test_una_regola_senza_pattern_ne_src_non_scarta_niente(con_regole, caplog):
    """Senza questo, un `pattern: ""` lasciato nello YAML svuoterebbe i log."""
    with caplog.at_level(logging.WARNING, logger="log-filter"):
        con_regole(LogExclude(pattern="", src=""))
        assert regole() == []
    assert not scarta(riga("qualunque cosa"))
    assert "senza 'pattern'" in caplog.text, "una regola ignorata va dichiarata"


def test_l_audit_non_e_filtrabile_nemmeno_nominandolo(con_regole):
    """E' la traccia di chi ha fatto cosa: poterne nascondere una riga dalla
    configurazione sarebbe esattamente il modo di coprire un abuso."""
    # Una regola che nomina l'audit, e una che varrebbe per tutte le sorgenti:
    # nessuna delle due deve poterlo toccare.
    con_regole(LogExclude(pattern="login", sources=["audit"]),
               LogExclude(pattern="utente"))
    assert not scarta(riga("login ok utente=admin", src="auth"), "audit")
    # La seconda altrove funziona: non e' il pattern a essere inerte.
    assert scarta(riga("login ok utente=admin", src="auth"), "backend")


# ── Regex ──────────────────────────────────────────────────────────

def test_la_regex_si_applica_solo_se_dichiarata(con_regole):
    con_regole(LogExclude(pattern=r"192\.0\.2\.\d+: timeout", regex=True))
    assert scarta(riga("docker/ssh 192.0.2.3: timeout"))
    assert not scarta(riga("docker/ssh 198.51.100.7: timeout"))


def test_una_regex_non_valida_viene_dichiarata_e_saltata(con_regole, caplog):
    """Applicarla a meta', o trattarla come testo, darebbe un filtro che
    nessuno ha scritto: si salta e si dice perche'."""
    with caplog.at_level(logging.WARNING, logger="log-filter"):
        con_regole(LogExclude(pattern="[non chiusa", regex=True))
        assert regole() == []
    assert not scarta(riga("[non chiusa"))
    assert "regex non valida" in caplog.text


# ── A quali sorgenti si applica ────────────────────────────────────

def test_sources_vuoto_vale_per_tutte(con_regole):
    con_regole(LogExclude(pattern="rumore"))
    for source in ("backend", "router", "journal:192.0.2.30"):
        assert scarta(riga("rumore"), source), source


def test_sources_limita_la_regola_alla_sorgente_dichiarata(con_regole):
    con_regole(LogExclude(pattern="rumore", sources=["router"]))
    assert scarta(riga("rumore"), "router")
    assert not scarta(riga("rumore"), "backend")


def test_journal_secco_vale_per_qualunque_host(con_regole):
    """Altrimenti si dovrebbe elencare un host alla volta, e una macchina
    aggiunta domani sfuggirebbe al filtro senza che nessuno se ne accorga."""
    con_regole(LogExclude(pattern="rumore", sources=["journal"]))
    assert scarta(riga("rumore"), "journal:192.0.2.30")
    assert scarta(riga("rumore"), "journal:192.0.2.31")
    assert not scarta(riga("rumore"), "router")


# ── Cosa puo' viaggiare come grep sul router ───────────────────────

def test_solo_i_pattern_di_testo_finiscono_nel_grep_del_router(con_regole):
    """`grep -i -v` non sa esprimere ne' una regex ne' un vincolo sul mittente:
    quelle regole restano al filtro in Python, e va detto nel codice."""
    con_regole(
        LogExclude(pattern="dropbear"),
        LogExclude(pattern=r"\d+", regex=True),
        LogExclude(pattern="timeout", src="docker-client"),
        LogExclude(pattern="solo-backend", sources=["backend"]),
    )
    assert pattern_per_grep("router") == ["dropbear"]


def test_una_regola_spenta_non_conta(con_regole):
    con_regole(LogExclude(pattern="dropbear", enabled=False))
    assert regole() == []
    assert not scarta(riga("dropbear[1]: exit"))


# ── I due punti in cui il filtro morde ─────────────────────────────

def test_la_riga_scartata_non_entra_nel_buffer(con_regole):
    """E' questo che allunga l'orizzonte dell'archivio: il buffer e' la coda da
    cui il collector pesca le righe da scrivere nel database. Filtrare solo in
    lettura avrebbe nascosto il rumore lasciandolo occupare posto."""
    from services.log_buffer import RingHandler, get_log_buffer

    con_regole(LogExclude(pattern="docker/ssh"))
    buf = get_log_buffer()
    buf.clear()
    handler = RingHandler()
    for msg in ("docker/ssh 192.0.2.3: timeout", "scan completato"):
        handler.emit(logging.LogRecord("collector", logging.WARNING, __file__, 1,
                                       msg, None, None))

    assert [r["msg"] for r in buf.tail()] == ["scan completato"]
    buf.clear()


async def test_il_filtro_vale_anche_su_cio_che_era_gia_archiviato(con_regole, monkeypatch):
    """Una regola scritta oggi deve ripulire anche la pagina, non solo il
    domani: altrimenti il rumore gia' salvato resta li' a coprire il resto."""
    from services import log_sources

    async def finto_backend(lines, since_ms, until_ms):
        return [riga("docker/ssh 192.0.2.3: timeout"), riga("scan completato")], ""

    monkeypatch.setattr(log_sources, "_leggi_backend", finto_backend)
    con_regole(LogExclude(pattern="docker/ssh"))

    esito = await log_sources.leggi(source="backend")
    assert [r["msg"] for r in esito["lines"]] == ["scan completato"]


async def test_sul_router_i_pattern_viaggiano_col_grep_non_dopo_il_tail(con_regole, monkeypatch):
    """Sul router il taglio alle ultime N righe avviene la': filtrare dopo
    averle ricevute restituirebbe una manciata di righe invece delle N chieste
    (e' un difetto gia' visto, al contrario)."""
    from services import log_sources

    visti: dict = {}

    async def finto_router(lines, filtro, escludi):
        visti["escludi"] = escludi
        return [], ""

    monkeypatch.setattr(log_sources, "_leggi_router", finto_router)
    con_regole(LogExclude(pattern="dropbear"))

    await log_sources.leggi(source="router", escludi=["crond"])
    assert visti["escludi"] == ["crond", "dropbear"]


def test_una_regola_malformata_non_manda_in_ricorsione_l_handler(con_regole):
    """Il rientro non e' ovvio: `_compila()` scrive un warning quando una regola
    e' malformata, quel warning passa da `RingHandler`, che chiama `scarta()`,
    che chiama `regole()` — con le regole ancora in costruzione. Senza guardia
    si ricompilerebbe da capo ad ogni riga, all'infinito."""
    from services.log_buffer import RingHandler, get_log_buffer, install, uninstall

    con_regole(LogExclude(pattern="", src=""), LogExclude(pattern="rumore"))
    buf = get_log_buffer()
    buf.clear()
    install()                       # l'handler agganciato davvero, come in esercizio
    try:
        assert not scarta(riga("prima riga"))
        assert scarta(riga("rumore"))
        # La ricorsione non si vede dal risultato — `scarta` intercetta anche il
        # RecursionError e risponde "tieni la riga" — ma si vede da quante volte
        # la regola malformata e' stata dichiarata: una, non mille.
        avvisi = [r for r in buf.tail() if "senza 'pattern'" in r["msg"]]
        assert len(avvisi) == 1, f"la compilazione e' rientrata {len(avvisi)} volte"
    finally:
        uninstall()
        buf.clear()
