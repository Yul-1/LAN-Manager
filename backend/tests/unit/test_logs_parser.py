"""
Parser del syslog del router (routers/logs.py).

Regressione del 2026-08-16: l'anno a 4 cifre non era previsto dalla regex, quindi
il campo `facility` finiva a leggere l'anno e **ogni riga risultava "info"** —
il log del router non distingueva piu' un guasto da un'operazione di routine.
Questo file esiste soprattutto per impedire che quel bug torni.
"""
from __future__ import annotations

from pathlib import Path

from routers.logs import _level_of, _parse_syslog_line

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _righe():
    return (FIXTURES / "logread_openwrt.txt").read_text().strip().splitlines()


# ── La regressione ─────────────────────────────────────────────────

def test_riga_con_anno_a_quattro_cifre_riconosce_il_livello():
    riga = "Sun Aug 16 19:59:01 2026 daemon.err dnsmasq[1234]: failed to send packet"
    out = _parse_syslog_line(riga)
    assert out["facility"] == "daemon.err"
    assert out["level"] == "error"          # era "info" prima del fix
    assert out["ts"] == "Sun Aug 16 19:59:01 2026"
    assert out["src"] == "dnsmasq"


def test_riga_senza_anno_resta_supportata():
    # Alcune build di OpenWrt non stampano l'anno. Il giorno della settimana
    # invece c'e' sempre: nella regex e' obbligatorio, opzionale e' solo l'anno.
    riga = "Sun Aug 16 19:59:06 daemon.err dropbear[8001]: Exit before auth"
    out = _parse_syslog_line(riga)
    assert out["facility"] == "daemon.err"
    assert out["level"] == "error"
    assert out["ts"] == "Sun Aug 16 19:59:06"


def test_timestamp_senza_giorno_della_settimana_ricade_su_grezzo():
    # Non e' un formato che logread produce: il test fissa che una riga
    # inattesa non solleva e conserva il testo, invece di essere mutilata.
    riga = "Aug 16 19:59:06 daemon.err dropbear[8001]: Exit before auth"
    out = _parse_syslog_line(riga)
    assert out["ts"] == "" and out["msg"] == riga


def test_nessuna_riga_della_cattura_finisce_tutta_a_info():
    livelli = {_parse_syslog_line(r)["level"] for r in _righe()}
    assert livelli == {"error", "warn", "info"}, (
        "se qui resta solo 'info' la regex del timestamp e' di nuovo rotta")


# ── Livelli ────────────────────────────────────────────────────────

def test_le_severita_di_errore_diventano_error():
    for facility in ("daemon.err", "kern.crit", "user.alert", "auth.emerg"):
        assert _level_of(facility) == "error", facility


def test_warn_diventa_warn_e_il_resto_info():
    assert _level_of("daemon.warn") == "warn"
    assert _level_of("daemon.info") == "info"
    assert _level_of("user.notice") == "info"
    assert _level_of("daemon.debug") == "info"


def test_il_job_ordinario_di_crond_non_e_un_errore():
    # crond gira con -l 5 e registra ogni esecuzione come cron.err: trattarli da
    # errori tingerebbe di rosso tutto il log e nasconderebbe i guasti veri.
    assert _level_of("cron.err", "crond", "USER root pid 7467 cmd /root/check.sh") == "info"


def test_un_errore_vero_di_crond_resta_errore():
    assert _level_of("cron.err", "crond", "time disparity of 5 minutes detected") == "error"


# ── Campi e casi limite ────────────────────────────────────────────

def test_la_sorgente_perde_il_pid():
    out = _parse_syslog_line(
        "Sun Aug 16 19:59:07 cron.err crond[3772]: USER root pid 1 cmd x")
    assert out["src"] == "crond"          # non "crond[3772]"


