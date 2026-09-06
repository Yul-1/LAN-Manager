"""
Servizi Windows monitorati via SSH (services/windows_services.py).

Le fixture sono **catture reali** di `Get-Service` su un PC Windows 11 con
PowerShell 5.1, prese chiedendo apposta anche un servizio inesistente.

Tre cose qui producono dati sbagliati in silenzio se si sbagliano, e per ognuna
c'e' un test: gli enum serializzati come interi, l'esito singolo che diventa un
oggetto invece di un array, e i caratteri jolly che `Get-Service` espande.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import windows_services as mod
from services.windows_services import (WindowsServicesMonitor, interpreta, script,
                                       valida_nome)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _blob(nome: str) -> str:
    return (FIXTURES / f"windows_services_{nome}.json").read_text(encoding="utf-8")


class FintaConnessione:
    def __init__(self, uscita: str):
        self._uscita = uscita
        self.comandi: list[str] = []

    async def run(self, comando, **kwargs):
        self.comandi.append(comando)
        from types import SimpleNamespace
        return SimpleNamespace(stdout=self._uscita, stderr="", exit_status=0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def ssh(monkeypatch):
    """Sostituisce asyncssh.connect e tiene il conto delle connessioni aperte."""
    import asyncssh
    stato = {"connessioni": [], "conn": []}

    def _programma(uscita: str = "[]", errore: Exception | None = None):
        def _connect(**kwargs):
            stato["connessioni"].append(kwargs.get("host"))
            if errore:
                raise errore
            conn = FintaConnessione(uscita)
            stato["conn"].append(conn)
            return conn
        monkeypatch.setattr(asyncssh, "connect", _connect)
        return stato

    return _programma


# ── Il nome del servizio ───────────────────────────────────────────

def test_un_nome_con_jolly_viene_rifiutato():
    # Get-Service espande i wildcard: `*` aggancerebbe decine di servizi e il
    # confronto con i nomi chiesti darebbe risultati a caso.
    for storto in ("Spool*", "Spool?er", "", "   "):
        with pytest.raises(ValueError):
            valida_nome(storto)
    assert valida_nome("  Spooler ") == "Spooler"


async def test_una_voce_col_nome_storto_non_ferma_le_altre(ssh):
    ssh(_blob("misti"))
    esiti = await WindowsServicesMonitor().status([
        {"name": "Spool*", "host": "192.0.2.12"},
        {"name": "Spooler", "host": "192.0.2.12"},
    ])
    per_nome = {e.name: e for e in esiti}
    assert per_nome["Spool*"].available is False
    assert "jolly" in per_nome["Spool*"].error
    assert per_nome["Spooler"].state == "running"


# ── Lo script remoto ───────────────────────────────────────────────

def test_lo_script_chiede_tutti_i_nomi_in_un_colpo_solo():
    s = script(["Spooler", "sshd", "WSearch"])
    assert "$n = @('Spooler','sshd','WSearch')" in s


def test_gli_apici_nel_nome_vengono_raddoppiati():
    assert "@('l''uno')" in script(["l'uno"])


def test_lo_script_forza_le_stringhe_e_l_array():
    s = script(["Spooler"])
    # Senza i cast, ConvertTo-Json di PowerShell 5.1 scrive gli enum come
    # interi: "Status":4 invece di "Status":"Running".
    assert "[string]$_.Status" in s and "[string]$_.StartType" in s
    # Senza @(), un esito solo diventa un oggetto e nessuno diventa null.
    assert "ConvertTo-Json -InputObject @($r)" in s


# ── Interpretazione delle catture reali ────────────────────────────

def test_lo_stato_arriva_come_stringa_non_come_numero():
    # E' questo test che impedisce di togliere i cast dallo script.
    for riga in json.loads(_blob("misti")):
        assert isinstance(riga["Status"], str)
        assert isinstance(riga["StartType"], str)


async def test_un_servizio_non_installato_non_e_un_guasto(ssh):
    ssh(_blob("misti"))
    esiti = await WindowsServicesMonitor().status([
        {"name": "Spooler", "host": "192.0.2.12"},
        {"name": "sshd", "host": "192.0.2.12"},
        {"name": "NonEsisto", "host": "192.0.2.12"},
    ])
    per_nome = {e.name: e for e in esiti}
    mancante = per_nome["NonEsisto"]
    # L'host ha risposto: si sa che il servizio non c'e', che e' diverso dal non
    # sapere niente.
    assert mancante.available is True
    assert mancante.state == "not-found"
    assert mancante.ok is False
    assert per_nome["Spooler"].ok is True
    assert per_nome["Spooler"].display_name == "Spooler di stampa"
    assert per_nome["Spooler"].start_type == "Automatic"


async def test_un_servizio_fermo_e_riconosciuto(ssh):
    ssh(_blob("fermi"))
    esiti = await WindowsServicesMonitor().status([
        {"name": "Fax", "host": "192.0.2.12"},
        {"name": "WSearch", "host": "192.0.2.12"},
    ])
    per_nome = {e.name: e for e in esiti}
    assert per_nome["Fax"].state == "stopped" and per_nome["Fax"].ok is False
    assert per_nome["WSearch"].state == "running"


def test_le_tre_forme_di_convertto_json():
    uno = json.loads(_blob("misti"))[0]
    assert interpreta(json.dumps(uno))["spooler"]["Name"] == "Spooler"   # oggetto solo
    assert interpreta(_blob("vuoto")) == {}                              # array vuoto
    assert interpreta("null") == {}                                      # null
    assert len(interpreta(_blob("misti"))) == 2                          # array


def test_output_non_interpretabile_e_un_errore_esplicito():
    for storto in ("", "   ", "non json"):
        with pytest.raises(ValueError):
            interpreta(storto)


# ── Host: uno per connessione, e mai quello di default ─────────────

async def test_un_host_per_connessione_e_un_comando_solo(ssh):
    stato = ssh(_blob("misti"))
    await WindowsServicesMonitor().status([
        {"name": "Spooler", "host": "192.0.2.12"},
        {"name": "sshd", "host": "192.0.2.12"},
        {"name": "Spooler", "host": "192.0.2.13"},
        {"name": "sshd", "host": "192.0.2.13"},
    ])
    assert sorted(stato["connessioni"]) == ["192.0.2.12", "192.0.2.13"]
    # Su Windows ogni canale e' un avvio completo di powershell.exe: un comando
    # per host, non uno per servizio.
    assert all(len(c.comandi) == 1 for c in stato["conn"])


async def test_una_voce_senza_host_non_apre_nessuna_connessione(ssh):
    # L'host di default sarebbe la macchina Linux del backend, dove un servizio
    # Windows non puo' esistere: tentare sarebbe solo un timeout.
    stato = ssh(_blob("misti"))
    esiti = await WindowsServicesMonitor().status([{"name": "Spooler", "host": ""}])
    assert stato["connessioni"] == []
    assert esiti[0].available is False and "host" in esiti[0].error


async def test_un_host_irraggiungibile_conserva_le_scelte_del_catalogo(ssh):
    ssh(errore=OSError("connessione rifiutata"))
    esiti = await WindowsServicesMonitor().status([
        {"name": "Spooler", "host": "192.0.2.12", "critical": True, "dashboard": True},
    ])
    assert esiti[0].available is False
    assert esiti[0].critical is True and esiti[0].dashboard is True
    assert esiti[0].error


async def test_un_host_giu_non_fa_sparire_i_servizi_degli_altri(ssh, monkeypatch):
    stato = ssh(_blob("misti"))
    reale = mod.WindowsServicesMonitor._per_host

    async def _per_host(self, host, voci):
        if host == "192.0.2.13":
            raise RuntimeError("host esploso")
        return await reale(self, host, voci)

    monkeypatch.setattr(mod.WindowsServicesMonitor, "_per_host", _per_host)
    esiti = await WindowsServicesMonitor().status([
        {"name": "Spooler", "host": "192.0.2.12"},
        {"name": "Spooler", "host": "192.0.2.13"},
    ])
    assert len(esiti) == 2
    assert {e.host: e.available for e in esiti} == {"192.0.2.12": True, "192.0.2.13": False}
    assert stato is not None


# ── Dashboard: il default e' fuori ─────────────────────────────────

async def test_un_servizio_critico_non_finisce_da_solo_in_dashboard(ssh):
    # Al contrario di systemd, dove la chiave assente eredita da `critical` per
    # non cambiare i cataloghi gia' scritti: qui non c'e' nessun catalogo
    # precedente da rispettare.
    ssh(_blob("misti"))
    esiti = await WindowsServicesMonitor().status([
        {"name": "Spooler", "host": "192.0.2.12", "critical": True},
    ])
    assert esiti[0].critical is True
    assert esiti[0].dashboard is False


async def test_l_etichetta_ripiega_sul_nome_visualizzato_remoto(ssh):
    ssh(_blob("misti"))
    esiti = await WindowsServicesMonitor().status([{"name": "sshd", "host": "192.0.2.12"}])
    assert esiti[0].to_dict()["label"] == "OpenSSH SSH Server"


def test_nessuna_voce_nessun_lavoro():
    import asyncio
    assert asyncio.run(WindowsServicesMonitor().status([])) == []


# ── Il confine del router ──────────────────────────────────────────

def test_il_router_pretende_l_host_e_rifiuta_i_jolly():
    from fastapi import HTTPException

    from routers.services import ServiceEntry, _entry_for_kind

    with pytest.raises(HTTPException) as senza_host:
        _entry_for_kind(ServiceEntry(kind="windows_service", name="Spooler"))
    assert senza_host.value.status_code == 400

    with pytest.raises(HTTPException):
        _entry_for_kind(ServiceEntry(kind="windows_service", name="Spool*",
                                     host="192.0.2.12"))

    e = _entry_for_kind(ServiceEntry(kind="windows_service", name=" Spooler ",
                                     label="Coda di stampa", host=" 192.0.2.12 ",
                                     critical=True))
    assert e == {"name": "Spooler", "label": "Coda di stampa", "critical": True,
                 "host": "192.0.2.12", "dashboard": True}


def test_il_catalogo_conosce_la_sezione_windows(tmp_path):
    from services.service_catalog import load_services_catalog

    f = tmp_path / "services.yaml"
    f.write_text("windows:\n  services:\n    - name: Spooler\n      host: 192.0.2.12\n")
    assert load_services_catalog(str(f))["windows"] == [
        {"name": "Spooler", "host": "192.0.2.12"}]


def test_un_catalogo_senza_sezione_windows_non_esplode(tmp_path):
    from services.service_catalog import load_services_catalog

    f = tmp_path / "services.yaml"
    f.write_text("systemd:\n  units:\n    - unit: ssh.service\n")
    assert load_services_catalog(str(f))["windows"] == []
