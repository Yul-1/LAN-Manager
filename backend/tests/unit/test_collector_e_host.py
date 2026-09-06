"""
Serie del traffico (services/collector.py) e diagnosi della rete dell'host
(services/host_inspector.py).

Il calcolo del rate e' quello corretto: prima si plottavano i contatori
cumulativi senza unita'. Ora si derivano bit/s, e dove il confronto non ha senso
si lascia un buco invece di uno zero inventato.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config import settings
from services import host_inspector
from services.collector import (DataCollector, _fmt_uptime, _mark_shared_devices,
                                _pick_wan_interface)
from services.host_inspector import _counters, _network_of

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def orologio(monkeypatch):
    import services.collector as mod
    ora = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ora[0])
    return ora


def _wan(rx, tx, name="wan"):
    return {"name": name, "rx_bytes": rx, "tx_bytes": tx}


# ── Rate del traffico ──────────────────────────────────────────────

def test_la_prima_lettura_non_ha_un_rate(orologio):
    assert DataCollector()._wan_rates(_wan(1000, 2000)) == (None, None)


def test_il_rate_e_in_bit_al_secondo(orologio):
    c = DataCollector()
    c._wan_rates(_wan(0, 0))
    orologio[0] += 10.0
    # 1250 byte in 10s = 125 byte/s = 1000 bit/s
    assert c._wan_rates(_wan(12500, 1250)) == (10000.0, 1000.0)


def test_cambiare_interfaccia_di_uplink_azzera_il_confronto(orologio):
    c = DataCollector()
    c._wan_rates(_wan(1000, 1000, name="wan"))
    orologio[0] += 10.0
    assert c._wan_rates(_wan(5000, 5000, name="tethering")) == (None, None)


def test_contatori_che_tornano_indietro_non_producono_un_picco(orologio):
    # Riavvio del router, o wrap dei contatori a 32 bit.
    c = DataCollector()
    c._wan_rates(_wan(10_000_000, 10_000_000))
    orologio[0] += 10.0
    assert c._wan_rates(_wan(100, 100)) == (None, None)


def test_due_letture_nello_stesso_istante_non_dividono_per_zero(orologio):
    c = DataCollector()
    c._wan_rates(_wan(0, 0))
    assert c._wan_rates(_wan(1000, 1000)) == (None, None)


def test_senza_interfaccia_wan_niente_rate(orologio):
    c = DataCollector()
    assert c._wan_rates(None) == (None, None)
    # E la lettura successiva riparte da capo, non confronta con il nulla.
    assert c._wan_rates(_wan(1000, 1000)) == (None, None)


def test_traffico_fermo_da_zero_non_none(orologio):
    c = DataCollector()
    c._wan_rates(_wan(1000, 1000))
    orologio[0] += 10.0
    assert c._wan_rates(_wan(1000, 1000)) == (0.0, 0.0)


# ── Scelta dell'interfaccia di uplink ──────────────────────────────

def test_linterfaccia_configurata_vince(monkeypatch):
    monkeypatch.setattr(settings.router, "wan_interface", "tethering")
    ifaces = [{"name": "wan", "rx_bytes": 999}, {"name": "tethering", "rx_bytes": 0}]
    assert _pick_wan_interface(ifaces)["name"] == "tethering"


def test_senza_configurazione_si_usa_il_primo_candidato_noto(monkeypatch):
    monkeypatch.setattr(settings.router, "wan_interface", "")
    monkeypatch.setattr(settings.router, "wan_candidates", ["wan", "usb0"])
    ifaces = [{"name": "lan", "rx_bytes": 999}, {"name": "usb0", "rx_bytes": 1}]
    assert _pick_wan_interface(ifaces)["name"] == "usb0"


def test_ultimo_ripiego_la_prima_interfaccia_con_traffico(monkeypatch):
    monkeypatch.setattr(settings.router, "wan_interface", "")
    monkeypatch.setattr(settings.router, "wan_candidates", [])
    ifaces = [{"name": "lan", "rx_bytes": 0}, {"name": "eth9", "rx_bytes": 5}]
    assert _pick_wan_interface(ifaces)["name"] == "eth9"


def test_una_interfaccia_configurata_ma_assente_non_blocca_il_ripiego(monkeypatch):
    monkeypatch.setattr(settings.router, "wan_interface", "non-esiste")
    monkeypatch.setattr(settings.router, "wan_candidates", ["wan"])
    assert _pick_wan_interface([{"name": "wan", "rx_bytes": 1}])["name"] == "wan"


def test_nessuna_interfaccia():
    assert _pick_wan_interface([]) is None


# ── Reti che condividono lo stesso dispositivo ─────────────────────

def test_le_reti_sullo_stesso_bridge_non_ripetono_i_byte():
    # lan, lanPC2 e lanVM stanno sullo stesso br-lan: i contatori sono del
    # dispositivo, non della singola rete.
    rows = [{"name": n, "ifname": "br-lan"} for n in ("lan", "lanPC2", "lanVM")]
    _mark_shared_devices(rows)
    assert [r["counters_own"] for r in rows] == [True, False, False]
    assert rows[0]["shared_with"] == ["lanPC2", "lanVM"]
    assert rows[1]["shared_with"] == ["lan", "lanVM"]


def test_una_rete_da_sola_rivendica_i_propri_byte():
    rows = [{"name": "wan", "ifname": "eth0"}]
    _mark_shared_devices(rows)
    assert rows[0]["counters_own"] is True and rows[0]["shared_with"] == []


def test_le_reti_senza_dispositivo_vengono_ignorate():
    rows = [{"name": "sconosciuta", "ifname": ""}]
    _mark_shared_devices(rows)
    assert "counters_own" not in rows[0]


def test_uptime_leggibile_del_collector():
    # Nota: questo e' in inglese, quello di host_metrics in italiano.
    assert _fmt_uptime(0) == "0m"
    assert _fmt_uptime(3661) == "1h 1m"
    assert _fmt_uptime(86400 + 7200) == "1d 2h 0m"


# ── /proc/net/dev ──────────────────────────────────────────────────

def test_i_contatori_si_leggono_da_proc_net_dev(monkeypatch):
    monkeypatch.setattr(host_inspector, "_PROC_DEV", FIXTURES / "proc_net_dev.txt")
    stats = _counters()
    assert "lo" in stats
    assert set(stats["lo"]) == {"rx_bytes", "rx_packets", "rx_errors", "rx_dropped",
                                "tx_bytes", "tx_packets", "tx_errors", "tx_dropped"}
    assert all(isinstance(v, int) for v in stats["lo"].values())
    assert stats["lo"]["rx_bytes"] > 0


def test_un_proc_net_dev_illeggibile_non_solleva(monkeypatch, tmp_path):
    monkeypatch.setattr(host_inspector, "_PROC_DEV", tmp_path / "non-esiste")
    assert _counters() == {}


# ── Rete dell'host ─────────────────────────────────────────────────
# Le regole che leggono queste interfacce stanno in tests/unit/test_alerts.py:
# non sono piu' calcolate qui.

def test_calcolo_della_rete_di_un_indirizzo():
    assert _network_of("192.0.2.30", 24) == "192.0.2.0/24"
    # Prefisso assente: l'indirizzo vale per se stesso, quindi /32. Due schede
    # senza prefisso non risultano cosi' sulla stessa subnet per sbaglio.
    assert _network_of("192.0.2.30", None) == "192.0.2.30/32"
    assert _network_of("non-un-ip", 24) == ""


# ── Aggancio allo storico persistente ──────────────────────────────

@pytest.fixture
def storico_finto(monkeypatch):
    """Sostituisce lo storico con un raccoglitore in memoria: qui interessa
    che il collector lo chiami con il punto giusto, non che SQLite funzioni
    (quello lo copre test_history_store.py)."""
    class Finto:
        def __init__(self):
            self.punti, self.compattazioni, self.dovuta = [], 0, False

        def add_traffic(self, point):
            self.punti.append(point)

        def due(self):
            return self.dovuta

        def compact(self):
            self.compattazioni += 1

    finto = Finto()
    import services.collector as mod
    monkeypatch.setattr(mod, "get_history", lambda: finto)

    # Nessun ping vero: la suite non tocca la rete.
    async def _senza_ping():
        return 12.5
    monkeypatch.setattr(mod, "_measure_latency", _senza_ping)
    return finto


def _wan_completa(rx=1000, tx=2000):
    """Interfaccia come la produce _collect_interfaces: il punto di traffico
    legge anche i totali in MB, non solo i contatori grezzi."""
    return {"name": "wan", "ifname": "eth0", "up": True, "ip4": "192.0.2.30",
            "rx_bytes": rx, "tx_bytes": tx,
            "rx_mb": rx / 1_048_576, "tx_mb": tx / 1_048_576}


async def test_il_punto_di_traffico_finisce_nello_storico(orologio, storico_finto):
    c = DataCollector()
    c._snapshot["interfaces"] = [_wan_completa()]
    await c._collect_traffic_point()
    assert len(storico_finto.punti) == 1
    # Stesso punto che finisce nella serie viva: una sola verita', due sedi.
    assert storico_finto.punti[0] == c._snapshot["traffic_series"][-1]


async def test_uno_storico_rotto_non_ferma_la_serie_viva(orologio, storico_finto, caplog):
    """Lo storico e' un di piu': se si guasta, il monitoraggio deve proseguire."""
    def esplode(_point):
        raise OSError("disco pieno")
    storico_finto.add_traffic = esplode

    c = DataCollector()
    c._snapshot["interfaces"] = [_wan_completa()]
    await c._collect_traffic_point()          # non deve sollevare
    assert len(c._snapshot["traffic_series"]) == 1
    assert "collect_traffic" in caplog.text


async def test_la_compattazione_gira_solo_quando_e_dovuta(storico_finto):
    c = DataCollector()
    await c._compatta_storico()
    assert storico_finto.compattazioni == 0
    storico_finto.dovuta = True
    await c._compatta_storico()
    assert storico_finto.compattazioni == 1


async def test_storicizza_solo_gli_host_raggiungibili(storico_finto):
    """Un host spento non ha un punto nuovo da salvare: scriverne uno
    inventerebbe una misura che nessuno ha preso."""
    salvati = {}
    storico_finto.add_host_points = salvati.update

    c = DataCollector()
    await c._storicizza_risorse({"hosts": [
        {"host": "192.0.2.10", "reachable": True,
         "series": [{"t": 1, "cpu": 1.0}, {"t": 2, "cpu": 9.0}]},
        {"host": "192.0.2.11", "reachable": False,
         "series": [{"t": 1, "cpu": 5.0}]},
        {"host": "192.0.2.12", "reachable": True, "series": []},
    ]})
    assert list(salvati) == ["192.0.2.10"]
    # L'ultimo punto della serie, non il primo.
    assert salvati["192.0.2.10"]["cpu"] == 9.0


async def test_nessun_host_raggiungibile_non_scrive_nulla(storico_finto):
    chiamate = []
    storico_finto.add_host_points = lambda p: chiamate.append(p)
    await DataCollector()._storicizza_risorse({"hosts": []})
    assert chiamate == []


async def test_i_device_pubblicati_finiscono_nello_storico(storico_finto):
    visti = []
    storico_finto.mark_devices = lambda devices: visti.append(devices)

    class FintoDevice:
        def __init__(self, key, online):
            self.key, self.online = key, online

        def to_dict(self):
            return {"key": self.key, "online": self.online, "name": self.key}

    c = DataCollector()
    c._store_devices([FintoDevice("192.0.2.5", True), FintoDevice("192.0.2.6", False)])
    assert len(visti) == 1
    assert [d["key"] for d in visti[0]] == ["192.0.2.5", "192.0.2.6"]


async def test_uno_storico_rotto_non_impedisce_di_pubblicare_i_device(storico_finto, caplog):
    def esplode(_devices):
        raise OSError("disco pieno")
    storico_finto.mark_devices = esplode

    class FintoDevice:
        key, online = "192.0.2.5", True
        def to_dict(self): return {"key": self.key, "online": self.online}

    c = DataCollector()
    c._store_devices([FintoDevice()])              # non deve sollevare
    assert len(c._snapshot["devices"]) == 1
    assert "storico presenza device" in caplog.text


# ── Stato delle sorgenti ───────────────────────────────────────────
# Prima un fallimento finiva solo in log.error e la sezione restava quella del
# giro precedente: la dashboard mostrava numeri vecchi come se fossero freschi,
# e dalla UI non c'era modo di accorgersene.

def test_una_sorgente_rotta_finisce_nello_snapshot_con_il_motivo():
    c = DataCollector()
    c._sorgente_ok("docker")
    ultimo_buono = c._snapshot["sources"]["docker"]["ts"]

    c._sorgente_ko("docker", RuntimeError("docker.sock non accessibile"))
    s = c._snapshot["sources"]["docker"]
    assert s["ok"] is False
    assert s["error"] == "docker.sock non accessibile"
    # `ts` resta l'ora dell'ultimo dato valido: e' quello che la UI deve dire.
    assert s["ts"] == ultimo_buono
    assert s["fails"] == 1
    assert s["since"] > 0


def test_i_fallimenti_ripetuti_si_contano_e_since_non_si_sposta():
    c = DataCollector()
    c._sorgente_ko("system", RuntimeError("giu'"))
    primo = c._snapshot["sources"]["system"]["since"]
    c._sorgente_ko("system", RuntimeError("ancora giu'"))
    s = c._snapshot["sources"]["system"]
    assert s["fails"] == 2
    # `since` e' l'inizio del guasto, non l'ultimo tentativo: altrimenti
    # "fermo da" ripartirebbe da zero ad ogni ciclo e non direbbe niente.
    assert s["since"] == primo


def test_una_sorgente_che_torna_su_si_ripulisce():
    c = DataCollector()
    c._sorgente_ko("wireguard", RuntimeError("giu'"))
    c._sorgente_ok("wireguard")
    s = c._snapshot["sources"]["wireguard"]
    assert s["ok"] is True
    assert "error" not in s and "fails" not in s


def test_un_eccezione_senza_messaggio_non_produce_un_motivo_vuoto():
    """asyncio.TimeoutError arriva senza testo: interpolarla da sola dava una
    riga che si interrompeva ai due punti."""
    c = DataCollector()
    c._sorgente_ko("resources", TimeoutError())
    assert c._snapshot["sources"]["resources"]["error"] == "TimeoutError"


# ── Alert nello snapshot ───────────────────────────────────────────

@pytest.fixture
def senza_avvisi_sicurezza(monkeypatch):
    """Gli avvisi di sicurezza dipendono dalla config della suite: qui si
    isolano, cosi' i test degli alert parlano solo della regola sotto esame."""
    import services.collector as mod
    monkeypatch.setattr(mod, "security_warnings", lambda: [])


