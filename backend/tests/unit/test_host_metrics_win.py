"""
Risorse di un host Windows (services/host_metrics_win.py).

Le fixture sono **catture reali** dello script che il servizio manda via SSH,
prese a 16 secondi di distanza da un PC Windows 11 con PowerShell 5.1 (solo il
nome dell'host e' stato sostituito). Servono a coprire i due casi che contano:
la prima lettura, che non puo' avere una percentuale, e la seconda, che la
calcola sul delta.

Il punto delicato e' che i contatori Windows arrivano gia' nella forma che la
strada POSIX produce da /proc, cosi' le medie su finestra restano scritte una
volta sola in `host_metrics._componi`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.host_metrics import HostMetricsCollector, HostResources
from services.host_metrics_win import interpreta, script
from services.ssh_hosts import TETTO_RIGA_COMANDO, comando_powershell

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _blob(n: int) -> str:
    return (FIXTURES / f"host_metrics_win_{n}.json").read_text(encoding="utf-8")


@pytest.fixture
def orologio(monkeypatch):
    """Cronometro pilotato: senza, la finestra dipenderebbe da quanto e' veloce
    la macchina che esegue i test."""
    import services.host_metrics as mod
    ora = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ora[0])
    return ora


# ── Lo script che si manda alla macchina remota ────────────────────

def test_lo_script_sta_nella_riga_di_comando_di_cmd():
    # Oltre il tetto cmd.exe tronca senza dire niente: e' un limite da tenere
    # sotto controllo qui, non da scoprire quando la scheda dell'host si svuota.
    assert len(comando_powershell(script(30))) < TETTO_RIGA_COMANDO


def test_lo_script_limita_il_numero_di_processi():
    assert "-First 6" in script(6)
    assert "-First 30" in script(999)      # tetto massimo
    assert "-First 1" in script(-5)        # pavimento
    assert "-First 6" in script(0)         # 0 = non specificato -> default


def test_lo_script_spegne_il_progresso_e_fissa_la_codifica():
    s = script(6)
    # Senza questi due, PowerShell sporca lo stderr con CLIXML e restituisce
    # stringhe nella codepage della console invece che in UTF-8.
    assert "$ProgressPreference = 'SilentlyContinue'" in s
    assert "[Console]::OutputEncoding" in s


def test_lo_script_forza_le_stringhe_sui_valori_di_testo():
    # ConvertTo-Json di PowerShell 5.1 serializza gli enum come interi: senza i
    # cast, i nomi tornerebbero come numeri e nessun confronto funzionerebbe.
    s = script(6)
    assert "[string]$_.Name" in s
    assert "[string]$_.DeviceID" in s


# ── Interpretazione di una cattura reale ───────────────────────────

def test_la_prima_lettura_non_ha_percentuale_ne_finestra(orologio):
    c = HostMetricsCollector()
    res = HostResources(host="192.0.2.12")
    c._parse_into(res, _blob(1), "windows")
    assert res.cpu["percent"] is None
    assert res.cpu["window_seconds"] is None
    assert res.cpu["cores"] == 8
    assert res.hostname == "host-windows-di-test"
    assert res.os == "windows"


def test_la_seconda_lettura_calcola_la_media_sulla_finestra(orologio):
    c = HostMetricsCollector()
    c._parse_into(HostResources(host="192.0.2.12"), _blob(1), "windows")
    orologio[0] += 16.0
    res = HostResources(host="192.0.2.12")
    c._parse_into(res, _blob(2), "windows")
    assert res.cpu["window_seconds"] == 16.0
    assert 0.0 < res.cpu["percent"] <= 100.0
    assert len(res.cpu["per_core"]) == res.cpu["cores"]
    assert all(v is None or 0.0 <= v <= 100.0 for v in res.cpu["per_core"])


def test_windows_non_ha_load_average():
    # Non si inventa un equivalente: la scheda mostra "—", come per qualunque
    # altro valore che quella macchina non sa dare.
    assert interpreta(_blob(1))["load"] == []


def test_memoria_e_swap():
    d = interpreta(_blob(1))
    mem, swap = d["memory"], d["swap"]
    assert mem["total"] > 8 * 1024 ** 3
    assert 0 < mem["used_pct"] <= 100
    assert mem["used"] == mem["total"] - mem["available"]
    assert 0 <= swap["used_pct"] <= 100


def test_le_unita_logiche_diventano_dischi():
    dischi = interpreta(_blob(1))["disks"]
    assert dischi, "nessun disco riconosciuto"
    for d in dischi:
        # Su Windows la lettera e' anche il punto di mount: la scheda resta
        # leggibile con lo stesso codice usato per Linux.
        assert d["device"] == d["mount"]
        assert d["total"] > 0 and 0 <= d["used_pct"] <= 100


def test_i_contatori_di_io_diventano_velocita(orologio):
    c = HostMetricsCollector()
    c._parse_into(HostResources(host="192.0.2.12"), _blob(1), "windows")
    orologio[0] += 16.0
    res = HostResources(host="192.0.2.12")
    c._parse_into(res, _blob(2), "windows")
    assert res.disk_io, "nessun disco fisico"
    for io in res.disk_io:
        assert io["device"] != "_Total", "il totale non e' un disco"
        assert io["read_bps"] is not None and io["read_bps"] >= 0


def test_le_temperature_acpi_diventano_gradi():
    temps = interpreta(_blob(1))["temperatures"]
    assert temps, "la cattura contiene una sonda ACPI"
    for t in temps:
        assert -50 < t["celsius"] < 150


def test_i_processi_hanno_una_quota_di_memoria():
    d = interpreta(_blob(1))
    procs = d["processes"]
    assert procs
    for p in procs:
        assert p["command"] and p["pid"] > 0
        # L'utente proprietario costerebbe una query per processo: resta vuoto
        # invece di essere inventato.
        assert p["user"] == ""
        assert 0 <= p["mem_percent"] <= 100


# ── Le tre forme di ConvertTo-Json ─────────────────────────────────

def test_un_elenco_con_un_solo_elemento_arriva_come_oggetto():
    # ConvertTo-Json restituisce un oggetto, non un array, quando l'elenco ha un
    # solo elemento: se non lo si gestisse, un PC con un disco solo perderebbe
    # il disco.
    d = json.loads(_blob(1))
    d["dischi"] = d["dischi"][0]
    assert len(interpreta(json.dumps(d))["disks"]) == 1


def test_un_elenco_vuoto_arriva_come_null():
    d = json.loads(_blob(1))
    d["temperature"] = None
    assert interpreta(json.dumps(d))["temperatures"] == []


def test_output_non_interpretabile_e_un_errore_esplicito():
    # Meglio un guasto scritto nella scheda dell'host che una scheda a zero che
    # sembra una macchina a riposo.
    for storto in ("", "   ", "non json", "[1, 2, 3]"):
        with pytest.raises(ValueError):
            interpreta(storto)
