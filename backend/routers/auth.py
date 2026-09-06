"""routers/auth.py — Login admin: stato, login, logout, set password."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from config import settings
from middleware.auth import (
    COOKIE, TTL, auth_enabled, hash_password, is_authenticated, is_lan,
    make_token, password_set, security_warnings, session_valid, verify_password,
)
from services.ratelimit import RateLimiter
from services.secrets_store import get_secrets_store

router = APIRouter()

# Anti brute-force sul login: max 5 tentativi FALLITI per IP ogni 5 minuti.
_login_rl = RateLimiter(max_hits=5, window_seconds=300)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class LoginBody(BaseModel):
    username: str = ""
    password: str


class PasswordBody(BaseModel):
    password: str


@router.get("/status")
async def auth_status(request: Request):
    """Indica se serve il login e se la richiesta e' gia' autorizzata."""
    return {
        "auth_required": auth_enabled(),
        "authenticated": is_authenticated(request),
        # Sessione vera (cookie firmato): le pagine sensibili la richiedono
        # anche quando l'auth globale e' spenta, dove "authenticated" e' sempre True.
        "session": session_valid(request.cookies),
        "username": settings.auth.username,
        "password_set": password_set(),
        # Motivi per cui l'API e' raggiungibile senza login: la dashboard li
        # mostra come banner. Solo descrizioni, nessun valore di configurazione.
        "insecure": security_warnings(),
    }


@router.post("/login")
async def login(body: LoginBody, response: Response, request: Request):
    ip = _client_ip(request)
    if not _login_rl.allowed(ip):
        raise HTTPException(status_code=429, detail="troppi tentativi, riprova piu' tardi",
                            headers={"Retry-After": str(_login_rl.retry_after(ip))})
    if (body.username and body.username != settings.auth.username) \
            or not verify_password(body.password):
        _login_rl.hit(ip)
        raise HTTPException(status_code=401, detail="credenziali non valide")
    _login_rl.reset(ip)
    response.set_cookie(COOKIE, make_token(settings.auth.username),
                        httponly=True, samesite="lax", max_age=TTL)
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.post("/password")
async def set_password(body: PasswordBody, request: Request):
    """Imposta/cambia l'hash della password admin (scritto nei segreti).

    Consentito se gia' autenticati, oppure in bootstrap (nessuna password
    ancora impostata) da una richiesta proveniente dalla LAN."""
    bootstrap = (not password_set()) and request.client and is_lan(request.client.host)
    if not is_authenticated(request) and not bootstrap:
        raise HTTPException(status_code=401, detail="non autorizzato")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="password troppo corta (minimo 6 caratteri)")
    get_secrets_store().update({"admin_password_hash": hash_password(body.password)})
    return {"ok": True, "restart_required": False}
