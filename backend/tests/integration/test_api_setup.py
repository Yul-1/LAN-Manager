"""
Primo avvio guidato (/api/setup/*).

La proprieta' che conta non e' che il wizard funzioni, ma che **smetta di
esistere** appena la configurazione c'e': quelle rotte scrivono config.yaml
senza autenticazione, quindi lasciarle vive sarebbe un modo di riconfigurare il
servizio scavalcando il login. Qui si verifica prima quello.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import bootstrap
from services.i18n import t
from services.config_store import get_config_store


@pytest.fixture
def senza_config(tmp_path, monkeypatch):
    """Sposta il config store su un file che non esiste: e' la condizione di
    primo avvio. La config vera della suite resta dov'e'."""
    store = get_config_store()
    originale = store.path
    monkeypatch.setattr(store, "path", tmp_path / "config.yaml")
    yield tmp_path / "config.yaml"
    store.path = originale


# ── Il gate: la superficie esiste solo prima della configurazione ──

def test_a_configurazione_fatta_le_rotte_non_esistono_piu(lan_client):
    """La suite gira con un config.yaml presente: e' gia' lo stato "configurato"."""
    assert bootstrap.serve_setup() is False
    assert lan_client.get("/api/setup/suggest").status_code == 404
    r = lan_client.post("/api/setup", json={"subnets": [{"cidr": "192.0.2.0/24"}]})
    assert r.status_code == 404, "scrittura di config senza login ancora raggiungibile"


def test_lo_stato_risponde_sempre(lan_client):
    """La SPA deve poter chiedere se serve il setup anche a giochi fatti."""
    r = lan_client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json() == {"setup_required": False}


def test_il_setup_non_sovrascrive_una_configurazione_esistente(senza_config, lan_client):
    """Doppia difesa: anche se il gate del router venisse aggirato, lo scrittore
    si rifiuta di toccare un file che c'e' gia'."""
    senza_config.write_text("subnets: []\n")
    with pytest.raises(ValueError) as e:
        bootstrap.scrivi_config([{"cidr": "192.0.2.0/24"}])
    assert e.value.chiave == "err.setupGiaFatto"


def test_da_fuori_dalla_lan_il_setup_e_rifiutato(senza_config, client):
    """`client` (non `lan_client`) arriva da "testclient", che non e' un IP
    privato: e' il caso di una richiesta che non viene dalla rete di casa."""
    r = client.post("/api/setup", json={"subnets": [{"cidr": "192.0.2.0/24"}]})
    assert r.status_code == 403
    assert r.json()["detail"] == t("err.setupSoloLan", lingua="en")


# ── Scrittura ──────────────────────────────────────────────────────

def test_il_primo_avvio_scrive_le_subnet_scelte(senza_config, lan_client):
    r = lan_client.post("/api/setup", json={
        "subnets": [{"cidr": "192.0.2.0/24", "label": "Casa", "scan": True},
                    {"cidr": "198.51.100.0/24", "label": "Lab", "scan": False}]})
    assert r.status_code == 200, r.text
    assert r.json()["subnets"] == 2
    assert r.json()["restart_required"] is True

    scritto = senza_config.read_text()
    assert "192.0.2.0/24" in scritto and "Casa" in scritto
    # Il router e' facoltativo: senza, non deve comparire una sezione vuota che
    # farebbe partire tentativi di connessione verso un host inesistente.
    assert "router:" not in scritto


def test_il_router_e_facoltativo_ma_se_c_e_viene_scritto(senza_config, lan_client):
    r = lan_client.post("/api/setup", json={
        "subnets": [{"cidr": "192.0.2.0/24"}],
        "router": {"host": "192.0.2.1", "user": "root"}})
    assert r.status_code == 200, r.text
    scritto = senza_config.read_text()
    assert "192.0.2.1" in scritto and "root" in scritto


def test_il_template_di_esempio_non_finisce_nella_config(senza_config, lan_client):
    """`_load_raw()` ripiega su config.example.yaml quando il file manca: se il
    primo avvio passasse di li', l'utente si ritroverebbe le subnet di
    documentazione (RFC 5737) e il router d'esempio, cioe' una rete non sua."""
    lan_client.post("/api/setup", json={"subnets": [{"cidr": "10.0.0.0/24"}]})
    scritto = senza_config.read_text()
    assert "10.0.0.0/24" in scritto
    assert "203.0.113" not in scritto, "relay d'esempio ereditato dal template"
    assert "198.51.100" not in scritto, "subnet d'esempio ereditata dal template"


