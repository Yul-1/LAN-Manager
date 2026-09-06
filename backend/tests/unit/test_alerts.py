"""
Alert contestuali (services/alerts.py, config.AlertsConfig).

Gli alert nascono da un difetto preciso: le vecchie regole erano scritte addosso
a un setup e suonavano per sempre, senza motivo e senza modo di zittirle. Qui si
prova cio' che rende utile la sostituzione: identita' stabile nel tempo, `since`
che non riparte, silenziamento che ha effetto **senza riavviare**, e le regole
deliberatamente assenti che restano assenti.
"""
from __future__ import annotations

import pytest

from config import AlertsConfig, AlertSilence, Settings


# ── La sezione alerts deve esistere davvero nello schema ───────────

def test_la_sezione_alerts_non_viene_scartata_da_settings():
    # Settings ha extra="ignore": senza il campo `alerts`, scrivere la sezione in
    # config.yaml la farebbe sparire in silenzio e il salvataggio riuscirebbe lo
    # stesso. Il risultato sarebbe un silenziamento che non silenzia, senza un
    # solo messaggio d'errore.
    s = Settings(alerts={"silenced": [
        {"rule": "host.subnet_duplicata", "subject": "192.0.2.0/24", "reason": "due schede volute"}
    ]})
    assert s.alerts.silenced[0].rule == "host.subnet_duplicata"
    assert s.alerts.silenced[0].reason == "due schede volute"


def test_i_default_degli_alert_sono_conservativi():
    cfg = AlertsConfig()
    assert cfg.enabled is True
    assert cfg.min_seconds == 120           # niente allarmi al primo ciclo
    assert cfg.source_critical_after == 3
    assert cfg.rules == {} and cfg.silenced == []


def test_una_sezione_alerts_vuota_non_rompe_lo_schema():
    # Chiavi YAML lasciate vuote arrivano come None: devono diventare vuoti.
    cfg = AlertsConfig(rules=None, silenced=None)
    assert cfg.rules == {} and cfg.silenced == []


def test_il_silenziamento_senza_soggetto_vale_per_tutta_la_regola():
    s = AlertSilence(rule="docker.restart_loop", reason="ci penso io")
    assert s.subject == ""


# ── Registro: identita' e memoria del tempo ────────────────────────

from services import alerts as A


class Orologio:
    """Orologio finto: `time` e' importato come modulo dentro alerts.py, quindi
    si sostituisce l'intero modulo con questo (strftime/localtime servono ai
    testi delle regole)."""

    def __init__(self, t=1_000_000):
        self.t = t

    def time(self):
        return self.t

    def avanza(self, secondi):
        self.t += secondi

    strftime = staticmethod(__import__("time").strftime)
    localtime = staticmethod(__import__("time").localtime)


@pytest.fixture
def orologio(monkeypatch):
    o = Orologio()
    monkeypatch.setattr(A, "time", o)
    return o


@pytest.fixture
def cfg(monkeypatch):
    """Config degli alert iniettata: i test non dipendono dal file su disco."""
    corrente = AlertsConfig()

    def usa(**kw):
        nonlocal corrente
        corrente = AlertsConfig(**kw)
        return corrente

    monkeypatch.setattr(A, "live_alerts_config", lambda: corrente)
    usa.set = usa
    return usa


@pytest.fixture
def registro():
    return A.AlertRegistry()


def _snap_restarting(nome="mosquitto", host="nas"):
    return {"docker": {"containers": [
        {"name": nome, "host": host, "status": "restarting", "running": False},
    ]}}


def test_lo_stesso_guasto_mantiene_lo_stesso_id_fra_cicli(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}})
    primo = registro.build(_snap_restarting())
    orologio.avanza(10)
    secondo = registro.build(_snap_restarting())
    assert [a["id"] for a in primo] == [a["id"] for a in secondo]
    assert primo[0]["id"] == "docker.restart_loop:nas/mosquitto"


