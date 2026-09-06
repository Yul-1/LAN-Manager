"""
services/alerts.py — Alert contestuali
======================================
Un alert dice **cosa** non va, **perche'**, **da quando** e **cosa fare**, e
dichiara la pagina che possiede l'oggetto (`scope`): Docker in Monitoraggio,
interfacce in Host, peer in WireGuard. Non esiste una tab "Alert": l'avviso
compare dove sta la cosa di cui parla.

Le regole sono **funzioni pure sullo snapshot** del collector: nessuna I/O,
nessuna connessione, quindi girano tutte ad ogni ciclo veloce. Le degradazioni
delle sorgenti stanno gia' in `snapshot["sources"]` (collector.py): qui si
**leggono**, non si ricalcolano.

Regole deliberatamente ASSENTI (decisioni del proprietario, 2026-09-02 — non
riaggiungerle senza chiederglielo):
  - dispositivo offline: su una LAN domestica sarebbe la sorgente piu' rumorosa,
    e la pagina Dispositivi mostra gia' "giu' da 3g" accanto a ciascuno;
  - container semplicemente fermo: un container spento di proposito e' la norma
    (resta solo il restart loop, che invece e' un guasto);
  - peer WireGuard mai connesso: un peer idle e' lo stato normale di un telefono
    (la regola esiste ma nasce spenta, si accende da config).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import yaml

from config import AlertsConfig, settings
from services.wireguard import HANDSHAKE_ATTIVO

log = logging.getLogger("alerts")

# Quanto un alert puo' sparire e tornare restando "lo stesso guasto". Un
# container che flappa esce e rientra fra due cicli: azzerare `since` ad ogni
# rientro direbbe "da 10s" per un guasto che dura da un'ora.
FINESTRA_OBLIO = 600

_LIVELLI = ("critical", "warn", "info")


@dataclass
class Alert:
    """Un avviso singolo. `id` = regola + soggetto e **nient'altro**.

    Il testo cambia ad ogni ciclo (durata, errore SSH, numero di fallimenti): se
    entrasse nell'identita', ogni ciclo produrrebbe un alert nuovo, `since`
    ripartirebbe da zero e un silenziamento non aggancerebbe piu' nulla.
    """
    # Vuoto nelle regole: lo riempie il registro con la chiave del catalogo,
    # cosi' la chiave sta scritta una volta sola (nel decoratore).
    rule: str = ""
    subject: str = ""
    level: str = ""
    scope: str = ""
    title: str = ""
    detail: str = ""
    action: str = ""
    source: str = ""
    since: int = 0

    @property
    def id(self) -> str:
        return f"{self.rule}:{self.subject}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "rule": self.rule, "subject": self.subject,
            "level": self.level, "scope": self.scope, "title": self.title,
            "detail": self.detail, "action": self.action,
            "source": self.source, "since": self.since,
        }


@dataclass
class Regola:
    key: str
    fn: Callable[[dict], list[Alert]]
    level: str = "warn"
    scope: str = "dashboard"
    min_seconds: Optional[int] = None   # None = usa il default di AlertsConfig
    enabled: bool = True                # di serie; sovrascrivibile da config
    descr: str = ""                 # una riga per la UI dei silenziamenti


# Unico elenco dei nomi validi: lo usano la tendina dei silenziamenti e la
# validazione della config, cosi' un nome sbagliato si vede al salvataggio e non
# con un silenziamento che, senza dire nulla, non zittisce niente.
CATALOGO: dict[str, Regola] = {}


def regola(key: str, *, level: str = "warn", scope: str = "dashboard",
           min_seconds: Optional[int] = None, enabled: bool = True, descr: str = ""):
    def deco(fn: Callable[[dict], list[Alert]]):
        CATALOGO[key] = Regola(key=key, fn=fn, level=level, scope=scope,
                               min_seconds=min_seconds, enabled=enabled, descr=descr)
        return fn
    return deco


# ── Config riletta a caldo ─────────────────────────────────────────

_cache: dict[str, Any] = {"mtime": 0.0, "gen": -1, "cfg": None}


def live_alerts_config() -> AlertsConfig:
    """Sezione `alerts` **riletta dal file**, non quella caricata all'avvio.

    Silenziare un avviso al prezzo di un riavvio del servizio sarebbe un mezzo
    servizio. Stessa meccanica di `ssh_hosts.live_ssh_config`: cache invalidata
    sul mtime, e fallback sulla config di avvio se il file non e' leggibile —
    un config.yaml rotto non deve far sparire gli alert.
    """
    from services.config_store import generazione, get_config_store   # qui: evita il ciclo

    try:
        path = get_config_store().path
        mtime, gen = path.stat().st_mtime, generazione()
        # Il mtime da solo non basta: su filesystem con timestamp a grana grossa
        # due salvataggi ravvicinati hanno lo stesso mtime e il secondo
        # resterebbe invisibile. Il contatore dei salvataggi copre quel caso, il
        # mtime copre le modifiche fatte a mano nel file.
        if _cache["cfg"] is not None and mtime == _cache["mtime"] and gen == _cache["gen"]:
            return _cache["cfg"]
        raw = yaml.safe_load(path.read_text()) or {}
        cfg = AlertsConfig(**((raw.get("alerts")) or {}))
        _cache.update(mtime=mtime, gen=gen, cfg=cfg)
        return cfg
    except Exception as e:
        log.warning(f"sezione alerts non rileggibile dal file ({e}): uso la config di avvio")
        return settings.alerts


# ── Registro ───────────────────────────────────────────────────────

class AlertRegistry:
    """Valuta le regole e tiene la memoria di quando ciascun alert e' comparso.

    Istanza unica di processo (`get_alerts()`): il primo avvistamento e' l'unico
    dato che non si puo' ricavare dallo snapshot corrente.
    """

    def __init__(self):
        self._primo: dict[str, int] = {}      # id -> epoch del primo avvistamento
        self._visto: dict[str, int] = {}      # id -> epoch dell'ultimo avvistamento

    def build(self, snap: dict) -> list[dict]:
        cfg = live_alerts_config()
        now = int(time.time())
        if not cfg.enabled:
            self._primo.clear()
            self._visto.clear()
            return []

        grezzi: list[Alert] = []
        for key, rule in CATALOGO.items():
            over = cfg.rules.get(key)
            if not _abilitata(rule, over):
                continue
            try:
                prodotti = rule.fn(snap) or []
            except Exception as e:
                # Una regola scritta male non deve far sparire tutte le altre.
                log.error(f"regola {key}: {e.__class__.__name__}: {e}")
                continue
            for a in prodotti:
                a.rule = a.rule or key
                a.level = (over.level if over and over.level else None) or a.level or rule.level
                a.scope = a.scope or rule.scope
                grezzi.append(a)

        visti: dict[str, Alert] = {}
        for a in grezzi:
            visti.setdefault(a.id, a)         # due regole con lo stesso id: la prima vince

        self._scorda(now, visti)

        fuori: list[Alert] = []
        for a in visti.values():
            primo = self._primo.setdefault(a.id, now)
            self._visto[a.id] = now
            # `since` dichiarato dalla regola (es. sources.since del collector)
            # vince: chi ha raccolto il dato sa da quando dura meglio di noi.
            a.since = a.since or primo
            if _zittito(cfg, a):
                continue
            attesa = _attesa(cfg, CATALOGO.get(a.rule), cfg.rules.get(a.rule))
            if attesa and now - a.since < attesa:
                continue                      # il guasto non e' ancora durato abbastanza
            fuori.append(a)

        fuori.sort(key=lambda a: (_LIVELLI.index(a.level) if a.level in _LIVELLI else 9,
                                  a.scope, a.title, a.subject))
        return [a.to_dict() for a in fuori]

    def _scorda(self, now: int, visti: dict[str, Alert]) -> None:
        """Dimentica gli alert spariti da piu' della finestra di oblio."""
        for aid, quando in list(self._visto.items()):
            if aid not in visti and now - quando > FINESTRA_OBLIO:
                self._visto.pop(aid, None)
                self._primo.pop(aid, None)


