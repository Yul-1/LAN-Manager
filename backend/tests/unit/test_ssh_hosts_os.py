"""
Riconoscimento del sistema operativo di un host SSH (services/ssh_hosts.py).

Serve perche' su Windows la shell di default via SSH e' cmd.exe: mandargli lo
script POSIX delle risorse non produce un errore, produce spazzatura che sembra
un host rotto. La regola e' che si sonda una volta sola, che la configurazione
vince sulla sonda, e che un esito incerto non viene mai indovinato.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import SSHDiscovery
from services import ssh_hosts
from services.ssh_hosts import TETTO_RIGA_COMANDO, comando_powershell, risolvi_os

PS = "powershell.exe -NoProfile -NonInteractive -EncodedCommand"


class FintaConnessione:
    """Risponde solo ai comandi che le si insegnano; a tutti gli altri risponde
    a vuoto, che e' esattamente il comportamento di cmd.exe con `uname`."""

    def __init__(self, risposte: dict[str, str]):
        self._risposte = risposte
        self.eseguiti: list[str] = []

    async def run(self, comando, **kwargs):
        self.eseguiti.append(comando)
        for prefisso, uscita in self._risposte.items():
            if comando.startswith(prefisso):
                return SimpleNamespace(stdout=uscita, stderr="", exit_status=0)
        return SimpleNamespace(stdout="", stderr="comando non riconosciuto", exit_status=1)


@pytest.fixture
def host_ssh(monkeypatch):
    """Sostituisce l'elenco host letto dal file e azzera la memoria della sonda,
    che altrimenti passerebbe da un test all'altro."""
    def _imposta(**campi):
        cfg = SSHDiscovery(hosts=[{"ip": "192.0.2.12", **campi}])
        monkeypatch.setattr(ssh_hosts, "live_ssh_config", lambda: cfg)
        ssh_hosts._os_cache.clear()
    ssh_hosts._os_cache.clear()
    return _imposta


# ── La configurazione vince sulla sonda ────────────────────────────

@pytest.mark.parametrize("dichiarato", ["linux", "windows"])
async def test_un_os_dichiarato_non_fa_partire_nessuna_sonda(host_ssh, dichiarato):
    host_ssh(os=dichiarato)
    conn = FintaConnessione({})
    assert await risolvi_os("192.0.2.12", conn) == dichiarato
    assert conn.eseguiti == [], "con l'os dichiarato non si interroga la macchina"


def test_un_os_scritto_male_viene_rifiutato_dalla_configurazione():
    # Meglio un salvataggio rifiutato che un valore che fallisce ad ogni ciclo.
    with pytest.raises(ValueError):
        SSHDiscovery(hosts=[{"ip": "192.0.2.12", "os": "win"}])


def test_senza_campo_os_vale_auto():
    assert SSHDiscovery(hosts=[{"ip": "192.0.2.12"}]).hosts[0].os == "auto"


# ── La sonda ───────────────────────────────────────────────────────

async def test_uname_riconosce_linux_al_primo_colpo(host_ssh):
    host_ssh(os="auto")
    conn = FintaConnessione({"uname -s": "Linux\n"})
    assert await risolvi_os("192.0.2.12", conn) == "linux"
    assert conn.eseguiti == ["uname -s"], "riconosciuto subito: niente seconda domanda"


async def test_uname_muto_porta_alla_domanda_a_powershell(host_ssh):
    host_ssh(os="auto")
    # cmd.exe non conosce `uname` e si lamenta sullo stderr: qui il segnale e'
    # lo stdout vuoto, non un errore.
    conn = FintaConnessione({PS: "Windows_NT\n"})
    assert await risolvi_os("192.0.2.12", conn) == "windows"
    assert len(conn.eseguiti) == 2


async def test_un_host_che_non_risponde_a_nessuna_delle_due_resta_sconosciuto(host_ssh):
    host_ssh(os="auto")
    assert await risolvi_os("192.0.2.12", FintaConnessione({})) == "unknown"


async def test_un_esito_incerto_non_viene_memorizzato(host_ssh):
    # Altrimenti un host che ha risposto male una volta resterebbe classificato
    # male fino al riavvio del servizio.
    host_ssh(os="auto")
    muta = FintaConnessione({})
    assert await risolvi_os("192.0.2.12", muta) == "unknown"
    parlante = FintaConnessione({"uname -s": "Linux\n"})
    assert await risolvi_os("192.0.2.12", parlante) == "linux"
    assert parlante.eseguiti, "la sonda deve essere ritentata"


async def test_la_sonda_riuscita_si_ricorda(host_ssh):
    host_ssh(os="auto")
    await risolvi_os("192.0.2.12", FintaConnessione({"uname -s": "Linux\n"}))
    seconda = FintaConnessione({})
    assert await risolvi_os("192.0.2.12", seconda) == "linux"
    assert seconda.eseguiti == [], "la seconda volta la risposta e' gia' nota"


# ── Consegna dello script PowerShell ───────────────────────────────

def test_lo_script_viaggia_codificato_in_utf16():
    import base64
    comando = comando_powershell("$env:OS")
    b64 = comando.rsplit(" ", 1)[1]
    assert base64.b64decode(b64).decode("utf-16-le") == "$env:OS"


def test_uno_script_troppo_lungo_viene_fermato_qui():
    # cmd.exe troncherebbe la riga senza dire niente: il guasto va sollevato
    # dove si puo' ancora capire, non sulla macchina remota.
    with pytest.raises(ValueError, match="troppo lungo"):
        comando_powershell("x" * TETTO_RIGA_COMANDO)