def test_since_non_riparte_a_ogni_ciclo(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}})
    since = registro.build(_snap_restarting())[0]["since"]
    for _ in range(5):
        orologio.avanza(10)
        assert registro.build(_snap_restarting())[0]["since"] == since


def test_un_alert_che_sparisce_e_torna_subito_conserva_since(orologio, cfg, registro):
    # Un container che flappa esce e rientra fra due cicli: ripartire da zero
    # direbbe "da 10s" per un guasto che dura da un'ora.
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}})
    since = registro.build(_snap_restarting())[0]["since"]
    orologio.avanza(30)
    assert registro.build({}) == []
    orologio.avanza(30)
    assert registro.build(_snap_restarting())[0]["since"] == since


def test_oltre_la_finestra_di_oblio_e_un_guasto_nuovo(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}})
    vecchio = registro.build(_snap_restarting())[0]["since"]
    orologio.avanza(A.FINESTRA_OBLIO + 60)
    assert registro.build({}) == []
    orologio.avanza(10)
    assert registro.build(_snap_restarting())[0]["since"] > vecchio


def test_un_testo_che_cambia_non_crea_un_alert_nuovo(orologio, cfg, registro):
    # `detail` contiene numeri che cambiano ad ogni ciclo: se entrassero
    # nell'identita', ogni giro sarebbe un guasto nuovo.
    cfg()
    snap = {"sources": {"docker": {"ok": False, "error": "timeout", "since": 900, "fails": 1}}}
    a1 = registro.build(snap)[0]
    snap["sources"]["docker"]["fails"] = 2
    orologio.avanza(10)
    a2 = registro.build(snap)[0]
    assert a1["id"] == a2["id"] and a1["since"] == a2["since"]
    assert a1["detail"] != a2["detail"]


def test_due_regole_con_lo_stesso_id_producono_un_alert_solo(orologio, cfg, registro, monkeypatch):
    cfg()
    doppia = A.Regola(key="docker.restart_loop", fn=lambda s: [
        A.Alert(subject="x", title="a"), A.Alert(subject="x", title="b")],
        min_seconds=0)
    monkeypatch.setattr(A, "CATALOGO", {"docker.restart_loop": doppia})
    out = registro.build({})
    assert len(out) == 1 and out[0]["title"] == "a"


# ── Le regole deliberatamente assenti ──────────────────────────────

def test_un_container_semplicemente_fermo_non_suona(orologio, cfg, registro):
    cfg()
    snap = {"docker": {"containers": [
        {"name": "vecchio", "host": "nas", "status": "exited", "running": False}]}}
    assert registro.build(snap) == []


def test_un_dispositivo_offline_non_suona(orologio, cfg, registro):
    cfg()
    snap = {"devices": [{"name": "telefono", "online": False, "ips": ["192.0.2.9"]}],
            "devices_summary": {"total": 1, "online": 0, "offline": 1}}
    assert registro.build(snap) == []


def test_un_host_saltato_perche_spento_non_suona(orologio, cfg, registro):
    cfg()
    snap = {"resources": {"hosts": [
        {"host": "192.0.2.9", "reachable": False, "skipped": True,
         "error": "host non raggiungibile (nessuna risposta al ping)"}]}}
    assert registro.build(snap) == []


def test_un_host_spento_secondo_la_discovery_non_suona(orologio, cfg, registro):
    # Con skip_offline spento la connessione parte comunque e fallisce: senza
    # questo controllo un portatile spento sarebbe un guasto permanente.
    cfg(rules={"resources.host_ssh_giu": {"min_seconds": 0}})
    snap = {"devices": [{"name": "portatile", "online": False, "ips": ["192.0.2.9"]}],
            "resources": {"hosts": [
                {"host": "192.0.2.9", "reachable": False, "skipped": False,
                 "error": "Connection timed out"}]}}
    assert registro.build(snap) == []