def _abilitata(rule: Regola, over) -> bool:
    if over is not None and over.enabled is not None:
        return bool(over.enabled)
    return rule.enabled


def _attesa(cfg: AlertsConfig, rule: Optional[Regola], over) -> int:
    """Quanto il guasto deve durare prima di suonare: override di config,
    altrimenti quella della regola, altrimenti il default globale."""
    if over is not None and over.min_seconds is not None:
        return max(int(over.min_seconds), 0)
    if rule is not None and rule.min_seconds is not None:
        return max(rule.min_seconds, 0)
    return max(int(cfg.min_seconds or 0), 0)


def _zittito(cfg: AlertsConfig, a: Alert) -> bool:
    """Un silenziamento senza soggetto vale per l'intera regola."""
    for s in cfg.silenced:
        if s.rule == a.rule and (not s.subject or s.subject == a.subject):
            return True
    return False


def summary(alerts: list[dict]) -> dict:
    """Conteggi per livello e per scope: il badge della sidebar non deve
    ricontare la stessa lista in JavaScript."""
    tot = {lv: 0 for lv in _LIVELLI}
    per_scope: dict[str, dict] = {}
    for a in alerts:
        lv = a.get("level") if a.get("level") in _LIVELLI else "info"
        tot[lv] += 1
        row = per_scope.setdefault(a.get("scope") or "dashboard",
                                   {lv2: 0 for lv2 in _LIVELLI} | {"total": 0})
        row[lv] += 1
        row["total"] += 1
    return {**tot, "total": len(alerts), "by_scope": per_scope}


