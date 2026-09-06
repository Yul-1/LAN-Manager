"""
middleware/auth.py — Login admin singolo (LAN-only)
===================================================
Autenticazione opzionale (config `auth.method`: none | basic). Quando
attiva, le rotte `/api/*` (tranne `/api/auth/*` e `/health`) richiedono
una sessione valida; il frontend statico resta servibile per mostrare la
pagina di login.

Sessione = cookie httponly firmato con HMAC-SHA256 su `secret_key`
(nessuna dipendenza esterna oltre a bcrypt per la verifica password).
La password admin e' uno **hash bcrypt** salvato nei segreti
(`LAN_AUTH__PASSWORD_HASH`), mai in chiaro.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import time
from urllib.parse import urlparse

import bcrypt
from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import settings
from services.i18n import t
from services.secrets_store import get_secrets_store

log = logging.getLogger("auth")

COOKIE = "lanmng_session"
TTL = 7 * 24 * 3600          # durata sessione: 7 giorni
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def same_origin(request) -> bool:
    """True se la richiesta e' same-origin (o priva di Origin, cioe' non-browser).
    Difesa CSRF: una pagina esterna non puo' far combaciare l'Origin con l'Host
    che nginx preserva (`proxy_set_header Host $host`).

    Accetta qualsiasi oggetto con `.headers`: vale anche per le connessioni
    WebSocket, che portano i cookie ma non passano ne' dalla CORS policy ne' dal
    middleware, e dove quindi questo controllo va fatto a mano."""
    origin = request.headers.get("origin")
    if not origin:
        return True   # niente Origin: client non-browser o same-origin -> non e' un vettore CSRF
    try:
        origin_host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    # Confronto per hostname (ignora la porta): nginx forwarda `Host` via $host
    # senza porta, mentre l'Origin la include. Sufficiente contro CSRF cross-site.
    req_host = request.headers.get("host", "").split(":")[0].lower()
    return bool(origin_host) and origin_host == req_host


def auth_enabled() -> bool:
    return (settings.auth.method or "none") not in ("none", "")


def security_warnings() -> list[str]:
    """Motivi per cui, con questa configurazione, l'API e' di fatto aperta.

    Il pentest 2026-08-17 ha trovato il servizio senza login a un mese dal
    report precedente: lo stato insicuro non era visibile da nessuna parte.
    Questa lista viene loggata all'avvio e mostrata come banner in dashboard,
    cosi' una config permissiva non passa piu' inosservata.
    """
    reasons: list[str] = []
    if not auth_enabled():
        reasons.append(t("sicurezza.authNone"))
    elif settings.auth.bypass_lan:
        reasons.append(t("sicurezza.bypassLan"))
    if auth_enabled() and not password_set():
        reasons.append(t("sicurezza.nessunaPassword"))
    return reasons


def admin_hash() -> str:
    """Hash bcrypt corrente (live): env > secrets.env > config.yaml."""
    return get_secrets_store().value("LAN_AUTH__PASSWORD_HASH") or (settings.auth.password_hash or "")


def password_set() -> bool:
    return bool(admin_hash())


def verify_password(password: str) -> bool:
    h = admin_hash()
    if not h or not password:
        return False
    try:
        return bcrypt.checkpw(password.encode(), h.encode())
    except (ValueError, TypeError):
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


# ── Token di sessione (HMAC) ───────────────────────────────────────

def _sign(msg: str) -> str:
    sig = hmac.new(settings.secret_key.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def make_token(username: str) -> str:
    msg = f"{username}.{int(time.time()) + TTL}"
    return f"{msg}.{_sign(msg)}"


def valid_token(token: str) -> bool:
    try:
        username, exp, sig = token.rsplit(".", 2)
    except (ValueError, AttributeError):
        return False
    if not hmac.compare_digest(sig, _sign(f"{username}.{exp}")):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def is_lan(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def is_authenticated(request: Request) -> bool:
    """True se la richiesta e' autorizzata (sessione valida o bypass LAN)."""
    if not auth_enabled():
        return True
    if settings.auth.bypass_lan and request.client and is_lan(request.client.host):
        return True
    return valid_token(request.cookies.get(COOKIE, ""))


def session_valid(cookies) -> bool:
    """Sessione valida in senso stretto: solo cookie firmato, nessuna scorciatoia.

    Usata dalle superfici sensibili (tool di rete, terminale SSH) e dal
    WebSocket del terminale, che riceve i cookie ma non passa dal middleware.
    """
    return valid_token((cookies or {}).get(COOKIE, ""))


def require_session(request: Request) -> None:
    """Dependency per le rotte sensibili: sessione **sempre** richiesta.

    A differenza del resto delle API, qui non valgono ne' `auth.method: none`
    ne' `auth.bypass_lan`: dare a chiunque sia in LAN una shell SSH sul router o
    l'esecuzione di comandi di rete non e' la stessa cosa che mostrargli lo
    stato dei dispositivi: e' una regola di progetto.
    """
    if not password_set():
        raise HTTPException(
            status_code=503,
            detail=t("err.servePasswordAdmin"))
    if not session_valid(request.cookies):
        raise HTTPException(status_code=401, detail=t("err.sessioneRichiesta"))


class AuthMiddleware(BaseHTTPMiddleware):
    """Protegge le rotte `/api/*` (escluse auth/health). Lascia liberi gli
    asset statici cosi' la SPA puo' mostrare la pagina di login."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CSRF: le mutazioni via /api/ devono essere same-origin. Vale anche con
        # auth disabilitata (difesa in profondita' per il servizio "no-auth").
        if (request.method in _MUTATING and path.startswith("/api/")
                and not same_origin(request)):
            return JSONResponse({"detail": t("err.origineNonConsentita")}, status_code=403)
        # `/api/setup/` resta fuori dall'auth perche' il primo avvio avviene
        # quando una password admin non esiste ancora — stessa finestra in cui
        # `/api/auth/password` accetta il bootstrap. E' sicuro solo perche' quel
        # router si spegne da solo (404) appena config.yaml esiste, e non puo'
        # sovrascrivere una configurazione gia' presente.
        if (request.method == "OPTIONS"
                or not auth_enabled()
                or not path.startswith("/api/")
                or path.startswith("/api/auth/")
                or path == "/api/setup" or path.startswith("/api/setup/")
                or path == "/health"):
            return await call_next(request)
        if is_authenticated(request):
            return await call_next(request)
        return JSONResponse({"detail": t("err.nonAutenticato")}, status_code=401)