def test_gli_avvisi_di_sicurezza_hanno_un_soggetto_stabile(orologio, cfg, registro):
    # Con l'indice nella lista, risolvere il primo motivo faceva scivolare il
    # secondo al posto suo: `since` ripartiva e un silenziamento avrebbe
    # agganciato l'avviso sbagliato.
    cfg()
    due = ["auth.method: none — le API rispondono a chiunque",
           "nessuna password admin impostata: il primo che apre la dashboard puo' sceglierla"]
    soggetti = [a["subject"] for a in registro.build({"security": due})]
    assert soggetti == ["auth.method", "nessuna password admin impostata"]

    orologio.avanza(10)
    rimasto = registro.build({"security": due[1:]})
    assert rimasto[0]["subject"] == "nessuna password admin impostata"
    assert rimasto[0]["since"] == 1_000_000, "l'avviso rimasto e' lo stesso di prima"


def test_un_host_acceso_che_non_risponde_in_ssh_suona(orologio, cfg, registro):
    cfg(rules={"resources.host_ssh_giu": {"min_seconds": 0}})
    snap = {"resources": {"hosts": [
        {"host": "192.0.2.9", "name": "nas", "reachable": False, "skipped": False,
         "error": "Permission denied (publickey)"}]}}
    out = registro.build(snap)
    assert len(out) == 1 and out[0]["scope"] == "resources"
    assert "publickey" in out[0]["detail"]


# ── Attesa prima di suonare ────────────────────────────────────────

def test_il_restart_loop_non_suona_prima_dell_attesa(orologio, cfg, registro):
    cfg()                                    # min_seconds della regola: 120s
    assert registro.build(_snap_restarting()) == []
    orologio.avanza(60)
    assert registro.build(_snap_restarting()) == []
    orologio.avanza(70)
    out = registro.build(_snap_restarting())
    assert len(out) == 1 and out[0]["level"] == "warn"


# ── Sorgenti giu' ──────────────────────────────────────────────────

def test_since_arriva_dalla_sorgente_non_dal_registro(orologio, cfg, registro):
    # Il collector sa da quando quella sorgente e' giu' meglio di chi la legge.
    cfg()
    snap = {"sources": {"system": {"ok": False, "error": "timeout", "since": 500, "fails": 1}}}
    assert registro.build(snap)[0]["since"] == 500


def test_una_sorgente_diventa_critica_dopo_i_fallimenti_configurati(orologio, cfg, registro):
    cfg(source_critical_after=3)
    snap = {"sources": {"docker": {"ok": False, "error": "timeout", "since": 1, "fails": 2}}}
    assert registro.build(snap)[0]["level"] == "warn"
    snap["sources"]["docker"]["fails"] = 3
    assert registro.build(snap)[0]["level"] == "critical"


def test_ogni_sorgente_finisce_nella_pagina_che_la_possiede(orologio, cfg, registro):
    cfg()
    snap = {"sources": {"devices": {"ok": False, "error": "x", "since": 1, "fails": 1},
                        "resources": {"ok": False, "error": "y", "since": 1, "fails": 1}}}
    per_scope = {a["subject"]: a["scope"] for a in registro.build(snap)}
    assert per_scope == {"devices": "devices", "resources": "resources"}


# ── Host Docker ────────────────────────────────────────────────────

def test_un_host_docker_giu_e_critico(orologio, cfg, registro):
    # Senza questa regola l'host sparisce in silenzio: i suoi container non
    # compaiono e i contatori calano come se li' non ci fosse mai stato nulla.
    cfg(rules={"docker.host_giu": {"min_seconds": 0}})
    snap = {"docker": {"hosts": [
        {"name": "homeserver", "reachable": True, "error": "", "seen_ok": True},
        {"name": "nas", "reachable": False, "error": "Connection refused", "seen_ok": True}]}}
    out = registro.build(snap)
    assert len(out) == 1
    assert out[0]["level"] == "critical" and out[0]["scope"] == "services"
    assert out[0]["subject"] == "nas" and "refused" in out[0]["detail"]