def catalogo() -> list[dict]:
    """Le regole, per la tendina dei silenziamenti e la documentazione in UI."""
    return [{"rule": r.key, "level": r.level, "scope": r.scope,
             "enabled": r.enabled, "descr": r.descr}
            for r in sorted(CATALOGO.values(), key=lambda r: r.key)]


_registry: Optional[AlertRegistry] = None


def get_alerts() -> AlertRegistry:
    global _registry
    if _registry is None:
        _registry = AlertRegistry()
    return _registry


# ── Regole ─────────────────────────────────────────────────────────

@regola("docker.restart_loop", level="warn", scope="services", min_seconds=120,
        descr="Container che continua a riavviarsi")
def _docker_restart_loop(snap: dict) -> list[Alert]:
    out = []
    for c in (snap.get("docker") or {}).get("containers") or []:
        if (c.get("status") or "") != "restarting":
            continue
        host, nome = c.get("host") or "", c.get("name") or "?"
        out.append(Alert(
            subject=f"{host}/{nome}" if host else nome,
            source="docker",
            title=f"Container {nome} in restart loop",
            detail=f"Docker su {host or 'locale'} lo sta riavviando in continuazione: "
                   f"il processo dentro il container esce subito dopo l'avvio.",
            action=f"Guarda perche' esce: docker logs --tail 50 {nome}",
        ))
    return out


@regola("docker.host_giu", level="critical", scope="services", min_seconds=60,
        descr="Host Docker che non risponde: i suoi container spariscono dall'elenco")
