"""
Costruzione dei client Docker multi-host (services/docker_client.py).

Regressione del 2026-08-16: homeserver compare sia come host locale (socket Unix)
sia dentro `discovery.ssh.hosts`, perche' e' anche un host SSH della LAN.
Interrogarlo due volte elencava **ogni suo container due volte**. Il filtro sta
in `_build`, che salta un host SSH quando `_is_local_address` dice che e' questa
macchina.

`_is_local_address` apre una connect UDP per chiedere al kernel se l'indirizzo e'
nostro: qui viene sostituita, cosi' il test non dipende dalla rete della macchina
su cui gira.
"""
from __future__ import annotations

import logging

import pytest

from config import DockerHost, SSHHost, settings
from services import docker_client
from services.docker_client import DockerManager, EngineDockerClient, SSHDockerClient

LOCALE = "192.0.2.30"


@pytest.fixture
def solo_locale_e_finto(monkeypatch):
    """Il kernel riconosce come propria solo LOCALE."""
    monkeypatch.setattr(docker_client, "_is_local_address", lambda ip: ip == LOCALE)
    monkeypatch.setattr(settings.docker, "local_enabled", True)
    monkeypatch.setattr(settings.docker, "local_name", "homeserver")
    monkeypatch.setattr(settings.docker, "hosts", [])
    monkeypatch.setattr(settings.docker, "autodiscover", True)
    monkeypatch.setattr(settings.discovery.ssh, "enabled", True)
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [])


# ── La regressione ─────────────────────────────────────────────────

def test_lhost_locale_presente_anche_fra_gli_host_ssh_non_viene_contato_due_volte(
        solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [
        SSHHost(ip=LOCALE),               # e' questa macchina: gia' coperta dal socket
        SSHHost(ip="192.0.2.10"),         # host remoto vero
    ])
    clients = DockerManager()._build()
    assert len(clients) == 2, "l'host locale e' stato interrogato due volte"
    assert isinstance(clients[0], EngineDockerClient)      # socket locale
    assert [c.name for c in clients] == ["homeserver", "192.0.2.10"]


def test_senza_host_locale_lo_stesso_ip_via_ssh_e_legittimo(solo_locale_e_finto, monkeypatch):
    # Se il socket locale e' spento, interrogare quell'IP via SSH e' l'unico modo.
    monkeypatch.setattr(settings.docker, "local_enabled", False)
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [SSHHost(ip=LOCALE)])
    clients = DockerManager()._build()
    assert len(clients) == 1
    assert isinstance(clients[0], SSHDockerClient)


def test_un_host_esplicito_non_viene_ripetuto_dallautodiscovery(solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.docker, "hosts",
                        [DockerHost(name="nas", host="192.0.2.10")])
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [SSHHost(ip="192.0.2.10")])
    clients = DockerManager()._build()
    assert len(clients) == 2                                # locale + nas, non tre
    assert [c.name for c in clients] == ["homeserver", "nas"]


# ── Composizione dei client ────────────────────────────────────────

def test_host_esplicito_tcp_usa_engine_e_ssh_usa_ssh(solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.docker, "hosts", [
        DockerHost(name="via-tcp", method="tcp", host="192.0.2.20", port=2375),
        DockerHost(name="via-ssh", method="ssh", host="192.0.2.21"),
    ])
    clients = DockerManager()._build()
    per_nome = {c.name: c for c in clients}
    assert isinstance(per_nome["via-tcp"], EngineDockerClient)
    assert isinstance(per_nome["via-ssh"], SSHDockerClient)


def test_autodiscovery_spenta_ignora_gli_host_ssh(solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.docker, "autodiscover", False)
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [SSHHost(ip="192.0.2.10")])
    assert len(DockerManager()._build()) == 1                # solo il locale


def test_discovery_ssh_disabilitata_ignora_gli_host_ssh(solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.discovery.ssh, "enabled", False)
    monkeypatch.setattr(settings.discovery.ssh, "hosts", [SSHHost(ip="192.0.2.10")])
    assert len(DockerManager()._build()) == 1


def test_host_esplicito_senza_indirizzo_viene_ignorato(solo_locale_e_finto, monkeypatch):
    monkeypatch.setattr(settings.docker, "hosts", [DockerHost(name="incompleto")])
    assert len(DockerManager()._build()) == 1


# ── Raggiungibilita' per host ──────────────────────────────────────
# Prima un host SSH irraggiungibile era invisibile: `_run` logga e ritorna
# ("", 1), `list_containers` ritorna [], e a valle una lista vuota e' identica a
# "questo host non ha container". I container sparivano in silenzio.

async def test_un_host_ssh_irraggiungibile_viene_dichiarato(monkeypatch):
    import asyncssh

    client = SSHDockerClient(name="nas", host="192.0.2.10", user="tester")

    def connect_impossibile(**_):
        # Solleva subito, come fa asyncssh quando la connect non parte proprio.
        raise asyncssh.Error(code=1, reason="Connection refused")

    monkeypatch.setattr(asyncssh, "connect", connect_impossibile)
    assert await client.list_containers() == []        # forma invariata
    assert client.last_error                            # ma il motivo c'e'

    mgr = DockerManager()
    monkeypatch.setattr(mgr, "_clients", [client])
    riga = mgr.hosts()[0]
    assert riga["name"] == "nas" and riga["reachable"] is False
    assert "refused" in riga["error"].lower()
    # Non ha mai risposto: e' una questione di configurazione, non un guasto.
    assert riga["seen_ok"] is False


