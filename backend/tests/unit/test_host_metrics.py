"""
Risorse degli host (services/host_metrics.py).

Il punto delicato e' il **campionamento onesto**: la CPU e' il
delta di /proc/stat fra due raccolte, la finestra viene dichiarata nel dato, e la
prima lettura vale `None` (un buco nel grafico) invece di uno zero inventato.
Il caso del 104% della 0.1.25 nasceva proprio da qui.

Le fixture sono catture reali del comando che il servizio manda via SSH:
i campioni 1 e 2 distano ~0.02s (sotto _MIN_WINDOW), 1 e 3 circa 15.5s.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.host_metrics import (_MIN_WINDOW, HostMetricsCollector, HostResources,
                                   _cpu_percent, _cpu_totals, _filesystems, _fmt_uptime,
                                   _meminfo, _milli_to_celsius, _remote_command,
                                   _split_sections)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _blob(n: int) -> str:
    return (FIXTURES / f"host_metrics_{n}.txt").read_text()


@pytest.fixture
def orologio(monkeypatch):
    """Cronometro pilotato: senza, la finestra dipenderebbe da quanto e' veloce
    la macchina che esegue i test."""
    import services.host_metrics as mod
    ora = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ora[0])
    return ora


# ── _cpu_percent: il buco invece dello zero ────────────────────────

def test_prima_lettura_niente_percentuale():
    assert _cpu_percent((1000, 900), None) is None
    assert _cpu_percent(None, (1000, 900)) is None


def test_percentuale_su_una_finestra_normale():
    # 100 jiffies totali, 75 inattivi -> 25% occupato.
    assert _cpu_percent((1100, 975), (1000, 900)) == 25.0


def test_contatori_azzerati_dal_riavvio_non_producono_un_picco():
    assert _cpu_percent((10, 5), (1000, 900)) is None      # totale all'indietro
    assert _cpu_percent((1100, 800), (1000, 900)) is None  # idle all'indietro


def test_finestra_a_delta_zero():
    assert _cpu_percent((1000, 900), (1000, 900)) is None


def test_la_percentuale_resta_nel_range():
    # Il caso del 104%: qualunque cosa succeda ai contatori, il valore e' 0..100.
    for v in (_cpu_percent((1100, 900), (1000, 900)), _cpu_percent((1100, 1000), (1000, 900))):
        assert v is None or 0.0 <= v <= 100.0


# ── _parse_into: due raccolte sulla stessa istanza ─────────────────

def test_la_prima_raccolta_non_ha_percentuale_ne_finestra(orologio):
    c = HostMetricsCollector()
    res = HostResources(host="192.0.2.30")
    c._parse_into(res, _blob(1), "linux")
    assert res.cpu["percent"] is None
    assert res.cpu["window_seconds"] is None
    assert res.cpu["cores"] > 0
    assert res.hostname == "host-di-test"


def test_la_seconda_raccolta_dichiara_la_finestra_su_cui_e_calcolata(orologio):
    c = HostMetricsCollector()
    res1 = HostResources(host="192.0.2.30")
    c._parse_into(res1, _blob(1), "linux")
    orologio[0] += 15.5
    res2 = HostResources(host="192.0.2.30")
    c._parse_into(res2, _blob(3), "linux")
    assert res2.cpu["percent"] is not None
    assert 0.0 <= res2.cpu["percent"] <= 100.0
    assert res2.cpu["window_seconds"] == 15.5
    assert len(res2.cpu["per_core"]) == res2.cpu["cores"]


def test_una_finestra_troppo_corta_viene_scartata(orologio):
    c = HostMetricsCollector()
    c._parse_into(HostResources(host="192.0.2.30"), _blob(1), "linux")
    orologio[0] += _MIN_WINDOW / 2
    res = HostResources(host="192.0.2.30")
    c._parse_into(res, _blob(2), "linux")
    assert res.cpu["percent"] is None, "sotto _MIN_WINDOW il dato non e' significativo"
    assert res.cpu["window_seconds"] is None


def test_una_finestra_scartata_non_rimette_il_cronometro_a_zero(orologio):
    # Se _prev avanzasse ad ogni lettura, due raccolte ravvicinate azzererebbero
    # il conteggio e la media non arriverebbe mai.
    c = HostMetricsCollector()
    c._parse_into(HostResources(host="192.0.2.30"), _blob(1), "linux")
    partenza = c._prev["192.0.2.30"]["t"]
    orologio[0] += _MIN_WINDOW / 2
    c._parse_into(HostResources(host="192.0.2.30"), _blob(2), "linux")
    assert c._prev["192.0.2.30"]["t"] == partenza
    orologio[0] += 15.0
    res = HostResources(host="192.0.2.30")
    c._parse_into(res, _blob(3), "linux")
    assert res.cpu["window_seconds"] == pytest.approx(15.0 + _MIN_WINDOW / 2)


def test_il_riavvio_dellhost_butta_la_finestra_precedente(orologio):
    c = HostMetricsCollector()
    res1 = HostResources(host="192.0.2.30")
    c._parse_into(res1, _blob(1), "linux")
    orologio[0] += 60.0
    # Uptime piu' basso della lettura precedente: l'host si e' riavviato.
    riavviato = _blob(3).replace("##UPTIME\n", "##UPTIME\n1.00 1.00\n##IGNORATA\n")
    res2 = HostResources(host="192.0.2.30")
    c._parse_into(res2, riavviato, "linux")
    assert res2.uptime_seconds == 1
    assert res2.cpu["percent"] is None, "i contatori post-riavvio non sono confrontabili"


def test_host_diversi_hanno_finestre_indipendenti(orologio):
    c = HostMetricsCollector()
    c._parse_into(HostResources(host="192.0.2.30"), _blob(1), "linux")
    orologio[0] += 15.5
    altro = HostResources(host="192.0.2.10")
    c._parse_into(altro, _blob(3), "linux")
    assert altro.cpu["percent"] is None      # per questo host e' la prima lettura


# ── Sezioni e helper puri ──────────────────────────────────────────

def test_le_sezioni_attese_ci_sono_tutte():
    s = _split_sections(_blob(1))
    assert {"STAT", "LOADAVG", "MEMINFO", "UPTIME", "HOSTNAME", "MODEL",
            "DISKSTATS", "BLOCK", "DF", "TEMP", "THERMAL", "PS", "END"} <= set(s)


def test_le_righe_prima_della_prima_sezione_sono_ignorate():
    s = _split_sections("commento iniziale\n##STAT\ncpu 1 2 3 4\n")
    assert list(s) == ["STAT"] and s["STAT"] == ["cpu 1 2 3 4"]


def test_cpu_totals_da_una_coppia_per_cpu_e_per_core():
    totals = _cpu_totals(_split_sections(_blob(1))["STAT"])
    assert "cpu" in totals
    totale, inattivo = totals["cpu"]
    assert totale > inattivo > 0
    assert sum(1 for k in totals if k.startswith("cpu") and k != "cpu") >= 1


def test_meminfo_converte_i_kilobyte_in_byte():
    mem = _meminfo(_split_sections(_blob(1))["MEMINFO"])
    assert mem["MemTotal"] % 1024 == 0
    assert mem["MemTotal"] > 100 * 1024 * 1024        # piu' di 100 MB: sono byte


def test_i_filesystem_pseudo_sono_esclusi():
    fs = _filesystems(_split_sections(_blob(1))["DF"])
    assert fs, "nessun filesystem riconosciuto"
    for f in fs:
        assert f["used_pct"] is None or 0 <= f["used_pct"] <= 100
        assert f["mount"] and f["total"] > 0
        assert f["fstype"] not in ("tmpfs", "devtmpfs", "overlay")


def test_le_temperature_fuori_scala_vengono_scartate():
    assert _milli_to_celsius("45000") == 45.0
    assert _milli_to_celsius("-60000") is None       # sotto -50
    assert _milli_to_celsius("200000") is None       # sopra 150
    assert _milli_to_celsius("non-un-numero") is None
    assert _milli_to_celsius("") is None


def test_il_comando_remoto_limita_il_numero_di_processi():
    assert "head -n 6" in _remote_command(6)
    assert "head -n 30" in _remote_command(999)       # tetto massimo
    assert "head -n 1" in _remote_command(-5)         # pavimento
    # 0 significa "non specificato" e ricade sul default, non sul pavimento.
    assert "head -n 6" in _remote_command(0)


def test_il_comando_remoto_e_posix_e_non_usa_bashismi():
    cmd = _remote_command(6)
    assert "[[" not in cmd and "$(( " not in cmd


def test_uptime_leggibile():
    assert _fmt_uptime(0) == "0m"
    assert _fmt_uptime(90) == "1m"
    assert _fmt_uptime(3600 * 2 + 60) == "2h 1m"
    assert _fmt_uptime(86400 * 3 + 3600) == "3g 1h 0m"
