"""
config.py — Configurazione centralizzata
=========================================
Ordine di precedenza (dal piu' debole al piu' forte):
    1. default definiti qui
    2. valori da config/config.yaml
    3. variabili d'ambiente con prefisso LAN_ (es. LAN_DEBUG=true,
       LAN_ROUTER__PASSWORD=segreto). Sezioni annidate: doppio underscore.

Unica fonte di verita' della configurazione per tutto il backend.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator

# Colore CSS sicuro: solo esadecimale #RGB/#RGBA/#RRGGBB/#RRGGBBAA o nome
# alfabetico. Neutralizza payload tipo `x"><img onerror=...>` nel campo color.
_SAFE_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,20})$")
_DEFAULT_COLOR = "#8b94a8"
from pydantic_settings import (
    BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource,
)


def _none_to_list(v):
    """Una chiave YAML lasciata vuota diventa None: la trattiamo come lista vuota."""
    return v or []


def _none_to_dict(v):
    return v or {}


# ── Sotto-configurazioni ───────────────────────────────────────────

class SubnetConfig(BaseModel):
    """Una subnet della rete: usata sia per lo scan (nmap) sia per
    etichettare/colorare i dispositivi nella UI. Nessun valore hardcoded
    nel codice: la topologia vive qui (config.yaml, editabile)."""
    cidr: str                                # es. "192.0.2.0/24"
    label: str                               # nome breve in UI, es. "LAN"
    color: str = _DEFAULT_COLOR              # colore mappa/legenda
    scan: bool = True                        # includila nello scan nmap

    @field_validator("color")
    @classmethod
    def _safe_color(cls, v: str) -> str:
        # Il colore finisce in un attributo style del frontend: un valore non
        # conforme viene neutralizzato al default (difesa contro XSS via config).
        return v if isinstance(v, str) and _SAFE_COLOR_RE.match(v.strip()) else _DEFAULT_COLOR


class RouterConfig(BaseModel):
    name: str = ""                           # nome visualizzato (mappa); default: host
    host: str = ""                           # IP/hostname del router (da config.yaml)
    port: int = 22
    user: str = "root"
    # Auth SSH: si offrono entrambe (chiave preferita, password come fallback).
    password: Optional[str] = None
    ssh_key: Optional[str] = None            # path assoluto alla chiave privata
    # Interfaccia di uplink (WAN) per il grafico traffico; se vuoto si prova
    # la lista wan_candidates. Su questo router l'uplink e' il tethering USB (eth1).
    wan_interface: str = ""
    # Nomi tipici delle interfacce di uplink OpenWrt fra cui scegliere se
    # wan_interface non e' impostato (unica fonte per backend/frontend).
    wan_candidates: list[str] = ["wan", "wan6", "usb0", "wwan", "tethering"]
    # Host di riferimento per test di connettivita'/latenza internet dal router.
    internet_probe: str = "8.8.8.8"
    # Device wireless per la lista dei client associati (iwinfo).
    wireless_device: str = "wlan0"
    # Deprecati: il router si interroga solo via SSH+ubus, non piu' via LuCI-RPC
    # HTTP (non installata sul router). Mantenuti per compatibilita' config.
    luci_url: str = ""
    luci_user: str = "root"
    luci_password: Optional[str] = None


class DockerHost(BaseModel):
    """Un host Docker remoto da monitorare."""
    name: str
    method: str = "ssh"               # ssh | tcp
    host: Optional[str] = None        # IP/hostname
    port: int = 2375                  # per method=tcp
    user: Optional[str] = None        # per method=ssh (default: discovery.ssh.default_user)
    key: Optional[str] = None         # per method=ssh
    password: Optional[str] = None


class DockerConfig(BaseModel):
    # Host locale (dove gira il backend): via socket Unix montato.
    local_enabled: bool = True
    socket: str = "/var/run/docker.sock"
    local_name: str = "localhost"
    # Host Docker remoti espliciti.
    hosts: list[DockerHost] = []
    # Interroga anche gli host SSH dove la discovery trova Docker.
    autodiscover: bool = True

    _v_hosts = field_validator("hosts", mode="before")(_none_to_list)


class UIConfig(BaseModel):
    """Preferenze dell'interfaccia che valgono per tutti i browser.

    La lingua qui e' il DEFAULT del servizio, non un obbligo: chi la sceglie
    dalla dashboard vince, e la sua scelta resta nel suo browser. Serve a chi
    installa per un'altra persona — o per se' su piu' dispositivi — senza
    doverla reimpostare ovunque.
    """
    default_language: str = ""      # "" = lingua del browser, poi inglese


class WireGuardConfig(BaseModel):
    interface: str = "wg0"
    config_file: str = ""
    # Endpoint del relay sempre attivo. Non e' solo un'etichetta: la regola
    # `wireguard.relay_giu` riconosce da qui QUALE peer deve stare sempre su.
    # Vuoto = nessun relay, e quella regola non gira.
    relay_host: str = ""
    # Mapping pubkey -> nome leggibile
    peer_names: dict[str, str] = {}

    _v_pn = field_validator("peer_names", mode="before")(_none_to_dict)


class ToolsConfig(BaseModel):
    """Strumenti di rete della pagina Tools.

    `speedtest_url` deve rispondere con un flusso di byte grande a piacere: si
    scarica e si butta, misurando quanto ci mette. Il bersaglio e' in config e
    non nel codice perche' e' una scelta (chi lo ospita vede il tuo IP), e su
    una WAN a consumo il test si paga in traffico: da qui anche il tetto.
    """
    speedtest_url: str = "https://speed.cloudflare.com/__down?bytes={bytes}"
    speedtest_mb: int = 5                    # quanto scaricare di serie
    speedtest_max_mb: int = 50               # tetto invalicabile dalla UI
    # Server whois di partenza: da li' si segue il rimando al registro giusto.
    whois_server: str = "whois.iana.org"


class AuthConfig(BaseModel):
    method: str = "basic"                    # none | basic
    # Salta l'auth per QUALUNQUE indirizzo privato piu' il loopback (vedi
    # is_lan in middleware/auth.py: ip.is_private or ip.is_loopback), non solo
    # per la propria subnet. Il pentest 2026-08-17 e' partito da li'.
    bypass_lan: bool = False
    username: str = "admin"
    password_hash: Optional[str] = None      # hash bcrypt (mai password in chiaro)


# ── Discovery (sorgenti pluggabili per scoprire piu' roba possibile) ─

class NmapDiscovery(BaseModel):
    enabled: bool = True
    ping_scan: bool = True            # nmap -sn: trova host vivi (anche non in DHCP/ARP)
    # nmap -sT sulle porte elencate. Acceso: misurato il 2026-08-19 sulle tre
    # subnet (768 indirizzi) costa 10-11s contro i 9-46s del solo ping scan, cioe'
    # niente: le porte si scansionano solo sui pochi host che rispondono.
    port_scan: bool = True
    os_detection: bool = False        # nmap -O (richiede privilegi root)
    ports: str = "22,53,80,443,3000,8000,8080,9000,9090,51820"
    extra_args: str = ""              # argomenti aggiuntivi liberi
    # nmap decide se e' privilegiato guardando l'uid: non vede le capability del
    # binario, quindi da non-root ripiega su un ping TCP verso la porta 80 e
    # dichiara spenti gli host che non la espongono. --privileged lo corregge.
    privileged: bool = True
    # Interfaccia da cui scansionare. "" (consigliato) = decide il routing del
    # kernel, che sa raggiungere ogni subnet; fissarla fa perdere host (vedi la
    # docstring di NmapProvider). Va valorizzata solo per forzare un percorso.
    interface: str = ""
    # nmap risolve da solo i nomi degli host trovati, in modo seriale: su questa
    # rete costava 27s su 8 host. Gli hostname li raccoglie gia' il provider
    # reverse DNS, in parallelo, quindi qui la risoluzione si tiene spenta.
    resolve_names: bool = False
    # Indirizzi (o CIDR) che si continuano a cercare col ping ma che NON si
    # port-scansionano. Nasce per il router: ogni connect TCP verso la sua
    # porta 22 gli finisce nel syslog, e con un buffer da 64 KB il rumore del
    # monitoraggio spingeva fuori gli eventi veri in meno di un'ora (misurato:
    # 226 righe su 243 in 50 minuti erano nostre). Escluderli dal port scan
    # toglie il rumore alla fonte; il router resta comunque fra i dispositivi
    # perche' lo scan a ping su quegli indirizzi si fa lo stesso.
    port_scan_exclude: list[str] = []
    # Aggiunge da solo a `port_scan_exclude` gli indirizzi del router: quello
    # configurato piu' quelli che il router stesso dichiara sulle proprie
    # interfacce (sono sei su questa rete). Nessun indirizzo nel codice: si
    # leggono a runtime, quindi restano giusti anche se la rete cambia.
    port_scan_skip_router: bool = True

    _v_pse = field_validator("port_scan_exclude", mode="before")(_none_to_list)


# Sistemi che i connettori sanno interrogare. "auto" = lo scopre da solo alla
# prima connessione (vedi services/ssh_hosts.py:risolvi_os).
OS_HOST = ("auto", "linux", "windows")


class SSHHost(BaseModel):
    ip: str
    user: Optional[str] = None        # se None usa default_user
    key: Optional[str] = None         # se None usa default_key
    password: Optional[str] = None
    port: int = 22
    # Che sistema parla l'host: decide se mandargli comandi POSIX o PowerShell.
    # Su Windows la shell di default via SSH e' cmd.exe, quindi uno script /bin/sh
    # non fallisce nemmeno: restituisce spazzatura che sembra un host rotto.
    os: str = "auto"

    @field_validator("os", mode="before")
    @classmethod
    def _v_os(cls, v):
        v = (str(v or "auto")).strip().lower()
        if v not in OS_HOST:
            raise ValueError(f"os dev'essere uno fra {', '.join(OS_HOST)}: ricevuto {v!r}")
        return v


class PingSweepDiscovery(BaseModel):
    """Sweep ICMP delle subnet con scan=True, eseguito dal backend.

    E' la sorgente piu' reattiva: trova un IP nuovo (o cambiato) senza
    dipendere dalle tabelle DHCP/ARP del router, che si aggiornano tardi.
    """
    enabled: bool = True
    concurrency: int = 128            # ping in parallelo (piu' alto = sweep piu' rapido)
    timeout: int = 1                  # secondi di attesa per risposta


class SSHDiscovery(BaseModel):
    """Raccoglie facts via SSH dagli host della LAN (tutti tuoi)."""
    enabled: bool = False
    default_user: Optional[str] = None
    default_key: Optional[str] = None
    hosts: list[SSHHost] = []         # host abilitati (con eventuali override)
    # Un host spento fa scadere la connect: senza timeout basso lo scan si
    # allunga di decine di secondi per ogni host irraggiungibile.
    connect_timeout: int = 5
    # Salta gli host che non hanno risposto al ping in questo ciclo.
    skip_offline: bool = True

    _v_hosts = field_validator("hosts", mode="before")(_none_to_list)


class SNMPDiscovery(BaseModel):
    """Interroga device che parlano SNMP (AP, switch, stampanti)."""
    enabled: bool = False
    community: str = "public"
    version: str = "2c"               # 1 | 2c
    hosts: list[str] = []             # IP da interrogare

    _v_hosts = field_validator("hosts", mode="before")(_none_to_list)


class ReverseDNSDiscovery(BaseModel):
    # Risolve gli hostname via PTR usando il resolver di sistema (che sulla LAN
    # e' il router/dnsmasq, quindi copre anche i nomi .lan). Nessun resolver
    # configurabile: la libreria standard usa il resolver di sistema.
    enabled: bool = True


class DiscoveryConfig(BaseModel):
    ping_sweep: PingSweepDiscovery = PingSweepDiscovery()
    nmap: NmapDiscovery = NmapDiscovery()
    ssh: SSHDiscovery = SSHDiscovery()
    snmp: SNMPDiscovery = SNMPDiscovery()
    reverse_dns: ReverseDNSDiscovery = ReverseDNSDiscovery()


class CorsConfig(BaseModel):
    """Origini esterne autorizzate a chiamare le API con le credenziali.

    Normalmente **vuoto**: l'unico client e' la SPA, servita dallo stesso host
    che espone le API, e una richiesta same-origin non passa da CORS. Elencare
    origini qui significa dichiarare fidato un altro sito: si fa solo se serve
    davvero un client esterno (es. "http://altrohost:3000").
    """
    allowed_origins: list[str] = []

    _v_origins = field_validator("allowed_origins", mode="before")(_none_to_list)


class TerminalConfig(BaseModel):
    """Terminale SSH dalla dashboard.

    Gli host raggiungibili NON si configurano qui: sono quelli gia' noti
    (router + `discovery.ssh.hosts` + host systemd), cosi' non esiste modo di
    far aprire al backend una sessione verso una macchina arbitraria.
    """
    enabled: bool = True
    term_type: str = "xterm-256color"
    idle_timeout: int = 900           # secondi di inattivita' prima della chiusura
    max_sessions: int = 3             # sessioni contemporanee
    # Registra i comandi digitati nel log di audit. L'euristica salta le righe
    # digitate dopo un prompt di password, ma non e' infallibile: se digiti
    # segreti a prompt non standard, spegni questa opzione.
    audit_commands: bool = True


class HostMetricsConfig(BaseModel):
    """Risorse (CPU, RAM, dischi, temperature) degli host della LAN.

    Si leggono via SSH, la stessa strada gia' usata per systemd e Docker:
    nessun agente da installare sugli host, si riusano host e credenziali di
    `discovery.ssh` (vedi services/ssh_hosts.py).
    """
    enabled: bool = True
    # Host da interrogare (IP). Vuoto = tutti quelli di discovery.ssh.hosts.
    hosts: list[str] = []
    # Secondi fra due raccolte. E' anche la finestra su cui si media la CPU:
    # la percentuale e' il delta di /proc/stat fra due letture, non un campione
    # istantaneo, cosi' il numero non dipende da quando lo si e' preso.
    interval: int = 60
    # Salta gli host che il ciclo corrente ha visto spenti: una connect verso
    # un host irraggiungibile costa il timeout intero.
    skip_offline: bool = True
    top_processes: int = 6            # quanti processi piu' pesanti mostrare
    history_points: int = 60          # punti di storia per host (~1h a 60s)
    command_timeout: int = 15         # tetto sull'esecuzione del comando remoto

    _v_hosts = field_validator("hosts", mode="before")(_none_to_list)


class HistoryConfig(BaseModel):
    """Storico persistente delle serie temporali (SQLite in cartella config).

    Senza storico ogni riavvio del container azzera i grafici: traffico e
    risorse vivono solo in memoria. Il file sta in `config/` perche' e' l'unica
    cartella scrivibile del container (rootfs in sola lettura) ed e' anche
    l'unica che la procedura di aggiornamento non tocca mai.

    Per contenere la crescita i punti si aggregano invece di cancellarsi: si
    tiene il dettaglio pieno per le ultime `full_hours`, poi medie da
    `bucket_seconds`, e si butta oltre `retain_days`.
    """
    enabled: bool = True
    retain_days: int = 7              # finestra totale conservata
    full_hours: int = 24              # oltre questa eta' i punti si aggregano
    bucket_seconds: int = 300         # granularita' dei punti aggregati (5 min)
    compact_every_minutes: int = 60   # ogni quanto gira la compattazione
    # Tetto di punti per risposta API: un grafico non ne disegna di piu' e cosi'
    # la query resta lontana dal timeout del proxy.
    max_points: int = 1500


class LogExclude(BaseModel):
    """Una regola di scarto: le righe che le corrispondono non entrano.

    Nasce perche' il log del backend cresce a un ritmo che nessuna retention a
    giorni sa contenere, e un terzo del volume sono poche frasi ripetute. Una
    riga scartata **non finisce nel buffer, nel database, ne' in pagina** — ma
    resta in `docker logs`, che e' un handler diverso: si smette di archiviare
    il rumore, non di produrlo, cosi' resta il modo di ritrovarlo se serve.

    `pattern` (testo cercato nel messaggio) e `src` (chi ha scritto la riga)
    valgono in AND. Una regola senza ne' l'uno ne' l'altro e' ignorata: non deve
    essere possibile svuotare il log per una svista nello YAML.
    """
    pattern: str = ""                 # testo cercato nel messaggio (o regex)
    src: str = ""                     # logger/servizio che ha scritto la riga
    regex: bool = False               # pattern come espressione regolare
    # Sorgenti a cui si applica; vuoto = tutte. "journal" copre ogni host.
    # L'audit non e' filtrabile in nessun caso: e' la traccia da leggere quando
    # qualcosa e' andato storto, e un filtro li' sarebbe un buco.
    sources: list[str] = []
    enabled: bool = True
    note: str = ""                    # perche' si scarta (si rilegge fra mesi)

    _v_sources = field_validator("sources", mode="before")(_none_to_list)


class LogsConfig(BaseModel):
    """Pagina Logs: quali sorgenti si vedono, quanto se ne tiene, come si segue.

    Le sorgenti sono quattro e non hanno lo stesso costo. Il syslog del
    **router** si legge a richiesta e non si archivia mai: quella macchina ha
    poche risorse ed e' gia' lei a tenere il proprio buffer. I log del
    **backend** vivono solo in memoria (e in `docker logs`, che sparisce con
    l'immagine), quindi sono l'unica cosa che si perde davvero: quelli si
    archiviano. Il registro di **audit** resta il file append-only di sempre —
    la copia nel database serve solo per cercarci dentro dalla pagina.
    """
    # Righe del backend tenute in memoria. E' anche quanto si riesce a mostrare
    # subito dopo un riavvio, prima che l'archivio abbia di nuovo qualcosa.
    buffer_lines: int = 2000
    max_lines: int = 5000             # tetto di righe per singola richiesta
    # Ogni quanto il "segui" ricontrolla le sorgenti che non sanno notificare
    # (router e journalctl). Sul router e' anche il ritmo con cui gli si parla:
    # abbassarlo vuol dire dargli piu' lavoro, ed e' il motivo del minimo sotto.
    tail_interval: int = 10
    tail_interval_min: int = 5
    # Un flusso che non finisce mai tiene occupata una connessione per sempre.
    # Allo scadere si chiude dichiarandolo, e la pagina lo riapre: cosi' una
    # scheda dimenticata aperta di notte non lascia dietro di se' un canale.
    tail_max_seconds: int = 3600
    persist: bool = True              # archivia backend e audit (mai il router)
    retain_days: int = 7              # per quanto si tengono le righe del backend
    # Tetto sulle righe del backend archiviate. Serve perche' la retention a
    # giorni NON limita un log di cui non si controlla il ritmo: misurato
    # sull'istanza vera, sette giorni valevano 46 MB contro i ~5 dello storico
    # delle serie. Vale il limite che scatta per primo fra questo e retain_days.
    # L'audit non ha tetto: e' poche righe al giorno e sono quelle che contano.
    max_rows: int = 50000
    # L'audit si tiene molto piu' a lungo: e' la traccia da consultare quando il
    # sospetto arriva mesi dopo. Il file su disco resta comunque completo.
    audit_retain_days: int = 90
    audit_enabled: bool = True        # mostrare il registro di audit in pagina
    # Host di cui si offre il journalctl. Vuoto = host systemd di default piu'
    # quelli di `discovery.ssh`: nessun nome di macchina vive nel codice.
    journal_hosts: list[str] = []
    # Righe da buttare via (vedi LogExclude). Si applicano in due punti: quando
    # la riga nasce, cosi' non occupa posto nell'archivio, e quando si legge una
    # sorgente, cosi' spariscono anche le righe archiviate prima della regola.
    # Le modifiche richiedono il riavvio del servizio, come il resto della
    # configurazione (il pulsante e' in Impostazioni).
    exclude: list[LogExclude] = []

    _v_journal = field_validator("journal_hosts", mode="before")(_none_to_list)
    _v_exclude = field_validator("exclude", mode="before")(_none_to_list)


class SystemdConfig(BaseModel):
    """Accesso allo stato delle unit systemd dell'host.

    Nel container non esiste `systemctl`: per leggere il systemd dell'HOST si
    esegue il comando via SSH verso l'host stesso (es. user@127.0.0.1, riusa
    la chiave gia' montata in /app/ssh). Con ssh_enabled=False si usa il
    `systemctl` locale (utile solo se il backend gira direttamente sull'host).
    """
    ssh_enabled: bool = False
    ssh_host: str = "127.0.0.1"
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key: str = "/app/ssh/id_ed25519"


class AlertSilence(BaseModel):
    """Un alert zittito per sempre: regola + soggetto, con il motivo scritto.

    Il motivo non e' decorativo: fra sei mesi deve essere possibile capire
    perche' quell'avviso non suona piu' (es. "quell'host ha due schede sulla
    stessa subnet per scelta"). `subject` vuoto zittisce l'intera regola.
    """
    rule: str
    subject: str = ""
    reason: str = ""
    since: int = 0                           # epoch del silenziamento


class AlertRule(BaseModel):
    """Override per una singola regola. Tutti i campi opzionali: assente = default."""
    enabled: Optional[bool] = None
    level: Optional[str] = None              # critical | warn | info
    min_seconds: Optional[int] = None        # quanto deve durare prima di suonare


class AlertsConfig(BaseModel):
    """Alert contestuali: regole valutate sullo snapshot del collector.

    `silenced` e' l'unica sezione riletta a caldo dal file (senza riavvio):
    vedi `services/alerts.py:live_alerts_config`.
    """
    enabled: bool = True
    # Attesa predefinita per le regole che non ne dichiarano una propria.
    min_seconds: int = 120
    # Dopo quanti fallimenti consecutivi una sorgente giu' diventa critical.
    source_critical_after: int = 3
    rules: dict[str, AlertRule] = {}
    silenced: list[AlertSilence] = []

    _v_rules = field_validator("rules", mode="before")(_none_to_dict)
    _v_silenced = field_validator("silenced", mode="before")(_none_to_list)


# ── Settings principale ────────────────────────────────────────────

# Cartella di config: /app/config nel container, altrimenti config/ del repo.
_CONFIG_DIR = ("/app/config" if Path("/app/config").exists()
               else str(Path(__file__).resolve().parent.parent / "config"))
# File config.yaml: override via LAN_CONFIG_FILE, altrimenti nella cartella config.
_CFG_FILE = os.environ.get("LAN_CONFIG_FILE", f"{_CONFIG_DIR}/config.yaml")
# File dei segreti (KEY=VALUE, stile .env): override via LAN_ENV_FILE.
_ENV_FILE = os.environ.get("LAN_ENV_FILE", f"{_CONFIG_DIR}/secrets.env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LAN_",
        env_nested_delimiter="__",
        env_file=_ENV_FILE,
        yaml_file=_CFG_FILE,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        # Priorita' (alta -> bassa): init kwargs, ENV (LAN_*), secrets.env, config.yaml, secrets.
        # Cosi' ENV e il file dei segreti sovrascrivono config.yaml.
        return (init_settings, env_settings, dotenv_settings,
                YamlConfigSettingsSource(settings_cls), file_secret_settings)

    app_name: str = "LANMng"
    # debug=True -> log piu' verbosi (DEBUG). Nessun effetto sui dati: sempre reali.
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # Intervalli del collector (secondi)
    collect_interval_fast: int = 10          # traffico, interfacce, sistema
    collect_interval_slow: int = 60          # devices, docker, systemd, wg, healthcheck

    # Sotto-config
    router: RouterConfig = RouterConfig()
    docker: DockerConfig = DockerConfig()
    ui: UIConfig = UIConfig()
    wireguard: WireGuardConfig = WireGuardConfig()
    auth: AuthConfig = AuthConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    systemd: SystemdConfig = SystemdConfig()
    host_metrics: HostMetricsConfig = HostMetricsConfig()
    history: HistoryConfig = HistoryConfig()
    logs: LogsConfig = LogsConfig()
    terminal: TerminalConfig = TerminalConfig()
    tools: ToolsConfig = ToolsConfig()
    cors: CorsConfig = CorsConfig()
    alerts: AlertsConfig = AlertsConfig()

    # Subnet della rete (scan + etichette/colori UI). Vuoto finche' non configurato.
    subnets: list[SubnetConfig] = []

    # Cataloghi
    devices_catalog: str = f"{_CONFIG_DIR}/devices.yaml"
    services_catalog: str = f"{_CONFIG_DIR}/services.yaml"
    # Registro delle azioni sensibili (tool di rete, terminale SSH).
    audit_log: str = f"{_CONFIG_DIR}/audit.log"
    # Storico delle serie temporali (SQLite). Sopravvive ai riavvii perche'
    # la cartella config e' un bind mount che gli aggiornamenti non toccano.
    history_db: str = f"{_CONFIG_DIR}/history.db"

    _v_subnets = field_validator("subnets", mode="before")(_none_to_list)

    @property
    def scan_subnets(self) -> list[str]:
        """CIDR da passare a nmap: le subnet con scan=True."""
        return [s.cidr for s in self.subnets if s.scan]


settings = Settings()