def test_un_host_che_non_ha_mai_risposto_non_e_un_guasto(orologio, cfg, registro):
    # Gli host Docker arrivano anche dall'autodiscovery SSH, e meta' della LAN
    # non fa girare Docker: su homeserver erano sei allarmi rossi permanenti al
    # primo avvio, cioe' il difetto da cui nasce la fase, ricreato.
    cfg(rules={"docker.host_giu": {"min_seconds": 0}})
    snap = {"docker": {"hosts": [
        {"name": "192.0.2.21", "reachable": False, "seen_ok": False,
         "error": "timeout: porta 22 chiusa o filtrata"}]}}
    assert registro.build(snap) == []


# ── Rete dell'host ─────────────────────────────────────────────────

def _iface(name, ip, prefix=24, state="up", virtual=False):
    return {"name": name, "state": state, "virtual": virtual,
            "addresses": [{"family": "inet", "ip": ip, "prefix": prefix}]}


def _rete(interfaces=(), routes=()):
    return {"host_network": {"interfaces": list(interfaces), "routes": list(routes)}}


def test_due_schede_sulla_stessa_subnet_si_segnalano_e_si_possono_zittire(orologio, cfg, registro):
    # E' la configurazione vera di homeserver (cablata piu' wireless di riserva):
    # prima era un avviso acceso in permanenza e senza interruttore. Ora il
    # soggetto e' il CIDR, quindi si silenzia quella subnet e basta.
    cfg()
    snap = _rete([_iface("enp1s0f0", "192.0.2.30"), _iface("wlp2s0", "192.0.2.31")])
    out = registro.build(snap)
    assert len(out) == 1 and out[0]["level"] == "warn" and out[0]["scope"] == "host"
    assert out[0]["subject"] == "192.0.2.0/24"
    assert "enp1s0f0" in out[0]["detail"] and "wlp2s0" in out[0]["detail"]

    cfg(silenced=[{"rule": "host.subnet_duplicata", "subject": "192.0.2.0/24",
                   "reason": "due schede volute"}])
    assert registro.build(snap) == []


def test_le_interfacce_virtuali_non_contano_come_doppione(orologio, cfg, registro):
    cfg()
    assert registro.build(_rete([_iface("enp1s0f0", "192.0.2.30"),
                                 _iface("docker0", "192.0.2.31", virtual=True)])) == []


def test_due_default_route_con_la_stessa_metrica_si_segnalano(orologio, cfg, registro):
    cfg()
    rotte = [{"default": True, "dev": "enp1s0f0", "gateway": "192.0.2.1", "metric": 100},
             {"default": True, "dev": "wlp2s0", "gateway": "192.0.2.1", "metric": 100}]
    out = registro.build(_rete(routes=rotte))
    assert len(out) == 1 and "stessa metrica" in out[0]["title"]
    # Nessun soggetto: l'elenco delle vie cambia, e metterlo nell'identita'
    # farebbe ripartire `since` ad ogni variazione.
    assert out[0]["subject"] == ""


def test_due_default_route_con_metriche_diverse_sono_una_riserva_voluta(orologio, cfg, registro):
    # E' la configurazione vera di homeserver: cablata 100, wifi 600. Il kernel
    # sceglie sempre la piu' bassa, quindi non c'e' niente di ambiguo da dire.
    cfg()
    rotte = [{"default": True, "dev": "enp1s0f0", "gateway": "192.0.2.1", "metric": 100},
             {"default": True, "dev": "wlp2s0", "gateway": "192.0.2.1", "metric": 600}]
    assert registro.build(_rete(routes=rotte)) == []


def test_una_sola_default_route_non_e_un_alert(orologio, cfg, registro):
    cfg()
    rotte = [{"default": True, "dev": "eth0", "gateway": "192.0.2.1", "metric": 100}]
    assert registro.build(_rete(routes=rotte)) == []


def test_un_link_giu_con_ip_e_solo_informativo(orologio, cfg, registro):
    cfg(rules={"host.link_giu_con_ip": {"min_seconds": 0}})
    out = registro.build(_rete([_iface("eth1", "192.0.2.40", state="down")]))
    assert len(out) == 1 and out[0]["level"] == "info"


