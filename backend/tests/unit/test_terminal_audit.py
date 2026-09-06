"""
Audit dei comandi del terminale SSH (services/terminal.py).

Il terminale registra ogni riga digitata. La riga che segue un prompt di
password NON deve finire nel registro: `config/audit.log` e' un file in chiaro,
e una password di root scritta li' e' peggio del non avere l'audit.

`TerminalSession` si costruisce con `__new__`: il costruttore vero aprirebbe una
connessione SSH.
"""
from __future__ import annotations

import pytest

from config import settings
from services import terminal as mod
from services.terminal import TerminalSession, _clamp


@pytest.fixture
def sessione(monkeypatch):
    """Sessione finta con l'audit sostituito da un raccoglitore in memoria."""
    registrate: list[dict] = []
    monkeypatch.setattr(mod, "audit",
                        lambda evento, **campi: registrate.append({"evento": evento, **campi}))
    monkeypatch.setattr(settings.terminal, "audit_commands", True)
    s = TerminalSession.__new__(TerminalSession)
    s._line = ""
    s._out_tail = ""
    s._skip_line = False
    s.client_ip = "192.0.2.5"
    s.host = "192.0.2.30"
    s.registrate = registrate
    return s


def _comandi(s):
    return [r["cmd"] for r in s.registrate]


# ── Registrazione normale ──────────────────────────────────────────

def test_una_riga_completa_finisce_nel_registro(sessione):
    sessione._track_input("uptime\r")
    assert _comandi(sessione) == ["uptime"]


def test_la_riga_si_accumula_tasto_per_tasto(sessione):
    for ch in "ls -la\n":
        sessione._track_input(ch)
    assert _comandi(sessione) == ["ls -la"]


def test_il_backspace_cancella(sessione):
    sessione._track_input("lsx\x7f -la\r")
    assert _comandi(sessione) == ["ls -la"]


def test_ctrl_c_abbandona_la_riga(sessione):
    sessione._track_input("rm -rf /\x03")
    sessione._track_input("uptime\r")
    assert _comandi(sessione) == ["uptime"]


def test_una_riga_vuota_non_viene_registrata(sessione):
    sessione._track_input("\r\n   \r")
    assert _comandi(sessione) == []


def test_laudit_si_puo_spegnere_da_configurazione(sessione, monkeypatch):
    monkeypatch.setattr(settings.terminal, "audit_commands", False)
    sessione._track_input("uptime\r")
    assert _comandi(sessione) == []


def test_un_incollo_enorme_non_cresce_senza_limite(sessione):
    sessione._track_input("a" * 10_000)
    assert len(sessione._line) <= 4096


# ── Soppressione dopo un prompt di password ────────────────────────

def test_la_riga_dopo_un_prompt_di_password_non_viene_registrata(sessione):
    sessione._note_output("[sudo] password for user: ")
    sessione._track_input("password-vera\r")
    assert _comandi(sessione) == []


@pytest.mark.parametrize("prompt", [
    "Password: ",
    "password:",
    "Enter passphrase: ",
    "[sudo] password for utente: ",
])
def test_i_prompt_tipici_sono_riconosciuti(sessione, prompt):
    sessione._note_output(prompt)
    sessione._track_input("segreta\r")
    assert _comandi(sessione) == []


def test_la_passphrase_di_una_chiave_ssh_non_finisce_nel_registro(sessione):
    # Trovato scrivendo questi test: la regex copriva "password for X:" ma non
    # "passphrase for X:", che e' proprio la forma stampata da ssh e ssh-add su
    # una chiave cifrata.
    sessione._note_output("Enter passphrase for key '/root/.ssh/id_ed25519': ")
    sessione._track_input("passphrase-della-chiave\r")
    assert _comandi(sessione) == []


def test_un_prompt_spezzato_fra_due_letture_viene_comunque_riconosciuto(sessione):
    # Con un PTY l'output arriva a pezzi: cercare nel singolo chunk non basta.
    sessione._note_output("Passwor")
    sessione._note_output("d: ")
    sessione._track_input("segreta\r")
    assert _comandi(sessione) == []