def _docker_host_giu(snap: dict) -> list[Alert]:
    out = []
    for hst in (snap.get("docker") or {}).get("hosts") or []:
        if not isinstance(hst, dict) or hst.get("reachable", True):
            continue
        # `seen_ok` falso = non ha mai risposto da quando il servizio gira: gli
        # host Docker arrivano anche dall'autodiscovery SSH, e la meta' della
        # LAN non fa girare Docker. Segnalarli sarebbe una fila di allarmi rossi
        # permanenti al primo avvio (misurato su un'istanza vera: sei).
        if not hst.get("seen_ok", True):
            continue
        nome = hst.get("name") or "?"
        out.append(Alert(
            subject=nome,
            source="docker",
            title=f"Host Docker {nome} non raggiungibile",
            detail=f"{hst.get('error') or 'nessuna risposta'}. I container di questo host "
                   f"non compaiono piu' nell'elenco e i conteggi dei servizi sono calati "
                   f"senza che li' sia cambiato nulla.",
            action="Controlla che la macchina sia accesa e che l'SSH risponda; poi che "
                   "l'utente configurato possa lanciare docker ps.",
        ))
    return out


@regola("services.systemd_giu", level="critical", scope="services", min_seconds=60,
        descr="Unit systemd marcata critica non attiva")
def _systemd_giu(snap: dict) -> list[Alert]:
    out = []
    for u in (snap.get("services") or {}).get("systemd") or []:
        if not u.get("critical") or u.get("ok"):
            continue
        nome, host = u.get("name") or "?", u.get("host") or ""
        stato = u.get("active_state") or u.get("status") or "sconosciuto"
        out.append(Alert(
            subject=f"{host}:{nome}" if host else nome,
            source="services",
            title=f"{u.get('label') or nome} non attivo",
            detail=f"L'unit systemd {nome} su {host or 'host locale'} e' in stato "
                   f"'{stato}', ed e' marcata critica in services.yaml.",
            action=f"systemctl status {nome} sull'host, poi journalctl -u {nome} -n 50",
        ))
    return out


@regola("services.windows_giu", level="critical", scope="services", min_seconds=60,
        descr="Servizio Windows marcato critico non in esecuzione")
def _windows_giu(snap: dict) -> list[Alert]:
    out = []
    for w in (snap.get("services") or {}).get("windows_services") or []:
        if not w.get("critical") or w.get("ok"):
            continue
        nome, host = w.get("name") or "?", w.get("host") or "?"
        etichetta = w.get("label") or nome
        if not w.get("available"):
            # L'host non ha risposto: di questo servizio non si sa niente, il
            # che e' diverso dal sapere che e' fermo.
            titolo = f"{etichetta}: stato non leggibile"
            dettaglio = (f"Non si riesce a leggere lo stato di {nome} su {host}: "
                         f"{w.get('error') or 'motivo non riportato'}.")
            azione = ("Verifica che il PC sia acceso e che utente, porta e chiave SSH "
                      "di quell'host siano giusti in Impostazioni -> Host SSH.")
        elif w.get("state") == "not-found":
            # Non installato non e' un guasto della macchina: e' il catalogo che
            # nomina un servizio che li' non c'e'.
            titolo = f"{etichetta} non installato"
            dettaglio = (f"Il servizio {nome} non esiste su {host}, ed e' marcato "
                         f"critico in services.yaml.")
            azione = (f"Controlla il nome con `sc query {nome}` su quell'host, oppure "
                      f"togli la voce dalla pagina Servizi.")
        else:
            titolo = f"{etichetta} non in esecuzione"
            dettaglio = (f"Il servizio Windows {nome} su {host} e' in stato "
                         f"'{w.get('state') or 'sconosciuto'}', ed e' marcato critico "
                         f"in services.yaml.")
            azione = f"Su quell'host: services.msc, oppure `sc start {nome}`."
        out.append(Alert(subject=f"{host}:{nome}", source="services",
                         title=titolo, detail=dettaglio, action=azione))
    return out


@regola("services.healthcheck_giu", level="warn", scope="services", min_seconds=120,
        descr="Controllo HTTP/TCP fallito")
