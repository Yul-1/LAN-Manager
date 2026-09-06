"""
Un GET per gruppo di endpoint, con i connettori sostituiti da fake.

Rete di sicurezza contro gli errori che nessun unit test vede: un import
sbagliato in un router, una dataclass che non si serializza, un campo rinominato
in un service e non aggiornato nel chiamante. Zero SSH, zero nmap, zero Docker.
"""
from __future__ import annotations

import json

import pytest

from tests.fakes import (FakeDockerManager, FakeRouterClient, FakeScanner, FakeSSH,
                         FakeWireGuard)

pytestmark = pytest.mark.integration


@pytest.fixture
def connettori_finti(monkeypatch):
    """Pre-carica i globali lazy: da qui in poi ogni get_x() torna il fake."""
    monkeypatch.setattr("services.openwrt._router", FakeRouterClient())
    monkeypatch.setattr("services.openwrt._ssh", FakeSSH())
    monkeypatch.setattr("services.docker_client._manager", FakeDockerManager())
    monkeypatch.setattr("services.scanner._scanner", FakeScanner())
    monkeypatch.setattr("services.wireguard._wg_service", FakeWireGuard())


ENDPOINT = [
    ("/api/system/info", "hostname"),
    ("/api/system/interfaces", None),
    ("/api/devices/", "devices"),
    ("/api/devices/dhcp/leases", None),
    ("/api/docker/containers", None),
    ("/api/docker/hosts", None),
    ("/api/docker/networks", None),
    ("/api/wan/status", None),
    ("/api/wan/firewall", "rules"),
    ("/api/logs/", "lines"),
    ("/api/wireguard/status", None),
    ("/api/wireguard/config", "config"),
    ("/api/config/", None),
    ("/api/config/secrets", "secrets"),
    ("/api/config/backups", None),
    ("/api/host/network", None),
    ("/api/alerts/rules", "rules"),
    ("/api/tools/", None),
    ("/api/terminal/hosts", None),
    ("/api/snapshot", "meta"),
]


@pytest.mark.parametrize("rotta,chiave", ENDPOINT)
def test_ogni_gruppo_risponde_200_con_json_valido(auth_client, connettori_finti, rotta, chiave):
    r = auth_client.get(rotta)
    assert r.status_code == 200, f"{rotta} -> {r.status_code}: {r.text[:200]}"
    corpo = r.json()                       # solleva se non e' JSON
    if chiave:
        assert chiave in corpo, f"{rotta} non contiene '{chiave}'"


def test_i_dispositivi_arrivano_dallo_scanner(auth_client, connettori_finti):
    devices = auth_client.get("/api/devices/").json()["devices"]
    assert len(devices) == 1
    assert devices[0]["ips"] == ["192.0.2.10"]
    assert devices[0]["name"] == "host"      # display_name: hostname senza dominio


def test_i_log_del_router_arrivano_gia_parsati(auth_client, connettori_finti):
    righe = auth_client.get("/api/logs/").json()["lines"]
    assert righe[0]["level"] == "error"
    assert righe[0]["src"] == "dnsmasq"


def test_il_filtro_per_livello_si_applica_dopo_il_parsing(auth_client, connettori_finti):
    # Non e' un grep sul testo: quello che si chiede combacia con il livello
    # mostrato accanto alla riga.
    assert auth_client.get("/api/logs/?level=error").json()["count"] == 1
    assert auth_client.get("/api/logs/?level=warn").json()["count"] == 0


def test_la_config_letta_dallapi_ha_i_segreti_mascherati(auth_client, connettori_finti):
    testo = auth_client.get("/api/config/").text
    assert "$2b$" not in testo
    assert "chiave-di-test-non-usare-in-produzione" not in testo


def test_i_segreti_espongono_solo_lo_stato_mai_i_valori(auth_client, connettori_finti):
    for voce in auth_client.get("/api/config/secrets").json()["secrets"]:
        assert set(voce) <= {"id", "label", "set"}
        assert isinstance(voce["set"], bool)


# ── Alert ──────────────────────────────────────────────────────────