def test_lo_snapshot_pubblica_gli_alert_e_il_riassunto(senza_avvisi_sicurezza):
    # Senza questo, gli alert esisterebbero solo mentre qualcuno guarda la
    # pagina che li possiede: niente contatore in sidebar, niente indice.
    c = DataCollector()
    c._snapshot = {"sources": {"docker": {"ok": False, "error": "timeout",
                                          "since": 100, "fails": 5}}}
    c._aggiorna_alert()
    assert [a["rule"] for a in c._snapshot["alerts"]] == ["collector.sorgente_giu"]
    assert c._snapshot["alerts"][0]["scope"] == "services"
    assert c._snapshot["alerts_summary"]["by_scope"]["services"]["critical"] == 1


def test_gli_avvisi_di_sicurezza_si_rivalutano_ad_ogni_ciclo(monkeypatch):
    # Prima erano letti una volta sola all'avvio: chiudere la falla dalla UI non
    # spegneva il banner fino al riavvio.
    import services.collector as mod
    motivi = ["auth.method: none — le API rispondono a chiunque"]
    monkeypatch.setattr(mod, "security_warnings", lambda: list(motivi))
    c = DataCollector()
    c._snapshot = {}
    c._aggiorna_alert()
    assert c._snapshot["security"] == motivi
    assert [a["scope"] for a in c._snapshot["alerts"]] == ["settings"]

    motivi.clear()
    c._aggiorna_alert()
    assert c._snapshot["alerts"] == []