def _healthcheck_giu(snap: dict) -> list[Alert]:
    out = []
    for c in (snap.get("services") or {}).get("healthchecks") or []:
        if c.get("ok"):
            continue
        nome = c.get("name") or "?"
        out.append(Alert(
            subject=nome,
            source="services",
            title=f"{nome} non risponde",
            detail=f"Il controllo {c.get('type') or '?'} su {c.get('target') or '?'} "
                   f"non passa: {c.get('detail') or 'nessuna risposta'}.",
            action="Verifica che il servizio sia acceso e che indirizzo e porta in "
                   "services.yaml siano ancora quelli giusti.",
        ))
    return out


# Nomi leggibili delle sorgenti del collector: stavano nel frontend
# (`sorgentiGiu`), cioe' in un posto dove non si potevano ne' silenziare ne'
# contare. Qui sono accanto alla regola che li usa.
_NOMI_SORGENTE = {
    "system": "il router", "interfaces": "le interfacce del router",
    "traffic": "il traffico WAN", "devices": "la scoperta dei dispositivi",
    "docker": "Docker", "services": "i servizi", "resources": "le risorse degli host",
    "wireguard": "WireGuard", "host_network": "la rete dell'host",
}
# Pagina che possiede il dato di ciascuna sorgente.
_SCOPE_SORGENTE = {
    "system": "dashboard", "interfaces": "wan", "traffic": "stats",
    "devices": "devices", "docker": "services", "services": "services",
    "resources": "resources", "wireguard": "wireguard", "host_network": "host",
}


@regola("collector.sorgente_giu", level="warn", scope="dashboard", min_seconds=0,
        descr="Una sorgente dati non risponde: i numeri mostrati sono vecchi")
def _sorgente_giu(snap: dict) -> list[Alert]:
    cfg = live_alerts_config()
    out = []
    for nome, s in (snap.get("sources") or {}).items():
        if not isinstance(s, dict) or s.get("ok"):
            continue
        fails = int(s.get("fails") or 0)
        out.append(Alert(
            subject=nome,
            # Un fallimento isolato capita (un timeout SSH); ripetuto significa
            # che quella parte della dashboard sta mostrando dati vecchi.
            level="critical" if fails >= max(cfg.source_critical_after, 1) else "warn",
            scope=_SCOPE_SORGENTE.get(nome, "dashboard"),
            source="collector",
            since=int(s.get("since") or 0),
            title=f"Dati fermi: {_NOMI_SORGENTE.get(nome, nome)}",
            # Niente orari assoluti: il container puo' girare su un fuso diverso
            # da quello di chi guarda, e un'ora sbagliata e' peggio di nessuna.
            detail=f"{s.get('error') or 'sorgente non raggiungibile'} "
                   f"({fails} tentativi falliti di fila; "
                   + ("i numeri mostrati sono quelli dell'ultima raccolta riuscita)"
                      if s.get("ts") else "non e' mai riuscita)"),
            action="Controlla che l'host di quella sorgente sia acceso e raggiungibile; "
                   "il motivo esatto e' nei log del servizio.",
        ))
    return out


def _chiave_sicurezza(motivo: str) -> str:
    """Identificativo stabile di un avviso di sicurezza.

    La posizione nella lista non va bene: se il primo motivo viene risolto, il
    secondo scivola all'indice zero, `since` riparte e un eventuale
    silenziamento aggancerebbe l'avviso sbagliato. La testa del messaggio e'
    invece il nome dell'impostazione che non va (`auth.method`, `auth.bypass_lan`).
    """
    return (motivo or "").split("—")[0].split(":")[0].strip()


@regola("security.api_aperta", level="critical", scope="settings", min_seconds=0,
        descr="Configurazione che lascia l'API accessibile senza login")
def _api_aperta(snap: dict) -> list[Alert]:
    return [
        Alert(
            subject=_chiave_sicurezza(motivo),
            source="security",
            title="L'API e' raggiungibile senza login",
            detail=motivo,
            action="Impostazioni -> Sicurezza: metti auth.method: basic con "
                   "bypass_lan: false e una password admin.",
        )
        for motivo in (snap.get("security") or [])
    ]