def test_i_due_punti_nel_messaggio_non_spezzano_il_campo():
    riga = ("Sun Aug 16 19:59:08 2026 daemon.info netifd: "
            "wan (900): udhcpc: sending renew to 203.0.113.1")
    out = _parse_syslog_line(riga)
    assert out["src"] == "netifd"
    assert out["msg"] == "wan (900): udhcpc: sending renew to 203.0.113.1"


def test_riga_fuori_formato_non_solleva_e_conserva_il_testo():
    riga = "    continuazione di una riga precedente, senza timestamp"
    out = _parse_syslog_line(riga)
    assert out["level"] == "info"
    assert out["ts"] == ""
    assert out["src"] == ""
    assert out["msg"] == riga
    assert out["raw"] == riga


def test_riga_vuota_non_solleva():
    out = _parse_syslog_line("")
    assert out["level"] == "info" and out["msg"] == ""


def test_ogni_riga_della_cattura_ha_le_chiavi_attese():
    for riga in _righe():
        out = _parse_syslog_line(riga)
        assert {"ts", "level", "src", "msg", "raw"} <= set(out)
        assert out["raw"] == riga


# ── Il comando costruito per il router ─────────────────────────────
# I filtri devono restringere TUTTO il log, non le ultime N righe.

import asyncio

from services.openwrt import SSHClient


class _SSHFinto(SSHClient):
    """Cattura il comando invece di aprire una connessione."""

    def __init__(self):
        self.cmd = ""

    async def run(self, cmd: str, timeout: int | None = None):
        self.cmd = cmd
        return "", ""


def _comando(**kw) -> str:
    ssh = _SSHFinto()
    asyncio.run(ssh.logread(**kw))
    return ssh.cmd


def test_il_filtro_cerca_in_tutto_il_log_non_nelle_ultime_righe():
    # Col `tail` prima del `grep` si cercava solo dentro le ultime N righe: su
    # homeserver le ultime 300 sono tutte connessioni SSH di LANMng, quindi
    # cercare "wireguard" tornava vuoto con il log pieno di quelle righe.
    cmd = _comando(lines=300, filters=["wireguard"])
    assert cmd.index("grep") < cmd.index("tail"), cmd
    assert cmd.endswith("| tail -n 300")


def test_i_criteri_da_nascondere_diventano_grep_negativi():
    cmd = _comando(lines=100, filters=["dnsmasq"], exclude=["dropbear"])
    assert "grep -i dnsmasq" in cmd
    assert "grep -i -v dropbear" in cmd


def test_i_criteri_vuoti_non_aggiungono_grep():
    assert _comando(lines=50, filters=[""], exclude=[""]) == "logread | tail -n 50"


def test_i_criteri_sono_quotati():
    # Arrivano da un query param: senza quoting sarebbero una shell aperta.
    cmd = _comando(lines=10, filters=["a; reboot"])
    assert "'a; reboot'" in cmd and "; reboot |" not in cmd


def test_piu_criteri_da_nascondere_separati_da_virgola():
    # Sul router vero i rumori sono due insieme (SSH del monitoraggio e cron
    # del modem): nasconderne uno solo lascia il log illeggibile lo stesso.
    import asyncio as _asyncio

    from routers.logs import get_logs

    class _SSH:
        def __init__(self):
            self.kw = {}

        async def logread(self, lines=100, filters=None, exclude=None):
            self.kw = {"lines": lines, "filters": filters, "exclude": exclude}
            return ""

    # La lettura vive in services/log_sources: e' li' che si
    # sostituisce l'SSH. La rotta resta il punto di ingresso da provare, perche'
    # e' lei a spezzare la stringa "a, b ," in criteri.
    import services.log_sources as LS
    ssh = _SSH()
    vecchio = LS.get_ssh
    LS.get_ssh = lambda: ssh
    try:
        _asyncio.run(get_logs(request=None, lines=50, exclude="dropbear, crond ,"))
    finally:
        LS.get_ssh = vecchio
    assert ssh.kw["exclude"] == ["dropbear", "crond"], ssh.kw
