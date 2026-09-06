"""
tests/conftest.py — Ambiente isolato della suite.

Regola numero uno: nessun test deve poter leggere il `config.yaml` VERO del repo
(contiene IP, host e credenziali di casa) ne' scrivere dentro `config/`.

L'isolamento si monta al **top-level di questo modulo**, non dentro una fixture,
perche' `config.py` risolve `LAN_CONFIG_FILE` e `LAN_ENV_FILE` a import-time
(config.py:291-293) e costruisce `settings = Settings()` come singleton globale
(config.py:356): dopo il primo `import config` non c'e' piu' modo di cambiare
idea, e `monkeypatch.setenv` in una fixture arriverebbe troppo tardi.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ── 1. Isolamento, PRIMA di qualunque import del backend ───────────

assert "config" not in sys.modules, (
    "config e' gia' importato: la suite leggerebbe la configurazione reale")

TMP = Path(tempfile.mkdtemp(prefix="lanmng-tests-"))
# Senza questo, ogni giro di test lascia una cartella in /tmp: la suite si lancia
# molte volte al giorno e nessuno andrebbe mai a ripulirle a mano.
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

# Password di test con hash bcrypt a cost 4 invece dei 12 di default: la
# verifica scende da ~250ms a ~1ms, cosi' provare il login vero costa quanto
# qualsiasi altro test. Non e' una scorciatoia di sicurezza: e' un valore che
# esiste solo dentro la suite.
TEST_PASSWORD = "password-di-test"
TEST_HASH = "$2b$04$shbHN1ZJf5fszh3DMF26lOGramESaAHAlLMyI8d/qOWWLAl8zD9bO"

# Config di test: indirizzi documentativi, mai quelli veri. `collect_interval_*`
# altissimi perche' nessun test deve mai vedere partire una raccolta.
(TMP / "config.yaml").write_text(
    "app_name: LANMng-test\n"
    "debug: false\n"
    "collect_interval_fast: 86400\n"
    "collect_interval_slow: 86400\n"
    "subnets:\n"
    "  - cidr: 192.0.2.0/24\n"
    "    label: test\n"
    "    scan: true\n"
    "router:\n"
    "  host: 192.0.2.1\n"
    "  user: tester\n"
    "discovery:\n"
    "  ssh:\n"
    "    hosts: []\n"
)

os.environ.update({
    "LAN_CONFIG_FILE":         str(TMP / "config.yaml"),
    # Volutamente inesistente: pydantic-settings tollera l'assenza, e se
    # qualcosa lo creasse finirebbe qui invece che in config/.
    "LAN_ENV_FILE":            str(TMP / "secrets.env"),
    "LAN_DEVICES_CATALOG":     str(TMP / "devices.yaml"),
    "LAN_SERVICES_CATALOG":    str(TMP / "services.yaml"),
    "LAN_AUDIT_LOG":           str(TMP / "audit.log"),
    # Storico su SQLite: senza questo i test scriverebbero nel database
    # reale del repo (config/history.db) sporcando dati veri.
    "LAN_HISTORY_DB":          str(TMP / "history.db"),
    # secret_key esplicita: rende deterministica la firma dei cookie e, come
    # effetto voluto, disinnesca _bootstrap_secret_key() del lifespan, che esce
    # subito quando la chiave non e' quella di default (main.py:47).
    "LAN_SECRET_KEY":          "chiave-di-test-non-usare-in-produzione",
    "LAN_AUTH__METHOD":        "basic",
    "LAN_AUTH__USERNAME":      "admin",
    "LAN_AUTH__PASSWORD_HASH": TEST_HASH,
    "LAN_AUTH__BYPASS_LAN":    "false",
})


# ── 2. Reset dello stato globale fra un test e l'altro ─────────────
# Il backend non usa Depends per i connettori: sono globali lazy `_x = None` piu'
# `get_x()`. Senza questo azzeramento un fake piazzato da un test resterebbe
# attivo in quelli successivi, e i RateLimiter porterebbero i colpi da un test
# all'altro facendo scattare 429 dove non c'entra nulla.

_SINGLETONS = (
    "services.alerts:_registry",
    "services.collector:_collector",
    "services.config_store:_store",
    "services.device_store:_store",
    "services.docker_client:_manager",
    "services.healthcheck:_checker",
    "services.history_store:_store",
    "services.host_metrics:_collector",
    "services.openwrt:_router",
    "services.openwrt:_ssh",
    "services.scanner:_scanner",
    "services.secrets_store:_store",
    "services.service_store:_store",
    "services.systemd_monitor:_monitor",
    "services.wireguard:_wg_service",
    "services.windows_services:_monitor",
)

_RATELIMITERS = (
    "routers.auth:_login_rl",
    "routers.devices:_scan_rl",
    "routers.services:_refresh_rl",
    "routers.tools:_rl",
)


def _gia_importato(nome: str):
    """Solo moduli gia' in sys.modules: importarli qui vanificherebbe il lazy-load
    e costruirebbe connettori che il test non ha chiesto."""
    return sys.modules.get(nome)


@pytest.fixture(autouse=True)
def _stato_pulito():
    yield
    # Lo storico tiene una connessione SQLite aperta: azzerare il singleton
    # senza chiuderla lascerebbe un descrittore per ogni test che lo tocca.
    if (m := _gia_importato("services.history_store")) is not None and m._store is not None:
        m._store.close()
    for ref in _SINGLETONS:
        nome, attr = ref.split(":")
        if (m := _gia_importato(nome)) is not None:
            setattr(m, attr, None)
    for ref in _RATELIMITERS:
        nome, attr = ref.split(":")
        if (m := _gia_importato(nome)) is not None:
            getattr(m, attr)._hits.clear()
    if (m := _gia_importato("services.ssh_hosts")) is not None:
        m._cache = {"mtime": 0.0, "cfg": None}   # cache invalidata sul mtime
    if (m := _gia_importato("services.alerts")) is not None:
        m._cache = {"mtime": 0.0, "gen": -1, "cfg": None}
    if (m := _gia_importato("services.terminal")) is not None:
        m._active = 0                            # contatore delle sessioni aperte


# ── 3. App e client ────────────────────────────────────────────────

SNAPSHOT_FINTO = {
    "devices": [],
    "collector": {"running": False},
    "meta": {"origine": "test"},
    "timestamp": 0,
}


@pytest.fixture(scope="session")
def app():
    import main
    return main.app


@pytest.fixture
def client(app, monkeypatch):
    """TestClient usato SENZA context manager: cosi' Starlette non esegue il
    lifespan, che farebbe partire il collector (SSH al router, nmap, ping sweep,
    docker su SSH) e proverebbe a scrivere config/secrets.env.

    I due monkeypatch sono la seconda cintura, per un eventuale test che entri
    nel `with`: va patchato `main.collector`, l'istanza catturata a import-time
    da main.py:39, perche' patchare `services.collector.get_collector` dopo
    l'import non cambierebbe cio' che il lifespan ha gia' in mano.
    """
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_bootstrap_secret_key", lambda: None)

    async def _collector_inerte():
        await asyncio.Event().wait()   # attende e si lascia cancellare: nessuna rete

    monkeypatch.setattr(main.collector, "run", _collector_inerte)
    # Snapshot pre-popolato: get_snapshot() su un dict vuoto innescherebbe una
    # raccolta vera su /api/snapshot e sul WebSocket /ws.
    monkeypatch.setattr(main.collector, "_snapshot", dict(SNAPSHOT_FINTO))
    return TestClient(app)


@pytest.fixture
def auth_client(client):
    """Client con sessione valida, firmata a mano: nessun round di bcrypt e
    nessuna dipendenza dal router /api/auth/login."""
    from config import settings
    from middleware.auth import COOKIE, make_token
    client.cookies.set(COOKIE, make_token(settings.auth.username))
    return client


class _ClientDaLan:
    """Riscrive `scope["client"]` prima di passare la richiesta all'app.

    Serve perche' TestClient fissa l'host chiamante a "testclient", che non e' un
    IP: `is_lan()` lo rifiuta e il ramo `bypass_lan` resterebbe non testabile.
    Qui l'indirizzo e' vero, quindi la funzione sotto esame gira davvero.
    """

    def __init__(self, app, indirizzo=("192.168.99.5", 45000)):
        self.app = app
        self.indirizzo = indirizzo

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=self.indirizzo)
        await self.app(scope, receive, send)


@pytest.fixture
def lan_client(app):
    """Client che il middleware vede arrivare da un indirizzo privato vero."""
    from fastapi.testclient import TestClient
    return TestClient(_ClientDaLan(app), base_url="http://testserver")


@pytest.fixture
def scanner_finto(monkeypatch):
    """Neutralizza lo scan di rete lanciato in background da POST /api/devices/scan:
    senza, il test aspetterebbe nmap e il ping sweep su una subnet inesistente."""
    import routers.devices as mod

    class _Scanner:
        def __init__(self):
            self.chiamate = 0

        async def scan(self):
            self.chiamate += 1

    finto = _Scanner()
    monkeypatch.setattr(mod, "get_scanner", lambda: finto)
    return finto


@pytest.fixture
def origine_valida():
    """Header che soddisfa la difesa CSRF: `same_origin` confronta il solo
    hostname di Origin con quello di Host, e sotto TestClient Host e'
    "testserver"."""
    return {"Origin": "http://testserver"}


@pytest.fixture
def origine_estranea():
    return {"Origin": "http://attaccante.example"}
