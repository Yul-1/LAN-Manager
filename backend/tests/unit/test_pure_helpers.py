"""
Helper puri sparsi: rate limiter, testi d'errore, sweep, validazione chiave SSH,
parsing di `docker stats`. Nessuno tocca rete o filesystem.
"""
from __future__ import annotations

import pytest

from services.docker_client import _mem, _pct, _state_from_status, _to_mb
from services.errors import exc_text, ssh_error
from services.ping import MAX_SWEEP_HOSTS, expand_subnets
from services.ratelimit import RateLimiter
from services.ssh_hosts import validate_key_path


# ── RateLimiter ────────────────────────────────────────────────────

def test_consente_esattamente_max_hits_poi_nega():
    rl = RateLimiter(max_hits=3, window_seconds=60)
    for _ in range(3):
        assert rl.allowed("ip")
        rl.hit("ip")
    assert not rl.allowed("ip")


def test_la_finestra_scade(monkeypatch):
    import services.ratelimit as mod
    ora = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ora[0])
    rl = RateLimiter(max_hits=1, window_seconds=10)
    rl.hit("ip")
    assert not rl.allowed("ip")
    ora[0] += 9.9
    assert not rl.allowed("ip")          # ancora dentro la finestra
    ora[0] += 0.2
    assert rl.allowed("ip")              # finestra scaduta


def test_reset_libera_la_chiave():
    rl = RateLimiter(max_hits=1, window_seconds=60)
    rl.hit("ip")
    assert not rl.allowed("ip")
    rl.reset("ip")
    assert rl.allowed("ip")


def test_le_chiavi_sono_indipendenti():
    rl = RateLimiter(max_hits=1, window_seconds=60)
    rl.hit("192.0.2.1")
    assert not rl.allowed("192.0.2.1")
    assert rl.allowed("192.0.2.2")


def test_i_colpi_vecchi_non_si_accumulano_senza_limite(monkeypatch):
    import services.ratelimit as mod
    ora = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ora[0])
    rl = RateLimiter(max_hits=100, window_seconds=1)
    for _ in range(50):
        rl.hit("ip")
        ora[0] += 1.1                    # ogni colpo esce dalla finestra
    assert len(rl._hits["ip"]) == 1


# ── Testi d'errore ─────────────────────────────────────────────────

def test_eccezione_senza_messaggio_diventa_il_nome_della_classe():
    # asyncssh e asyncio sollevano spesso eccezioni con str(e) == "": interpolarle
    # da sole produceva righe di log troncate dopo i due punti.
    assert exc_text(TimeoutError()) == "TimeoutError"
    assert exc_text(ValueError("")) == "ValueError"


def test_eccezione_con_messaggio_lo_conserva():
    assert exc_text(OSError("[Errno 113] No route to host")) == "[Errno 113] No route to host"


def test_ssh_error_spiega_il_timeout_invece_di_nominarlo():
    testo = ssh_error(TimeoutError())
    assert "porta 22" in testo and "firewall" in testo


def test_ssh_error_non_riscrive_gli_altri_errori():
    assert ssh_error(OSError("No route to host")) == "No route to host"


# ── expand_subnets ─────────────────────────────────────────────────

def test_una_24_esclude_rete_e_broadcast():
    ips = expand_subnets(["192.0.2.0/24"])
    assert len(ips) == 254
    assert "192.0.2.0" not in ips and "192.0.2.255" not in ips
    assert ips[0] == "192.0.2.1" and ips[-1] == "192.0.2.254"


def test_piu_subnet_si_sommano():
    assert len(expand_subnets(["192.0.2.0/30", "198.51.100.0/30"])) == 4


def test_una_subnet_troppo_grande_viene_saltata():
    # Un /16 sarebbe 65k ping per ciclo: la guardia esiste per questo.
    assert expand_subnets(["10.0.0.0/16"]) == []
    assert len(expand_subnets(["10.0.0.0/22"])) <= MAX_SWEEP_HOSTS


def test_una_subnet_malformata_non_ferma_le_altre():
    ips = expand_subnets(["non-un-cidr", "192.0.2.0/30"])
    assert len(ips) == 2


def test_elenco_vuoto():
    assert expand_subnets([]) == []


# ── validate_key_path ──────────────────────────────────────────────

def test_il_contenuto_incollato_al_posto_del_percorso_viene_respinto():
    # Bug del 2026-08-16: dalle Impostazioni si poteva salvare la chiave stessa.
    with pytest.raises(ValueError, match="contenuto della chiave"):
        validate_key_path("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 utente@host", check_file=False)


def test_il_percorso_relativo_viene_respinto():
    with pytest.raises(ValueError, match="assoluto"):
        validate_key_path("ssh/id_ed25519", check_file=False)


def test_il_percorso_assoluto_passa_il_controllo_di_forma():
    assert validate_key_path("/app/ssh/id_ed25519", check_file=False) == "/app/ssh/id_ed25519"


def test_la_chiave_vuota_e_ammessa_significa_usa_la_predefinita():
    assert validate_key_path("", check_file=False) == ""
    assert validate_key_path("   ", check_file=False) == ""


def test_un_file_inesistente_viene_respinto_con_check_file():
    with pytest.raises(ValueError, match="inesistente"):
        validate_key_path("/percorso/che/non/esiste/id_ed25519", check_file=True)


# ── Parsing di docker ps / docker stats ────────────────────────────

def test_lo_stato_del_container_si_deduce_dallo_status():
    assert _state_from_status("Up 3 days (healthy)") == "running"
    assert _state_from_status("Up 2 hours") == "running"
    assert _state_from_status("Exited (0) 5 minutes ago") == "exited"
    assert _state_from_status("Restarting (1) 2 seconds ago") == "restarting"


def test_le_percentuali_tollerano_valori_mancanti():
    assert _pct("0.00%") == 0.0
    assert _pct(None) == 0.0
    assert _pct("--") == 0.0
    assert _pct("") == 0.0


def test_le_percentuali_sono_arrotondate_a_un_decimale():
    # Scelta voluta: `docker stats` da' due decimali, che in dashboard sono
    # rumore. Fissato qui perche' e' una differenza visibile nell'API.
    assert _pct("12.34%") == 12.3
    assert _pct("99.99%") == 100.0


def test_le_unita_di_memoria_diventano_megabyte():
    assert _to_mb("512KiB") == pytest.approx(0.5, rel=1e-2)
    assert _to_mb("40MiB") == pytest.approx(40.0)
    assert _to_mb("2GiB") == pytest.approx(2048.0)


def test_luso_di_memoria_si_spezza_in_usato_e_limite():
    assert _mem("40MiB / 2GiB") == (pytest.approx(40.0), pytest.approx(2048.0))
    assert _mem(None) == (0.0, 0.0)


# ── RateLimiter.retry_after ────────────────────────────────────────

def test_retry_after_e_zero_finche_c_e_spazio():
    from services.ratelimit import RateLimiter
    rl = RateLimiter(max_hits=2, window_seconds=60)
    assert rl.retry_after("x") == 0
    rl.hit("x")
    assert rl.retry_after("x") == 0


def test_retry_after_dice_quanti_secondi_mancano_quando_e_saturo():
    from services.ratelimit import RateLimiter
    rl = RateLimiter(max_hits=1, window_seconds=30)
    rl.hit("x")
    atteso = rl.retry_after("x")
    # Senza questo il client puo' solo dire "troppe richieste" e l'utente
    # ritenta a caso.
    assert 1 <= atteso <= 30