@pytest.fixture
def config_di_prova(monkeypatch, tmp_path):
    """ConfigStore su un file usa e getta: silenziare qui non deve toccare la
    configurazione condivisa dalla suite."""
    import routers.alerts as mod
    from services.config_store import ConfigStore

    store = ConfigStore()
    monkeypatch.setattr(store, "path", tmp_path / "config.yaml")
    monkeypatch.setattr(mod, "get_config_store", lambda: store)
    return store


def test_il_catalogo_delle_regole_e_completo(auth_client, connettori_finti):
    corpo = auth_client.get("/api/alerts/rules").json()
    chiavi = {r["rule"] for r in corpo["rules"]}
    assert "docker.restart_loop" in chiavi and "host.subnet_duplicata" in chiavi
    assert all(r["scope"] and r["descr"] for r in corpo["rules"])


def test_silenziare_scrive_il_motivo_e_non_chiede_il_riavvio(auth_client, config_di_prova):
    r = auth_client.post("/api/alerts/silence", json={
        "rule": "host.subnet_duplicata", "subject": "192.0.2.0/24",
        "reason": "due schede per scelta"})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is False
    salvati = config_di_prova.read_section("alerts_silenced")
    assert salvati[0]["reason"] == "due schede per scelta" and salvati[0]["since"] > 0


def test_silenziare_due_volte_non_duplica(auth_client, config_di_prova):
    corpo = {"rule": "docker.restart_loop", "subject": "nas/uno", "reason": "lo so"}
    auth_client.post("/api/alerts/silence", json=corpo)
    r = auth_client.post("/api/alerts/silence", json=corpo)
    assert r.status_code == 200 and r.json()["silenced"] == 1


def test_senza_motivo_non_si_silenzia_niente(auth_client, config_di_prova):
    r = auth_client.post("/api/alerts/silence", json={
        "rule": "docker.restart_loop", "subject": "nas/uno", "reason": "   "})
    assert r.status_code == 400 and "motivo" in r.json()["detail"]
    assert not config_di_prova.path.exists()      # niente scritto a meta'


def test_una_regola_inventata_viene_rifiutata(auth_client, config_di_prova):
    r = auth_client.post("/api/alerts/silence", json={
        "rule": "docker.inventata", "reason": "boh"})
    assert r.status_code == 400 and "sconosciuta" in r.json()["detail"]


# ── Tool di rete in diretta (/api/tools/stream) ────────────────────
#  L'output esce mentre il comando gira. Il processo qui e' finto: quello che
#  si verifica e' il contratto della rotta (NDJSON, sessione, 400 sull'input
#  rifiutato), non il comportamento di ping o nmap.

class _PipeFinta:
    def __init__(self, pezzi):
        self._pezzi = list(pezzi)

    async def read(self, n):
        return self._pezzi.pop(0) if self._pezzi else b""


class _ProcessoFinto:
    def __init__(self, pezzi):
        self.stdout = _PipeFinta(pezzi)
        self.returncode = None

    def kill(self):
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def processo_finto(monkeypatch):
    from services import nettools

    async def _mai_bloccato(host):
        return False

    async def _exec(*args, **kw):
        return _ProcessoFinto([b"prima riga\n", b"seconda riga\n"])

    monkeypatch.setattr(nettools, "target_blocked", _mai_bloccato)
    monkeypatch.setattr(nettools.asyncio, "create_subprocess_exec", _exec)


def test_lo_stream_dei_tool_esige_la_sessione(client):
    r = client.post("/api/tools/stream", json={"tool": "ping", "target": "192.0.2.1"})
    assert r.status_code == 401


def test_lo_stream_torna_ndjson_con_start_out_e_end(auth_client, processo_finto):
    r = auth_client.post("/api/tools/stream",
                         json={"tool": "ping", "target": "192.0.2.1", "options": {}})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    # Senza questo header nginx accumula la risposta in un buffer e la consegna
    # tutta alla fine: lo streaming sarebbe solo apparente.
    assert r.headers.get("x-accel-buffering") == "no"

    eventi = [json.loads(riga) for riga in r.text.splitlines() if riga.strip()]
    assert [e["type"] for e in eventi] == ["start", "out", "out", "end"]
    assert eventi[0]["command"].startswith("ping ")
    assert "".join(e["text"] for e in eventi if e["type"] == "out") == \
        "prima riga\nseconda riga\n"
    assert eventi[-1]["exit_code"] == 0