def test_una_rete_sana_non_produce_alert(orologio, cfg, registro):
    cfg()
    rotte = [{"default": True, "dev": "eth0", "gateway": "192.0.2.1", "metric": 100}]
    assert registro.build(_rete([_iface("eth0", "192.0.2.30")], rotte)) == []


# ── Silenziamenti ──────────────────────────────────────────────────

def test_silenziare_un_soggetto_lascia_suonare_gli_altri(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}},
        silenced=[{"rule": "docker.restart_loop", "subject": "nas/uno",
                   "reason": "lo riavvio a mano"}])
    snap = {"docker": {"containers": [
        {"name": "uno", "host": "nas", "status": "restarting"},
        {"name": "due", "host": "nas", "status": "restarting"}]}}
    out = registro.build(snap)
    assert [a["subject"] for a in out] == ["nas/due"]


def test_silenziare_senza_soggetto_zittisce_tutta_la_regola(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"min_seconds": 0}},
        silenced=[{"rule": "docker.restart_loop", "reason": "ci penso io"}])
    snap = {"docker": {"containers": [
        {"name": "uno", "host": "nas", "status": "restarting"},
        {"name": "due", "host": "nas", "status": "restarting"}]}}
    assert registro.build(snap) == []


def test_una_regola_spenta_da_config_non_gira(orologio, cfg, registro):
    cfg(rules={"docker.restart_loop": {"enabled": False}})
    assert registro.build(_snap_restarting()) == []


def test_il_livello_si_puo_abbassare_da_config(orologio, cfg, registro):
    cfg(rules={"services.systemd_giu": {"level": "info", "min_seconds": 0}})
    snap = {"services": {"systemd": [
        {"name": "docker.service", "critical": True, "ok": False, "active_state": "failed"}]}}
    assert registro.build(snap)[0]["level"] == "info"


# ── Servizi Windows ────────────────────────────────────────────────

def _snap_windows(**campi):
    voce = {"name": "Spooler", "label": "Coda di stampa", "host": "192.0.2.12",
            "critical": True, "ok": False, "available": True, "state": "stopped"}
    voce.update(campi)
    return {"services": {"windows_services": [voce]}}


def test_un_servizio_windows_critico_e_fermo_suona(orologio, cfg, registro):
    cfg(rules={"services.windows_giu": {"min_seconds": 0}})
    alert = registro.build(_snap_windows())[0]
    assert "non in esecuzione" in alert["title"]
    assert "192.0.2.12" in alert["subject"]


def test_un_servizio_windows_non_installato_lo_dice_con_parole_sue(orologio, cfg, registro):
    # Non e' un guasto della macchina: e' il catalogo che nomina un servizio che
    # su quell'host non esiste, e l'azione da suggerire e' diversa.
    cfg(rules={"services.windows_giu": {"min_seconds": 0}})
    alert = registro.build(_snap_windows(state="not-found"))[0]
    assert "non installato" in alert["title"]
    assert "sc query" in alert["action"]


def test_un_host_che_non_risponde_non_diventa_un_servizio_fermo(orologio, cfg, registro):
    # "Non so" e "so che e' fermo" sono due cose diverse, e mescolarle manderebbe
    # a controllare il servizio invece della macchina.
    cfg(rules={"services.windows_giu": {"min_seconds": 0}})
    alert = registro.build(_snap_windows(available=False, state="unknown",
                                         error="connessione rifiutata"))[0]
    assert "non leggibile" in alert["title"]
    assert "Host SSH" in alert["action"]


def test_un_servizio_windows_non_critico_non_suona(orologio, cfg, registro):
    cfg(rules={"services.windows_giu": {"min_seconds": 0}})
    assert registro.build(_snap_windows(critical=False)) == []


def test_un_servizio_windows_in_esecuzione_non_suona(orologio, cfg, registro):
    cfg(rules={"services.windows_giu": {"min_seconds": 0}})
    assert registro.build(_snap_windows(ok=True, state="running")) == []


