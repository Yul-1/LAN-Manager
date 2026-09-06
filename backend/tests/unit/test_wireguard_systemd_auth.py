"""
Tre superfici piccole ma delicate: dump WireGuard letto dal router, stato delle
unit systemd, e le funzioni pure dell'autenticazione (firma dei token, difesa
CSRF, avvisi di configurazione permissiva).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from config import settings
from services.i18n import t
from middleware.auth import (auth_enabled, is_lan, make_token, same_origin,
                             security_warnings, valid_token)
from services.systemd_monitor import _from_props, _parse_props, _unavailable
from services.wireguard import parse_wg_dump

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

PEER_UNO = "cGVlci11bm8tcHViYmxpY2Etbm9uLXZlcmEtY2hpYXZlLQ="
PEER_DUE = "cGVlci1kdWUtcHViYmxpY2Etbm9uLXZlcmEtY2hpYXZlLQ="


# ── WireGuard ──────────────────────────────────────────────────────

def _dump():
    return (FIXTURES / "wg_show_all_dump.txt").read_text()


def test_interfaccia_e_peer_si_distinguono_dal_numero_di_campi():
    ifaces = parse_wg_dump(_dump())
    assert len(ifaces) == 1
    wg0 = ifaces[0]
    assert wg0.name == "wg0" and wg0.listen_port == 51820
    assert len(wg0.peers) == 2


def test_il_catalogo_da_un_nome_leggibile_ai_peer():
    # La mappa pubkey -> nome sta in config.yaml: nessun nome di peer nel codice.
    ifaces = parse_wg_dump(_dump(), {PEER_UNO: "portatile"})
    per_pub = {p.public_key: p for p in ifaces[0].peers}
    assert per_pub[PEER_UNO].name == "portatile"
    assert per_pub[PEER_DUE].name == ""          # sconosciuto: nessun nome inventato


def test_endpoint_assente_diventa_stringa_vuota():
    per_pub = {p.public_key: p for p in parse_wg_dump(_dump())[0].peers}
    assert per_pub[PEER_UNO].endpoint == "198.51.100.7:41234"
    assert per_pub[PEER_DUE].endpoint == ""      # "(none)" non e' un endpoint


def test_gli_allowed_ips_multipli_diventano_una_lista():
    per_pub = {p.public_key: p for p in parse_wg_dump(_dump())[0].peers}
    assert per_pub[PEER_DUE].allowed_ips == ["10.8.0.3/32", "10.8.1.0/24"]


def test_un_peer_senza_handshake_non_e_attivo():
    per_pub = {p.public_key: p for p in parse_wg_dump(_dump())[0].peers}
    assert per_pub[PEER_DUE].last_handshake == 0
    assert per_pub[PEER_DUE].is_active is False
    assert per_pub[PEER_DUE].last_handshake_ago == "never"


def test_un_peer_con_handshake_recente_e_attivo():
    dump = _dump().replace("\t1755370740\t", f"\t{int(time.time())}\t")
    per_pub = {p.public_key: p for p in parse_wg_dump(dump)[0].peers}
    assert per_pub[PEER_UNO].is_active is True
    assert per_pub[PEER_UNO].last_handshake_ago.endswith("s ago")


def test_un_peer_con_handshake_vecchio_non_e_attivo():
    # La soglia e' 3 minuti: un peer fermo da un'ora non va mostrato come su.
    dump = _dump().replace("\t1755370740\t", f"\t{int(time.time()) - 3600}\t")
    per_pub = {p.public_key: p for p in parse_wg_dump(dump)[0].peers}
    assert per_pub[PEER_UNO].is_active is False
    assert per_pub[PEER_UNO].last_handshake_ago == "1h ago"


def test_i_contatori_dei_peer_sono_numeri():
    per_pub = {p.public_key: p for p in parse_wg_dump(_dump())[0].peers}
    assert per_pub[PEER_UNO].rx_bytes == 1048576
    assert per_pub[PEER_UNO].tx_bytes == 524288


def test_dump_vuoto_o_malformato_non_solleva():
    assert parse_wg_dump("") == []
    assert parse_wg_dump("una riga\tcon\tpochi\tcampi") == []


# ── systemd ────────────────────────────────────────────────────────

def _props(blocco: str) -> dict:
    testo = (FIXTURES / "systemctl_show.txt").read_text()
    pezzo = testo.split(f"## {blocco}")[1].split("##")[0]
    return _parse_props(pezzo)


def test_le_proprieta_si_leggono_dal_formato_chiave_uguale_valore():
    props = _props("docker.service (attiva)")
    assert props == {"LoadState": "loaded", "ActiveState": "active", "SubState": "running"}


def test_una_unit_attiva():
    st = _from_props({"unit": "docker.service", "label": "Docker", "critical": True},
                     _props("docker.service (attiva)"))
    assert st.active_state == "active" and st.sub_state == "running"
    assert st.available is True and st.critical is True


def test_una_unit_inesistente_e_riconoscibile():
    st = _from_props({"unit": "inesistente.service"},
                     _props("inesistente.service (non caricata)"))
    assert st.load_state == "not-found" and st.active_state == "inactive"
    assert st.available is True       # la risposta c'e', dice che l'unit non c'e'


def test_una_unit_non_interrogabile_non_finge_di_essere_attiva():
    # Host irraggiungibile: mai inventare uno stato.
    st = _unavailable({"unit": "docker.service", "critical": True})
    assert st.available is False
    assert st.active_state == "unknown" and st.load_state == "unknown"
    assert st.critical is True


def test_props_vuote_danno_una_unit_non_disponibile():
    st = _from_props({"unit": "x.service"}, {})
    assert st.available is False and st.active_state == "unknown"


def test_lhost_viene_ripulito():
    assert _from_props({"unit": "x", "host": "  192.0.2.10 "}, {}).host == "192.0.2.10"
    assert _from_props({"unit": "x"}, {}).host == ""


def test_righe_senza_uguale_vengono_ignorate():
    assert _parse_props("rumore\nLoadState=loaded\n") == {"LoadState": "loaded"}


# ── Token di sessione ──────────────────────────────────────────────

def test_un_token_appena_creato_e_valido():
    assert valid_token(make_token("admin")) is True


def test_un_token_manomesso_viene_rifiutato():
    token = make_token("admin")
    username, exp, sig = token.rsplit(".", 2)
    assert valid_token(f"altro.{exp}.{sig}") is False        # utente cambiato
    assert valid_token(f"{username}.{exp}.{sig[:-1]}x") is False   # firma cambiata


def test_un_token_con_scadenza_allungata_viene_rifiutato():
    # Senza la firma sulla scadenza, chiunque potrebbe rendere eterna la sessione.
    username, exp, sig = make_token("admin").rsplit(".", 2)
    assert valid_token(f"{username}.{int(exp) + 10**6}.{sig}") is False


def test_un_token_scaduto_viene_rifiutato(monkeypatch):
    import middleware.auth as mod
    token = make_token("admin")
    adesso = time.time()
    monkeypatch.setattr(mod.time, "time", lambda: adesso + mod.TTL + 10)
    assert valid_token(token) is False


def test_cambiare_la_secret_key_invalida_i_token_esistenti(monkeypatch):
    token = make_token("admin")
    monkeypatch.setattr(settings, "secret_key", "un-altra-chiave")
    assert valid_token(token) is False


@pytest.mark.parametrize("token", ["", "senza-punti", "a.b", None, "a.non-numero.firma"])
def test_un_token_malformato_non_solleva(token):
    assert valid_token(token) is False


# ── same_origin: difesa CSRF ───────────────────────────────────────

class _Richiesta:
    def __init__(self, **headers):
        self.headers = headers


def test_senza_header_origin_la_richiesta_passa():
    # Client non-browser (curl, lo smoke script): non e' un vettore CSRF.
    assert same_origin(_Richiesta(host="testserver")) is True


def test_origin_uguale_allhost_passa():
    assert same_origin(_Richiesta(origin="http://testserver", host="testserver")) is True


def test_origin_con_porta_diversa_passa():
    # nginx forwarda Host senza porta, l'Origin la include: si confronta l'hostname.
    assert same_origin(_Richiesta(origin="http://192.0.2.30:81", host="192.0.2.30")) is True


def test_origin_di_un_altro_host_viene_respinta():
    assert same_origin(_Richiesta(origin="http://attaccante.example",
                                  host="testserver")) is False


def test_origin_malformata_viene_respinta():
    assert same_origin(_Richiesta(origin="://", host="testserver")) is False


def test_il_confronto_ignora_le_maiuscole():
    assert same_origin(_Richiesta(origin="http://TestServer", host="testserver")) is True


# ── is_lan ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,atteso", [
    ("192.168.1.10", True), ("10.0.0.1", True), ("172.16.0.1", True),
    ("127.0.0.1", True), ("::1", True),
    ("93.184.216.34", False), ("8.8.8.8", False),
    ("testclient", False), ("", False), (None, False),
])
def test_riconoscimento_degli_indirizzi_privati(host, atteso):
    assert is_lan(host) is atteso


# ── security_warnings ──────────────────────────────────────────────

def test_una_configurazione_chiusa_non_produce_avvisi():
    # basic + password impostata + bypass spento: e' la config di produzione.
    assert security_warnings() == []


def test_auth_spenta_viene_denunciata(monkeypatch):
    monkeypatch.setattr(settings.auth, "method", "none")
    avvisi = security_warnings()
    assert avvisi == [t("sicurezza.authNone", lingua="en")]
    assert auth_enabled() is False


def test_il_bypass_lan_viene_denunciato(monkeypatch):
    # La "LAN" comprende subnet segmentate e client VPN: il pentest 2026-08-17
    # e' entrato esattamente da li'.
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    avvisi = security_warnings()
    assert len(avvisi) == 1 and "bypass_lan" in avvisi[0]


def test_una_password_admin_mancante_viene_denunciata(monkeypatch):
    # admin_hash() legge prima l'ambiente di processo, poi secrets.env, poi la
    # config: per simulare "nessuna password" vanno tolte tutte e tre.
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH", raising=False)
    monkeypatch.setattr(settings.auth, "password_hash", "")
    avvisi = security_warnings()
    assert t("sicurezza.nessunaPassword", lingua="en") in avvisi


def test_nessun_avviso_contiene_un_valore_di_configurazione(monkeypatch):
    # Gli avvisi finiscono nei log e in dashboard: devono descrivere, non esporre.
    monkeypatch.setattr(settings.auth, "method", "none")
    monkeypatch.setattr(settings.auth, "bypass_lan", True)
    monkeypatch.delenv("LAN_AUTH__PASSWORD_HASH", raising=False)
    monkeypatch.setattr(settings.auth, "password_hash", "")
    for avviso in security_warnings():
        assert settings.secret_key not in avviso
        assert "$2b$" not in avviso