def test_un_host_senza_errori_risulta_raggiungibile():
    mgr = DockerManager()
    mgr._clients = [EngineDockerClient(name="homeserver", socket="/var/run/docker.sock")]
    assert mgr.hosts() == [{"name": "homeserver", "reachable": True,
                            "error": "", "seen_ok": False}]


# ── Il log segna i cambi di stato, non ogni tentativo ──────────────
#
# Misurato il 2026-09-04: sei host Docker spenti, due chiamate a testa per ciclo
# lento, e un terzo di tutto il log del backend era la stessa frase ripetuta.
# Con `logs.max_rows` a 50.000 righe quel rumore riduceva l'orizzonte
# dell'archivio a poco piu' di un giorno.

class _ConnessioneFinta:
    """Il minimo che serve a `_run`: un context manager con `.run()`."""

    def __init__(self, stdout=""):
        self._stdout = stdout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, cmd, check=False):
        class Esito:
            stdout = self._stdout
            exit_status = 0
        return Esito()


@pytest.fixture
def ssh_pilotato(monkeypatch):
    """Fa fallire o riuscire la connessione SSH a comando."""
    import asyncssh
    stato = {"errore": OSError("timeout")}

    def connect(**kw):
        if stato["errore"]:
            raise stato["errore"]
        return _ConnessioneFinta("ok\n")

    monkeypatch.setattr(asyncssh, "connect", connect)
    return stato


async def test_un_host_spento_non_riempie_il_log_ad_ogni_giro(ssh_pilotato, caplog):
    client = SSHDockerClient(name="spento", host="192.0.2.3", user="tester")
    with caplog.at_level(logging.WARNING, logger="docker"):
        for _ in range(6):
            await client._run("docker ps")

    righe = [r for r in caplog.records if "192.0.2.3" in r.getMessage()]
    assert len(righe) == 1, "sei tentativi, una riga: il guasto e' uno solo"
    # Quello che la UI e l'alert leggono NON e' il log: resta aggiornato sempre.
    assert client.last_error


async def test_la_ripresa_si_scrive_e_un_nuovo_guasto_pure(ssh_pilotato, caplog):
    client = SSHDockerClient(name="host", host="192.0.2.3", user="tester")
    with caplog.at_level(logging.INFO, logger="docker"):
        await client._run("docker ps")          # giu'
        ssh_pilotato["errore"] = None
        await client._run("docker ps")          # torna su
        await client._run("docker ps")          # ancora su: non si ripete
        ssh_pilotato["errore"] = OSError("connection refused")
        await client._run("docker ps")          # giu' di nuovo

    messaggi = [r.getMessage() for r in caplog.records if "192.0.2.3" in r.getMessage()]
    assert len(messaggi) == 3
    assert "timeout" in messaggi[0]
    assert "torna a rispondere" in messaggi[1]
    assert "connection refused" in messaggi[2]


async def test_un_errore_diverso_e_uno_stato_diverso(ssh_pilotato, caplog):
    """Due guasti differenti sullo stesso host sono due notizie: comprimerli
    nasconderebbe il passaggio da 'irraggiungibile' a 'chiave rifiutata'."""
    client = SSHDockerClient(name="host", host="192.0.2.3", user="tester")
    with caplog.at_level(logging.WARNING, logger="docker"):
        await client._run("docker ps")
        ssh_pilotato["errore"] = OSError("permission denied")
        await client._run("docker ps")

    assert len([r for r in caplog.records if "192.0.2.3" in r.getMessage()]) == 2


# ── Socket Docker locale negato ────────────────────────────────────
# Sulla VM di prova (2026-09-06) il compose della radice non concedeva al
# container il gruppo docker dell'host: il log diceva solo
# "[Errno 13] Permission denied", che non indica nessun rimedio a chi installa.

def test_il_socket_locale_negato_suggerisce_docker_gid(caplog):
    client = EngineDockerClient(name="localhost", socket="/var/run/docker.sock")
    with caplog.at_level(logging.ERROR, logger="docker"):
        client._traccia("[Errno 13] Permission denied")

    messaggio = caplog.records[-1].getMessage()
    assert "Errno 13" in messaggio, "l'errore vero resta"
    assert "DOCKER_GID" in messaggio, "e accanto ci sta il rimedio"


def test_un_engine_remoto_non_riceve_il_suggerimento(caplog):
    """Su un Engine via TCP il gruppo docker dell'host locale non c'entra:
    suggerirlo manderebbe fuori strada."""
    client = EngineDockerClient(name="nas", host="192.0.2.10")
    with caplog.at_level(logging.ERROR, logger="docker"):
        client._traccia("[Errno 13] Permission denied")

    assert "DOCKER_GID" not in caplog.records[-1].getMessage()
