"""
LANMng — FastAPI backend
========================
Entry point. Monta i router, espone WebSocket /ws per gli update live e
serve il frontend statico.

Avvio (Docker / host):
    uvicorn main:app --host 0.0.0.0 --port 8000
Log verbosi: LAN_DEBUG=true (i dati restano sempre reali, niente mock).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from middleware.auth import (AuthMiddleware, COOKIE, auth_enabled, is_lan, same_origin,
                             security_warnings, valid_token)
from routers import (alerts, auth, config_api, devices, docker_svc, history, host, logs,
                     services, system, terminal, tools, wan, wireguard)
from services.collector import get_collector
from services.errors import exc_text
from services.log_buffer import install as install_log_buffer

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
# asyncssh logga ogni comando/canale a INFO: troppo rumore (una riga per ogni
# ubus call). Teniamo solo warning/errori della libreria.
logging.getLogger("asyncssh").setLevel(logging.WARNING)
# httpx scrive una riga INFO per OGNI chiamata all'API Docker ("HTTP Request:
# GET http://docker/v1.43/containers/..."): sull'istanza vera erano un quarto
# di tutto il log del backend e non dicono niente che non si veda altrove.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Buffer in memoria dei log del backend (pagina Logs). Va installato SUBITO
# dopo basicConfig: le righe scritte prima non finirebbero da nessuna parte se
# non in stdout, e sono quelle dell'avvio, cioe' proprio quelle che si vanno a
# cercare quando qualcosa non parte.
install_log_buffer()
log = logging.getLogger("lanmng")

collector = get_collector()


def _bootstrap_secret_key():
    """Se la secret_key (firma dei cookie di sessione) e' assente o quella di
    default, ne genera una casuale e la persiste in secrets.env: mai una chiave
    hardcoded in produzione. Aggiorna anche il valore in memoria (auth live)."""
    import secrets as _secrets
    from services.secrets_store import get_secrets_store
    if settings.secret_key and settings.secret_key != "change-me-in-production":
        return
    key = get_secrets_store().ensure("LAN_SECRET_KEY",
                                     lambda: _secrets.token_urlsafe(48))
    settings.secret_key = key
    log.info("secret_key generata/persistita (firma sessioni)")


def _log_security_warnings():
    """Rende rumorosa una configurazione che lascia le API senza login: un
    servizio aperto deve saltare all'occhio nei log, non restare implicito."""
    for reason in security_warnings():
        log.warning(f"SICUREZZA: {reason}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_secret_key()
    _log_security_warnings()
    log.info("Starting background data collector...")
    task = asyncio.create_task(collector.run())
    yield
    log.info("Shutting down collector...")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Chiude i client Docker (httpx) per igiene del lifecycle.
    try:
        from services.docker_client import get_docker_manager
        await get_docker_manager().aclose()
    except Exception as e:
        log.debug(f"docker manager close: {e}")
    # Chiude lo storico: con il WAL una chiusura pulita ne riversa il contenuto
    # nel database, cosi' l'ultimo tratto di storia non resta nel file laterale.
    try:
        from services.history_store import get_history
        get_history().close()
    except Exception as e:
        log.debug(f"chiusura storico: {e}")


def _read_version() -> str:
    """Versione dal file VERSION (copiato nell'immagine), cosi' /health riflette
    il tag realmente deployato invece di un valore hardcoded."""
    for p in ("/app/VERSION", str(Path(__file__).resolve().parent.parent / "VERSION")):
        try:
            v = Path(p).read_text().strip()
            if v:
                return v
        except OSError:
            continue
    return "unknown"


app = FastAPI(
    title="LANMng API",
    version=_read_version(),
    description="Management & monitoring della LAN domestica",
    lifespan=lifespan,
)

# ── Errori non gestiti ─────────────────────────────────────────────
# Senza questi due handler un'eccezione non prevista usciva come
# "Internal Server Error" in text/plain dal ServerErrorMiddleware: nessuna
# riga di log nostra, nessuno stack, e un corpo che il frontend non poteva
# nemmeno interpretare. Girano sul livello piu' esterno dello stack ASGI,
# quindi fuori dal CORS: con `cors.allowed_origins` vuoto (il caso normale)
# non cambia nulla, ma un 500 verso un'origine dichiarata arriverebbe senza
# header CORS.


@app.exception_handler(Exception)
async def errore_non_gestito(request: Request, exc: Exception):
    # Il riferimento lega la riga di log al messaggio mostrato in dashboard:
    # senza, "errore interno" non e' rintracciabile in `docker logs`.
    rid = uuid.uuid4().hex[:8]
    log.error(f"[{rid}] non gestita: {request.method} {request.url.path}", exc_info=True)
    corpo = {"detail": f"errore interno del backend (riferimento {rid})", "request_id": rid}
    if settings.debug:            # in esercizio niente dettagli interni al client
        corpo["exception"] = f"{type(exc).__name__}: {exc_text(exc)}"
    return JSONResponse(corpo, status_code=500)


@app.exception_handler(RequestValidationError)
async def errore_validazione(request: Request, exc: RequestValidationError):
    """Il 422 di Pydantic rompe la convenzione: `detail` e' una lista di
    oggetti invece di una frase, e chi la interpola stampa "[object Object]".
    Qui torna una frase; la lista grezza resta in `errors` per il debug."""
    parti = []
    for e in exc.errors():
        campo = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        parti.append(f"{campo}: {e.get('msg')}" if campo else str(e.get("msg")))
    return JSONResponse(
        {"detail": "; ".join(parti) or "dati non validi",
         "errors": jsonable_encoder(exc.errors())},
        status_code=422,
    )


# Auth (login admin opzionale): protegge /api/* se attiva. Aggiunta PRIMA del
# CORS cosi' CORS resta lo strato piu' esterno (preflight gestito per primo).
app.add_middleware(AuthMiddleware)

# CORS: montato solo se sono state dichiarate origini esterne. Di norma la lista
# e' vuota — l'unico client e' la SPA servita dallo stesso host, e una richiesta
# same-origin non passa da CORS — quindi non si risponde con nessun header CORS
# e nessun altro sito puo' chiamare le API con le credenziali dell'utente.
if settings.cors.allowed_origins:
    log.info(f"CORS abilitato per: {', '.join(settings.cors.allowed_origins)}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ── Router API ─────────────────────────────────────────────────────
app.include_router(system.router,     prefix="/api/system",    tags=["system"])
app.include_router(alerts.router,     prefix="/api/alerts",    tags=["alerts"])
app.include_router(devices.router,    prefix="/api/devices",   tags=["devices"])
app.include_router(wireguard.router,  prefix="/api/wireguard", tags=["wireguard"])
app.include_router(docker_svc.router, prefix="/api/docker",    tags=["docker"])
app.include_router(services.router,   prefix="/api/services",  tags=["services"])
app.include_router(wan.router,        prefix="/api/wan",       tags=["wan"])
app.include_router(logs.router,       prefix="/api/logs",      tags=["logs"])
app.include_router(config_api.router, prefix="/api/config",    tags=["config"])
app.include_router(auth.router,       prefix="/api/auth",      tags=["auth"])
app.include_router(host.router,       prefix="/api/host",      tags=["host"])
app.include_router(history.router,    prefix="/api/history",   tags=["history"])
app.include_router(tools.router,      prefix="/api/tools",     tags=["tools"])
app.include_router(terminal.router,   prefix="/api/terminal",  tags=["terminal"])
# Il WebSocket del terminale sta sotto /ws: nginx fa l'upgrade solo li'.
app.include_router(terminal.ws_router, prefix="/ws",           tags=["terminal"])


# ── WebSocket: update live ─────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info(f"WS connesso ({len(self.active)} totali)")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info(f"WS disconnesso ({len(self.active)} rimasti)")

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(data, default=str))
            except Exception as e:
                # Un client che se ne va e' normale: livello debug, ma non muto.
                log.debug(f"WS caduto in broadcast: {exc_text(e)}")
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
collector.on_update = manager.broadcast


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Stessa difesa del terminale: un WebSocket aperto da una pagina di un altro
    # host porterebbe comunque i cookie, e qui non arrivano ne' CORS ne' middleware.
    if not same_origin(ws):
        # La reason distingue i due rifiuti: con lo stesso 1008 muto il client
        # ritentava ogni due secondi anche quando insistere era inutile.
        await ws.close(code=1008, reason="origine")
        return
    # Se l'auth e' attiva, lo stream live richiede una sessione valida
    # (o bypass LAN): chiude prima di accettare se non autorizzato.
    if auth_enabled():
        client_ok = settings.auth.bypass_lan and ws.client and is_lan(ws.client.host)
        if not client_ok and not valid_token(ws.cookies.get(COOKIE, "")):
            await ws.close(code=1008, reason="sessione")
            return
    await manager.connect(ws)
    try:
        snapshot = await collector.get_snapshot()
        await ws.send_text(json.dumps({"type": "snapshot", "data": snapshot}, default=str))
        while True:
            await ws.receive_text()   # keepalive dal client
    except WebSocketDisconnect:
        pass
    except Exception:
        # Se lo snapshot solleva, il socket restava in `active` per sempre e
        # ogni broadcast successivo ci sbatteva contro.
        log.exception("WS: errore nella sessione")
    finally:
        manager.disconnect(ws)


# ── Endpoint comodi ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/snapshot")
async def snapshot():
    """Snapshot completo corrente (comodo per debug e load iniziale)."""
    return await collector.get_snapshot()


# ── Frontend statico ───────────────────────────────────────────────
# Montato per ULTIMO su "/" cosi' le route /api, /ws, /health hanno la
# precedenza; serve index.html sulla root e gli asset (app.js, styles.css)
# con path relativi (funziona identico anche dietro nginx).
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if (FRONTEND / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