def test_un_byte_qualsiasi_dopo_il_prompt_non_riapre_laudit(sessione):
    # Un beep o un riposizionamento del cursore non devono bastare a far
    # finire la password nel registro.
    sessione._note_output("Password: ")
    sessione._note_output("\x07")
    sessione._track_input("segreta\r")
    assert _comandi(sessione) == []


def test_la_soppressione_vale_per_una_riga_sola(sessione):
    sessione._note_output("Password: ")
    sessione._track_input("segreta\r")
    sessione._track_input("uptime\r")
    assert _comandi(sessione) == ["uptime"]


def test_una_parola_password_a_meta_riga_non_sopprime(sessione):
    # Il prompt e' riconosciuto solo a fine riga: "cat password.txt" e' un comando.
    sessione._note_output("user@host:~$ ")
    sessione._track_input("cat password.txt\r")
    assert _comandi(sessione) == ["cat password.txt"]


def test_il_registro_riporta_ip_e_host(sessione):
    sessione._track_input("uptime\r")
    assert sessione.registrate[0]["ip"] == "192.0.2.5"
    assert sessione.registrate[0]["host"] == "192.0.2.30"
    assert sessione.registrate[0]["evento"] == "terminal.comando"


# ── _clamp ─────────────────────────────────────────────────────────

def test_clamp():
    assert _clamp(5, 1, 10) == 5
    assert _clamp(0, 1, 10) == 1
    assert _clamp(99, 1, 10) == 10
    assert _clamp("non-un-numero", 1, 10) == 1
    assert _clamp(None, 1, 10) == 1


# ── Etichette degli host ───────────────────────────────────────────
#  L'elenco mostrava "198.51.100.10 — user@198.51.100.10": l'indirizzo due volte
#  e mai il nome della macchina, che il catalogo dei dispositivi conosce gia'.

def test_gli_host_ssh_prendono_il_nome_dal_catalogo(monkeypatch):
    from services import terminal as T

    class _Store:
        def catalog_by_ip(self):
            return {"198.51.100.10": {"name": "homeserver"}}

    monkeypatch.setattr("services.device_store.get_device_store", lambda: _Store())
    monkeypatch.setattr(T, "live_ssh_config",
                        lambda: type("C", (), {"hosts": [type("H", (), {"ip": "198.51.100.10"})()]})())
    monkeypatch.setattr(T, "resolve_ssh_target", lambda host: {"username": "user"})
    monkeypatch.setattr(T.settings.router, "host", "")
    monkeypatch.setattr(T.settings.systemd, "ssh_enabled", False)

    hosts = T.allowed_hosts()
    assert [h["label"] for h in hosts] == ["homeserver"]


def test_senza_nome_a_catalogo_resta_lindirizzo(monkeypatch):
    from services import terminal as T

    monkeypatch.setattr("services.device_store.get_device_store",
                        lambda: type("S", (), {"catalog_by_ip": lambda self: {}})())
    monkeypatch.setattr(T, "live_ssh_config",
                        lambda: type("C", (), {"hosts": [type("H", (), {"ip": "192.0.2.9"})()]})())
    monkeypatch.setattr(T, "resolve_ssh_target", lambda host: {"username": "user"})
    monkeypatch.setattr(T.settings.router, "host", "")
    monkeypatch.setattr(T.settings.systemd, "ssh_enabled", False)

    assert T.allowed_hosts()[0]["label"] == "192.0.2.9"


def test_un_catalogo_rotto_non_toglie_gli_host(monkeypatch, caplog):
    # Un nome mancante e' un dettaglio; perdere l'host significherebbe non
    # poterci piu' entrare dal terminale.
    from services import terminal as T

    def esplode():
        raise RuntimeError("devices.yaml illeggibile")

    monkeypatch.setattr("services.device_store.get_device_store", esplode)
    monkeypatch.setattr(T, "live_ssh_config",
                        lambda: type("C", (), {"hosts": [type("H", (), {"ip": "192.0.2.9"})()]})())
    monkeypatch.setattr(T, "resolve_ssh_target", lambda host: {"username": "user"})
    monkeypatch.setattr(T.settings.router, "host", "")
    monkeypatch.setattr(T.settings.systemd, "ssh_enabled", False)

    hosts = T.allowed_hosts()
    assert [h["host"] for h in hosts] == ["192.0.2.9"]
    assert "illeggibile" in caplog.text, "e il motivo si logga, mai in silenzio"