@regola("resources.nessun_host", level="info", scope="resources", min_seconds=0,
        descr="Nessun host SSH da cui leggere le risorse")
def _nessun_host(snap: dict) -> list[Alert]:
    avviso = (snap.get("resources") or {}).get("warning")
    if not avviso:
        return []
    return [Alert(
        source="resources",
        title="Nessun host configurato per le risorse",
        detail=str(avviso),
        action="Impostazioni -> Host SSH: aggiungi almeno un host.",
    )]


@regola("resources.host_ssh_giu", level="warn", scope="resources", min_seconds=300,
        descr="Host SSH acceso ma che non risponde alla lettura delle risorse")
def _host_ssh_giu(snap: dict) -> list[Alert]:
    # Con `skip_offline` spento (o al primo ciclo, quando non si sa ancora chi e'
    # acceso) la connessione parte comunque e fallisce: un portatile spento
    # risulterebbe un guasto permanente. Chi e' gia' dichiarato offline dalla
    # discovery non produce alert, come per i dispositivi.
    spenti = {ip for d in snap.get("devices") or []
              if not d.get("online") for ip in (d.get("ips") or [])}
    out = []
    for hst in (snap.get("resources") or {}).get("hosts") or []:
        # `skipped` = saltato perche' spento: sarebbe un alert su un dispositivo
        # offline, che qui non esistono per scelta.
        if hst.get("reachable") or hst.get("skipped") or hst.get("host") in spenti:
            continue
        ip = hst.get("host") or "?"
        out.append(Alert(
            subject=ip,
            source="resources",
            title=f"Risorse non leggibili da {hst.get('name') or ip}",
            detail=f"L'host risponde al ping ma la lettura via SSH fallisce: "
                   f"{hst.get('error') or 'motivo non riportato'}.",
            action="Verifica utente, porta e chiave SSH di quell'host in "
                   "Impostazioni -> Host SSH.",
        ))
    return out


# ── Rete dell'host che ospita LANMng ───────────────────────────────
# Erano tre regole fisse dentro host_inspector, visibili solo mentre si stava
# sulla pagina Host e senza alcun modo di zittirle: la prima resta accesa in
# permanenza su un host che ha due schede sulla stessa subnet per scelta.

def _reti_per_interfaccia(snap: dict) -> dict[str, list[str]]:
    from services.host_inspector import _network_of      # qui: evita il ciclo

    per_rete: dict[str, list[str]] = {}
    for iface in (snap.get("host_network") or {}).get("interfaces") or []:
        if iface.get("virtual"):
            continue
        for addr in iface.get("addresses") or []:
            if addr.get("family") != "inet":
                continue
            rete = _network_of(addr.get("ip") or "", addr.get("prefix"))
            if rete and iface["name"] not in per_rete.setdefault(rete, []):
                per_rete[rete].append(iface["name"])
    return per_rete


@regola("host.subnet_duplicata", level="warn", scope="host", min_seconds=0,
        descr="Piu' interfacce dell'host con un IP nella stessa subnet")
def _subnet_duplicata(snap: dict) -> list[Alert]:
    out = []
    for rete, schede in _reti_per_interfaccia(snap).items():
        if len(schede) < 2:
            continue
        out.append(Alert(
            subject=rete,                    # il CIDR: si silenzia una subnet sola
            source="host",
            title=f"Piu' interfacce sulla stessa subnet {rete}",
            detail=f"{len(schede)} interfacce non virtuali hanno un IP in {rete}: "
                   f"{', '.join(schede)}. Le risposte possono rientrare da una scheda "
                   f"diversa da quella di uscita e la diagnosi di rete diventa inaffidabile.",
            action="Se la doppia scheda e' voluta, silenzia questo avviso scrivendo il "
                   "motivo; altrimenti togli l'IP a una delle due.",
        ))
    return out