# ── Validazione ────────────────────────────────────────────────────

@pytest.mark.parametrize("subnets, chiave", [
    ([], "err.serveSubnet"),
    ([{"cidr": "non-una-rete"}], "err.subnetNonValida"),
    ([{"cidr": ""}], "err.subnetNonValida"),
    ([{"cidr": "192.0.2.5/32"}], "err.subnetSingoloIndirizzo"),
])
def test_input_non_validi_sono_rifiutati_dicendo_perche(senza_config, lan_client,
                                                        subnets, chiave):
    r = lan_client.post("/api/setup", json={"subnets": subnets})
    assert r.status_code == 400
    # Il messaggio arriva reso: si confronta con il catalogo, cosi' il test
    # verifica QUALE errore e' e non come e' scritto in una lingua.
    # Un cidr vuoto diventa "?" nel messaggio: "subnet non valida: " da solo
    # non direbbe niente a chi legge.
    cidr = (subnets[0]["cidr"] or "?") if subnets else ""
    assert r.json()["detail"] == t(chiave, lingua="en", cidr=cidr)
    assert not senza_config.exists(), "un input rifiutato non deve lasciare un file"


def test_una_subnet_scritta_male_viene_normalizzata(senza_config, lan_client):
    """192.0.2.7/24 non e' l'indirizzo di rete: si scrive la rete, altrimenti
    la mappa mostrerebbe un CIDR che non combacia con quello dello scanner."""
    r = lan_client.post("/api/setup", json={"subnets": [{"cidr": "192.0.2.7/24"}]})
    assert r.status_code == 200, r.text
    assert "192.0.2.0/24" in senza_config.read_text()


# ── Proposta delle subnet ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_le_subnet_proposte_escludono_docker_e_vpn(monkeypatch):
    """Proporre docker0 o un'interfaccia WireGuard riempirebbe la mappa di reti
    che non sono la LAN dell'utente."""
    async def finte():
        return [
            {"name": "eth0", "addresses": [{"ip": "192.0.2.10", "prefix": 24, "family": "inet"}]},
            {"name": "docker0", "addresses": [{"ip": "172.17.0.1", "prefix": 16, "family": "inet"}]},
            {"name": "br-abc123", "addresses": [{"ip": "172.20.0.1", "prefix": 16, "family": "inet"}]},
            {"name": "wg0", "addresses": [{"ip": "10.100.0.2", "prefix": 24, "family": "inet"}]},
            {"name": "lo", "addresses": [{"ip": "127.0.0.1", "prefix": 8, "family": "inet"}]},
        ]
    monkeypatch.setattr(bootstrap, "_interfaces_ip", finte)
    proposte = await bootstrap.subnet_candidate()
    assert [p["cidr"] for p in proposte] == ["192.0.2.0/24"]
    assert proposte[0]["label"] == "LAN"
    assert proposte[0]["color"], "senza colore la subnet resta grigia in mappa"


@pytest.mark.asyncio
async def test_un_indirizzo_pubblico_sull_host_non_viene_proposto(monkeypatch):
    """Sarebbe l'uplink: scansionarlo vuol dire scansionare internet."""
    async def finte():
        return [{"name": "eth0", "addresses": [
            {"ip": "93.184.216.34", "prefix": 24, "family": "inet"},
            {"ip": "192.0.2.10", "prefix": 24, "family": "inet"}]}]
    monkeypatch.setattr(bootstrap, "_interfaces_ip", finte)
    proposte = await bootstrap.subnet_candidate()
    assert [p["cidr"] for p in proposte] == ["192.0.2.0/24"]


@pytest.mark.asyncio
async def test_se_le_interfacce_non_si_leggono_non_si_inventa_niente(monkeypatch):
    """Nessuna proposta e' meglio di una sbagliata: la pagina lascia scrivere
    le subnet a mano invece di mandare lo scanner su una rete a caso."""
    async def vuote():
        return []
    async def esplode():
        raise OSError("ip non disponibile")
    monkeypatch.setattr(bootstrap, "_interfaces_ip", vuote)
    monkeypatch.setattr(bootstrap, "_interfaces_nmap", vuote)
    assert await bootstrap.subnet_candidate() == []

    monkeypatch.setattr(bootstrap, "_interfaces_ip", esplode)
    assert await bootstrap.subnet_candidate() == []