def test_un_guasto_nel_calcolo_degli_alert_non_ferma_la_raccolta(monkeypatch, caplog):
    import services.collector as mod

    def esplode():
        raise RuntimeError("registro rotto")

    monkeypatch.setattr(mod, "get_alerts", esplode)
    monkeypatch.setattr(mod, "security_warnings", lambda: [])
    c = DataCollector()
    c._snapshot = {"devices": []}
    c._aggiorna_alert()                       # non solleva: il ciclo prosegue
    assert "registro rotto" in caplog.text    # ma non in silenzio


async def test_la_rete_dellhost_entra_nello_snapshot(monkeypatch):
    import services.collector as mod

    async def finta():
        return {"source": "ip", "interfaces": [], "routes": []}

    monkeypatch.setattr(mod, "get_host_network", finta)
    c = DataCollector()
    await c._collect_host_network()
    assert c._snapshot["host_network"]["source"] == "ip"
    assert c._snapshot["sources"]["host_network"]["ok"] is True


async def test_se_la_rete_dellhost_non_si_legge_lo_dice(monkeypatch):
    import services.collector as mod

    async def rotta():
        raise OSError("ip: comando non trovato")

    monkeypatch.setattr(mod, "get_host_network", rotta)
    c = DataCollector()
    await c._collect_host_network()
    assert c._snapshot["sources"]["host_network"]["ok"] is False
    assert "comando non trovato" in c._snapshot["sources"]["host_network"]["error"]