@regola("host.default_route_multipla", level="warn", scope="host", min_seconds=0,
        descr="Piu' default route con la stessa metrica: la via in uscita e' ambigua")
def _default_route_multipla(snap: dict) -> list[Alert]:
    tutte = [r for r in (snap.get("host_network") or {}).get("routes") or [] if r.get("default")]
    # Metriche diverse non sono un'anomalia: il kernel sceglie sempre la piu'
    # bassa e la seconda via e' una riserva voluta (es. cablata 100 e wifi 600
    # sullo stesso host). Segnalarle era un allarme acceso in permanenza su una
    # configurazione corretta. Ambiguo e' solo il pareggio.
    per_metrica: dict = {}
    for r in tutte:
        per_metrica.setdefault(r.get("metric"), []).append(r)
    rotte = next((g for g in per_metrica.values() if len(g) > 1), [])
    if len(rotte) < 2:
        return []
    vie = ", ".join(f"{r.get('dev')} via {r.get('gateway')} (metrica {r.get('metric')})"
                    for r in rotte)
    # Nessun soggetto: l'elenco delle vie cambia da un ciclo all'altro, e
    # metterlo nell'identita' farebbe ripartire `since` ad ogni variazione.
    return [Alert(
        source="host",
        title=f"{len(rotte)} default route con la stessa metrica",
        detail=f"A parita' di metrica il kernel non ha un criterio stabile: il traffico "
               f"in uscita puo' cambiare strada senza preavviso fra {vie}.",
        action="Distanzia le metriche (piu' bassa = preferita) perche' la scelta sia "
               "prevedibile, o lascia una sola via predefinita.",
    )]


@regola("host.link_giu_con_ip", level="info", scope="host", min_seconds=300,
        descr="Interfaccia con un IP configurato ma il cavo giu'")
def _link_giu_con_ip(snap: dict) -> list[Alert]:
    out = []
    for iface in (snap.get("host_network") or {}).get("interfaces") or []:
        if iface.get("virtual") or iface.get("state") != "down" or not iface.get("addresses"):
            continue
        out.append(Alert(
            subject=iface["name"],
            source="host",
            title=f"{iface['name']} ha un IP ma il link e' giu'",
            detail="L'interfaccia e' configurata ma non ha portante: il cavo e' staccato "
                   "o la scheda non e' stata attivata.",
            action="Se non serve piu', toglile l'indirizzo: cosi' non compare fra le "
                   "interfacce attive e non confonde la diagnosi.",
        ))
    return out


def _host_endpoint(endpoint: str) -> str:
    """Host di un endpoint WireGuard: `1.2.3.4:51820` o `[2001:db8::1]:51820`.

    Si toglie la porta perche' `relay_host` in configurazione e' un indirizzo
    e basta, e un confronto fra stringhe intere non aggancerebbe mai.
    """
    ep = (endpoint or "").strip()
    if ep.startswith("["):
        return ep[1:ep.index("]")] if "]" in ep else ep[1:]
    # Un IPv6 nudo (piu' di un ":") non ha porta da togliere: tagliare sull'ultimo
    # ":" ne farebbe un indirizzo diverso, e il confronto col relay fallirebbe.
    if ep.count(":") != 1:
        return ep
    testa, _, coda = ep.rpartition(":")
    return testa if coda.isdigit() else ep


@regola("wireguard.relay_giu", level="critical", scope="wireguard", min_seconds=300,
        descr="Il tunnel verso il relay WireGuard non fa piu' handshake")