def test_alerts_spenti_del_tutto(orologio, cfg, registro):
    cfg(enabled=False)
    assert registro.build(_snap_restarting()) == []


# ── Config riletta a caldo ─────────────────────────────────────────

def _store_su(tmp_path, monkeypatch):
    """ConfigStore vero, ma su un file di test: la rilettura a caldo si prova
    con la stessa meccanica che gira in produzione."""
    from services import config_store

    store = config_store.ConfigStore()
    monkeypatch.setattr(store, "path", tmp_path / "config.yaml")
    monkeypatch.setattr(config_store, "get_config_store", lambda: store)
    A._cache.update(mtime=0.0, gen=-1, cfg=None)
    return store


def _silenzia(rule, subject, reason):
    return {"alerts": {"silenced": [
        {"rule": rule, "subject": subject, "reason": reason}]}}


def test_silenziare_ha_effetto_senza_riavviare(tmp_path, monkeypatch):
    store = _store_su(tmp_path, monkeypatch)
    store._write_with_backup({"app_name": "prova"})
    assert A.live_alerts_config().silenced == []

    # Il file cambia mentre il processo gira: nessun riavvio, nessun import nuovo.
    store._write_with_backup(_silenzia("host.subnet_duplicata", "192.0.2.0/24",
                                       "due schede volute"))
    silenziati = A.live_alerts_config().silenced
    assert len(silenziati) == 1 and silenziati[0].reason == "due schede volute"


def test_due_salvataggi_ravvicinati_sono_entrambi_visibili(tmp_path, monkeypatch):
    # Su un filesystem con timestamp a grana grossa due scritture consecutive
    # hanno lo stesso mtime: con la sola cache sul mtime la seconda resterebbe
    # invisibile fino al riavvio, che e' esattamente cio' che questa fase promette
    # di non richiedere.
    store = _store_su(tmp_path, monkeypatch)
    store._write_with_backup(_silenzia("docker.restart_loop", "nas/uno", "primo"))
    assert A.live_alerts_config().silenced[0].subject == "nas/uno"
    store._write_with_backup(_silenzia("docker.restart_loop", "nas/due", "secondo"))
    assert A.live_alerts_config().silenced[0].subject == "nas/due"