def test_uno_stream_con_input_rifiutato_e_un_400_vero(auth_client, processo_finto):
    # Non un errore infilato dentro un corpo gia' cominciato: il client non
    # saprebbe distinguerlo dall'output del comando.
    r = auth_client.post("/api/tools/stream",
                         json={"tool": "ping", "target": "-oX /tmp/out"})
    assert r.status_code == 400
    assert "bersaglio" in r.json()["detail"]


def test_un_tool_senza_output_progressivo_non_si_streamma(auth_client, processo_finto):
    r = auth_client.post("/api/tools/stream", json={"tool": "whois", "target": "esempio.it"})
    assert r.status_code == 400


def test_il_catalogo_dichiara_quali_tool_escono_man_mano(auth_client):
    tools = {t["id"]: t for t in auth_client.get("/api/tools/").json()["tools"]}
    assert tools["ping"]["stream"] is True and tools["nmap"]["stream"] is True
    # La misura di velocita' ha senso solo a fine download: un risultato
    # parziale non esiste, e prometterlo sarebbe una bugia della UI.
    assert tools["speedtest"]["stream"] is False and tools["whois"]["stream"] is False


# ── Riavvio del servizio (/api/config/restart) ─────────────────────
#  Il processo esce e a riaccenderlo e' Docker: la rotta non deve mai far
#  uscire il processo quando nessuno lo rimetterebbe in piedi.

@pytest.fixture
def uscita_finta(monkeypatch):
    """Registra la richiesta di uscita invece di spegnere la suite."""
    from routers import config_api
    chiamate = []
    monkeypatch.setattr(config_api, "programma_uscita", lambda *a, **k: chiamate.append(a))
    return chiamate


def _stato_riavvio(monkeypatch, **campi):
    from routers import config_api
    base = {"available": True, "policy": "unless-stopped", "container": "abc123", "reason": ""}
    base.update(campi)

    async def _stato():
        return base
    monkeypatch.setattr(config_api, "stato_riavvio", _stato)


def test_il_riavvio_esige_la_sessione(client, uscita_finta, monkeypatch):
    _stato_riavvio(monkeypatch)
    assert client.post("/api/config/restart").status_code == 401
    assert not uscita_finta, "nessuna uscita programmata senza sessione"


def test_il_riavvio_chiede_di_uscire_e_lo_scrive_nellaudit(auth_client, uscita_finta,
                                                            monkeypatch):
    from routers import config_api
    righe = []
    monkeypatch.setattr(config_api, "audit",
                        lambda evento, **campi: righe.append((evento, campi)))
    _stato_riavvio(monkeypatch)
    r = auth_client.post("/api/config/restart")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert uscita_finta, "il processo deve uscire: e' Docker a riaccenderlo"
    # Chiude ogni sessione aperta, terminale compreso: deve restare tracciato.
    assert righe and righe[0][0] == "servizio.riavvio"
    assert righe[0][1]["policy"] == "unless-stopped"


def test_senza_chi_lo_riaccenda_la_rotta_rifiuta(auth_client, uscita_finta, monkeypatch):
    _stato_riavvio(monkeypatch, available=False, policy="no",
                   reason="il container ha politica di riavvio 'no'")
    r = auth_client.post("/api/config/restart")
    assert r.status_code == 409
    assert "politica di riavvio" in r.json()["detail"]
    assert not uscita_finta, "spegnere e basta lascerebbe la dashboard irraggiungibile"


def test_lo_stato_del_riavvio_si_legge_con_la_sessione(auth_client, monkeypatch):
    _stato_riavvio(monkeypatch, available=False, reason="non gira in un container")
    r = auth_client.get("/api/config/restart")
    assert r.status_code == 200
    assert r.json()["available"] is False and r.json()["reason"]