def _relay_giu(snap: dict) -> list[Alert]:
    """Il peer del relay deve stare sempre su (richiesta del proprietario,
    2026-09-03): e' il tunnel da cui passa la VPN, non un telefono che si
    collega quando serve. Gli altri peer restano senza regola apposta."""
    relay = _host_endpoint(settings.wireguard.relay_host)
    if not relay:
        return []                     # nessun relay dichiarato: niente da sorvegliare
    now = int(time.time())
    out = []
    for iface in (snap.get("wireguard") or {}).get("interfaces") or []:
        for p in iface.get("peers") or []:
            if _host_endpoint(p.get("endpoint") or "") != relay:
                continue
            hs = int(p.get("last_handshake") or 0)
            # Si guarda il timestamp, non il campo `status`: quando il router
            # smette di rispondere lo snapshot resta quello di prima, e uno
            # `status` vecchio direbbe "attivo" di un tunnel caduto un'ora fa.
            if hs and now - hs <= HANDSHAKE_ATTIVO:
                continue
            nome = iface.get("name") or "wg"
            out.append(Alert(
                subject=f"{nome}/{relay}",
                source="wireguard",
                # Da quando e' caduto davvero, non da quando l'abbiamo notato:
                # cosi' l'attesa prima di suonare conta dal guasto, e "da
                # quando" resta giusto anche dopo un riavvio del backend.
                since=hs,
                title=f"Il tunnel WireGuard verso {relay} e' giu'",
                detail=("Nessun handshake da quando l'interfaccia e' stata caricata."
                        if not hs else
                        f"Ultimo handshake {max(1, (now - hs) // 60)} minuti fa: con il "
                        f"tunnel su ce n'e' uno almeno ogni due minuti."),
                action=f"Sul router: `wg show {nome}` e controlla che la WAN sia su. Se il "
                       f"relay ha cambiato indirizzo, aggiorna l'endpoint del peer e "
                       f"`wireguard.relay_host` in Impostazioni.",
            ))
    return out


@regola("wireguard.relay_non_trovato", level="info", scope="wireguard", min_seconds=0,
        descr="Il relay dichiarato in configurazione non e' fra i peer del router")
def _relay_non_trovato(snap: dict) -> list[Alert]:
    """Senza questo, un `relay_host` sbagliato spegnerebbe in silenzio la
    sorveglianza sul tunnel: nessun peer da guardare, nessun avviso, e nessuno
    che lo dice."""
    relay = _host_endpoint(settings.wireguard.relay_host)
    if not relay:
        return []
    peers = [p for i in (snap.get("wireguard") or {}).get("interfaces") or []
             for p in (i.get("peers") or [])]
    # Nessun peer: puo' voler dire router muto o WireGuard spento, e lo dicono
    # gia' la pagina e l'avviso di sorgente. Qui si parla solo di disallineamento.
    if not peers or any(_host_endpoint(p.get("endpoint") or "") == relay for p in peers):
        return []
    return [Alert(
        subject=relay,
        source="wireguard",
        title=f"Il relay {relay} non corrisponde a nessun peer",
        detail="`wireguard.relay_host` dice che il tunnel va li', ma nessun peer del router "
               "ha quell'endpoint: l'avviso sul tunnel giu' non ha nulla da sorvegliare.",
        action="Correggi `wireguard.relay_host` in Impostazioni con l'endpoint vero del peer, "
               "oppure lascialo vuoto se non c'e' un tunnel che deve stare sempre su.",
    )]


@regola("wireguard.peer_mai_connesso", level="info", scope="wireguard", min_seconds=0,
        enabled=False, descr="Peer WireGuard che non si e' mai collegato "
                             "(spenta di serie: un peer idle e' la norma)")
def _peer_mai_connesso(snap: dict) -> list[Alert]:
    out = []
    for iface in (snap.get("wireguard") or {}).get("interfaces") or []:
        for p in iface.get("peers") or []:
            if p.get("last_handshake"):
                continue
            nome = p.get("name") or "peer"
            out.append(Alert(
                subject=f"{iface.get('name') or 'wg'}/{nome}",
                source="wireguard",
                title=f"Il peer {nome} non si e' mai collegato",
                detail="Nessun handshake da quando il router ha caricato l'interfaccia.",
                action="Se il peer non serve piu', toglilo dalla configurazione del router.",
            ))
    return out