def test_una_modifica_a_mano_del_file_viene_vista(tmp_path, monkeypatch):
    import os

    import yaml

    store = _store_su(tmp_path, monkeypatch)
    store._write_with_backup({"app_name": "prova"})
    assert A.live_alerts_config().silenced == []
    # Modifica fatta nell'editor, senza passare dalla UI: il contatore dei
    # salvataggi non si muove, resta il mtime a dirlo.
    store.path.write_text(yaml.safe_dump(_silenzia("docker.restart_loop", "", "a mano")))
    st = store.path.stat()
    os.utime(store.path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert A.live_alerts_config().silenced[0].reason == "a mano"


def test_una_config_illeggibile_non_fa_sparire_gli_alert(tmp_path, monkeypatch):
    store = _store_su(tmp_path, monkeypatch)
    store.path.write_text("alerts: [questo non e' un oggetto\n")
    # Fallback sulla config di avvio: un config.yaml rotto non deve spegnere
    # proprio gli avvisi che servono a capire che qualcosa non va.
    assert A.live_alerts_config().enabled is True


# ── Robustezza e catalogo ──────────────────────────────────────────

def test_una_regola_che_solleva_non_svuota_le_altre(orologio, cfg, registro, monkeypatch, caplog):
    cfg()

    def rotta(snap):
        raise RuntimeError("regola scritta male")

    catalogo = dict(A.CATALOGO)
    catalogo["prova.rotta"] = A.Regola(key="prova.rotta", fn=rotta, min_seconds=0)
    monkeypatch.setattr(A, "CATALOGO", catalogo)
    snap = {"sources": {"docker": {"ok": False, "error": "timeout", "since": 1, "fails": 1}}}
    out = registro.build(snap)
    assert [a["rule"] for a in out] == ["collector.sorgente_giu"]
    assert "regola scritta male" in caplog.text        # mai un fallimento silenzioso


def test_il_riassunto_conta_per_livello_e_per_pagina():
    dati = [{"level": "critical", "scope": "services"},
            {"level": "warn", "scope": "services"},
            {"level": "info", "scope": "host"}]
    r = A.summary(dati)
    assert r["critical"] == 1 and r["warn"] == 1 and r["info"] == 1 and r["total"] == 3
    assert r["by_scope"]["services"]["total"] == 2
    assert r["by_scope"]["host"]["info"] == 1


def test_ogni_regola_punta_a_una_pagina_che_esiste_davvero():
    # Uno scope inventato manderebbe l'indice della dashboard su una pagina
    # inesistente, e il contatore in sidebar non si accenderebbe mai.
    from pathlib import Path
    import re

    sorgente = (Path(__file__).resolve().parents[3] / "frontend" / "app.js").read_text()
    blocco = re.search(r"const PAGES = \{(.*?)\n\};", sorgente, re.S).group(1)
    pagine = set(re.findall(r"^\s{2}(\w+):\s*\{", blocco, re.M))
    assert pagine, "PAGES non trovato in app.js"
    for r in A.catalogo():
        assert r["scope"] in pagine, f"{r['rule']} -> scope inesistente {r['scope']}"


def test_il_catalogo_documenta_ogni_regola():
    for r in A.catalogo():
        assert r["descr"], f"{r['rule']} senza descrizione: la UI mostrerebbe una riga muta"


# ── Il tunnel del relay deve stare sempre su ───────────────────────
# Richiesta del proprietario (2026-09-03): gli altri peer si collegano quando
# servono, il relay no — se non fa handshake e' un guasto.


def _snap_wg(endpoint="203.0.113.10:51820", hs=None, ora=1_000_000):
    return {"wireguard": {"interfaces": [{
        "name": "wgclient", "listen_port": 56224,
        "peers": [{"name": "relay", "public_key": "K" * 44, "endpoint": endpoint,
                   "allowed_ips": ["10.100.0.0/24"],
                   "last_handshake": ora - 30 if hs is None else hs,
                   "status": "active" if hs is None else "idle"}],
    }]}}


@pytest.fixture
def relay(monkeypatch):
    """Relay dichiarato in configurazione, come su homeserver."""
    monkeypatch.setattr(A.settings.wireguard, "relay_host", "203.0.113.10")
    return "203.0.113.10"


def test_il_tunnel_del_relay_su_non_suona(orologio, cfg, registro, relay):
    cfg()
    assert registro.build(_snap_wg()) == []


def test_il_tunnel_del_relay_giu_suona_come_critico(orologio, cfg, registro, relay):
    cfg()
    # Handshake fermo da dieci minuti: oltre l'attesa di serie della regola.
    out = registro.build(_snap_wg(hs=1_000_000 - 600))
    assert len(out) == 1
    a = out[0]
    assert a["rule"] == "wireguard.relay_giu" and a["level"] == "critical"
    assert a["scope"] == "wireguard", "l'avviso deve comparire nella pagina WireGuard"
    assert a["subject"] == "wgclient/203.0.113.10"
    assert "10 minuti fa" in a["detail"]


def test_l_attesa_conta_dal_momento_in_cui_e_caduto(orologio, cfg, registro, relay):
    # `since` e' l'ultimo handshake, non il primo giro in cui ce ne accorgiamo:
    # altrimenti un backend appena riavviato ricomincerebbe ad aspettare da capo
    # davanti a un tunnel gia' giu' da un'ora.
    cfg()
    caduto = 1_000_000 - 240                 # 4 minuti: sotto i 300s di attesa
    assert registro.build(_snap_wg(hs=caduto)) == []
    orologio.avanza(120)                     # ora sono 6 minuti dalla caduta
    out = registro.build(_snap_wg(hs=caduto))
    assert len(out) == 1 and out[0]["since"] == caduto


def test_uno_status_vecchio_non_zittisce_l_avviso(orologio, cfg, registro, relay):
    # Se il router smette di rispondere lo snapshot resta quello di prima: un
    # `status: active` stantio direbbe che il tunnel e' su mentre non lo e'.
    cfg()
    snap = _snap_wg(hs=1_000_000 - 900)
    snap["wireguard"]["interfaces"][0]["peers"][0]["status"] = "active"
    assert len(registro.build(snap)) == 1


def test_il_peer_mai_collegato_del_relay_suona_dopo_l_attesa(orologio, cfg, registro, relay):
    # Senza un handshake mai avvenuto non c'e' un istante di caduta: l'attesa
    # parte da quando lo si vede, non prima.
    cfg()
    assert registro.build(_snap_wg(hs=0)) == []
    orologio.avanza(301)
    out = registro.build(_snap_wg(hs=0))
    assert len(out) == 1 and "Nessun handshake" in out[0]["detail"]


def test_gli_altri_peer_non_fanno_suonare_niente(orologio, cfg, registro, relay):
    # Un telefono fermo da ore e' la normalita': la regola guarda solo il relay,
    # che nella stessa interfaccia sta su.
    cfg()
    snap = _snap_wg()
    snap["wireguard"]["interfaces"][0]["peers"].append(
        {"name": "telefono", "public_key": "T" * 44, "endpoint": "203.0.113.7:38112",
         "allowed_ips": ["10.100.0.9/32"], "last_handshake": 1_000_000 - 26_000,
         "status": "idle"})
    assert registro.build(snap) == []


def test_senza_relay_dichiarato_la_regola_non_gira(orologio, cfg, registro, monkeypatch):
    cfg()
    monkeypatch.setattr(A.settings.wireguard, "relay_host", "")
    assert registro.build(_snap_wg(hs=1_000_000 - 900)) == []


def test_un_relay_che_non_combacia_con_nessun_peer_lo_dice(orologio, cfg, registro, monkeypatch):
    # Senza questo, un relay_host sbagliato spegnerebbe la sorveglianza in
    # silenzio: nessun peer da guardare, nessun avviso, e nessuno che lo dice.
    cfg()
    monkeypatch.setattr(A.settings.wireguard, "relay_host", "198.51.100.7")
    out = registro.build(_snap_wg())
    assert len(out) == 1
    assert out[0]["rule"] == "wireguard.relay_non_trovato" and out[0]["level"] == "info"


def test_senza_peer_non_si_accusa_la_configurazione(orologio, cfg, registro, relay):
    # Router muto o WireGuard spento: lo dicono gia' la pagina e l'avviso di
    # sorgente, non serve dare la colpa a relay_host.
    cfg()
    assert registro.build({"wireguard": {"interfaces": []}}) == []


def test_l_endpoint_ipv6_viene_confrontato_senza_la_porta(orologio, cfg, registro, monkeypatch):
    cfg()
    monkeypatch.setattr(A.settings.wireguard, "relay_host", "2001:db8::1")
    out = registro.build(_snap_wg(endpoint="[2001:db8::1]:51820", hs=1_000_000 - 900))
    assert len(out) == 1 and out[0]["rule"] == "wireguard.relay_giu"


def test_la_soglia_dell_handshake_e_una_sola(orologio, cfg, registro, relay):
    # La regola e `WGPeer.is_active` devono usare lo stesso numero: con due
    # soglie diverse la pagina direbbe "attivo" mentre l'alert suona.
    from services.wireguard import HANDSHAKE_ATTIVO
    cfg(rules={"wireguard.relay_giu": {"min_seconds": 0}})
    assert registro.build(_snap_wg(hs=1_000_000 - HANDSHAKE_ATTIVO)) == []
    assert len(registro.build(_snap_wg(hs=1_000_000 - HANDSHAKE_ATTIVO - 1))) == 1
