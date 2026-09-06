#!/usr/bin/env python3
"""
smoke.py — Verifica ripetibile di un'istanza LANMng gia' deployata.

Sostituisce gli script usa-e-getta usati in origine. Va lanciato dopo
ogni deploy: dice in una schermata se l'immagine giusta e' in esercizio, se il
login e' ancora obbligatorio e se ogni gruppo di endpoint risponde.

Uso:
    export LANMNG_PASS='...'                 # mai sulla riga di comando
    backend/.venv/bin/python scripts/smoke.py --base http://<host>:81 \
        --expect-version "$(cat VERSION)"

Variabili d'ambiente:
    LANMNG_URL   indirizzo base (sovrascritto da --base)
    LANMNG_USER  utente admin (default: admin)
    LANMNG_PASS  password admin

In alternativa: --password-file <percorso> (una riga, permessi 600), oppure la
password su stdin (`... < file` o `pass show ... | ...`). Se il terminale e'
interattivo e non e' stato indicato nulla, viene chiesta senza essere stampata.

Esce 0 se tutto passa, 1 al primo controllo fallito.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys

import httpx

ESITI: list[tuple[bool, str]] = []


def esito(ok: bool, titolo: str, dettaglio: str = "") -> bool:
    ESITI.append((ok, titolo))
    marchio = "OK  " if ok else "FAIL"
    print(f"[{marchio}] {titolo}" + (f" — {dettaglio}" if dettaglio else ""))
    return ok


def _password(percorso_file: str) -> str:
    """Password admin, senza mai passare dagli argomenti del processo (visibili
    a chiunque possa leggere `ps`)."""
    if percorso_file:
        return open(percorso_file, encoding="utf-8").read().strip()
    pw = os.environ.get("LANMNG_PASS", "")
    if pw:
        return pw
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()          # password su stdin
    return getpass.getpass("Password admin (non viene stampata): ")


async def _websocket(base: str, cookie: str, timeout: float) -> tuple[bool, str]:
    """Primo frame dello stream live. E' l'unico controllo che copre il
    collector e il lifespan, che i test di integrazione non toccano."""
    try:
        import websockets
    except ImportError:
        return False, "modulo websockets non installato"
    url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    origine = base.rstrip("/")
    try:
        async with websockets.connect(
                url, additional_headers={"Cookie": cookie, "Origin": origine},
                open_timeout=timeout, close_timeout=2) as ws:
            messaggio = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if messaggio.get("type") != "snapshot":
        return False, f"primo frame inatteso: {str(messaggio)[:80]}"
    device = messaggio.get("data", {}).get("devices", [])
    return True, f"snapshot con {len(device)} dispositivi"


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke test di un'istanza LANMng")
    ap.add_argument("--base", default=os.environ.get("LANMNG_URL", "http://localhost:81"))
    ap.add_argument("--expect-version", default="", help="versione attesa da /health")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--no-ws", action="store_true", help="salta il controllo WebSocket")
    ap.add_argument("--password-file", default="",
                    help="file contenente la password admin (una riga)")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    utente = os.environ.get("LANMNG_USER", "admin")
    print(f"LANMng smoke — {base}\n")

    c = httpx.Client(base_url=base, timeout=args.timeout, follow_redirects=False)

    # 1. Versione realmente in esercizio.
    try:
        r = c.get("/health")
        corpo = r.json()
    except Exception as e:
        esito(False, "/health raggiungibile", f"{type(e).__name__}: {e}")
        return 1
    if not esito(r.status_code == 200 and corpo.get("status") == "ok",
                 "/health risponde", f"versione {corpo.get('version')}"):
        return 1
    if args.expect_version:
        if not esito(corpo.get("version") == args.expect_version,
                     f"versione in esercizio = {args.expect_version}",
                     f"trovata {corpo.get('version')}"):
            return 1

    # 2. Lo stato dell'auth: un deploy non deve aver riaperto le API.
    stato = c.get("/api/auth/status").json()
    if not esito(stato.get("auth_required") is True, "il login e' richiesto"):
        return 1
    if not esito(not stato.get("insecure"), "nessun avviso di configurazione",
                 "; ".join(stato.get("insecure") or [])):
        return 1

    # 3. Senza credenziali le API tacciono (remediation del pentest 2026-08-17).
    for rotta in ("/api/devices/", "/api/snapshot", "/api/config/"):
        codice = c.get(rotta).status_code
        if not esito(codice == 401, f"{rotta} senza cookie e' 401", f"risponde {codice}"):
            return 1

    # 4. Login.
    try:
        password = _password(args.password_file)
    except OSError as e:
        esito(False, "lettura della password", f"{type(e).__name__}: {e}")
        return 1
    if not password:
        esito(False, "password admin non fornita: controlli autenticati saltati",
              "usa LANMNG_PASS, --password-file, oppure passala su stdin")
        return 1
    r = c.post("/api/auth/login", json={"username": utente, "password": password},
               headers={"Origin": base})
    if not esito(r.status_code == 200, "login", f"risponde {r.status_code}"):
        return 1
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())

    # 5. Un endpoint per gruppo.
    for rotta, chiave in [
        ("/api/system/info", "hostname"),
        ("/api/devices/", "devices"),
        ("/api/services/", None),
        ("/api/docker/containers", None),
        ("/api/wan/status", None),
        ("/api/logs/", "lines"),
        ("/api/wireguard/status", None),
        ("/api/config/", None),
        ("/api/host/network", None),
        ("/api/host/resources", None),
        ("/api/snapshot", "meta"),
    ]:
        try:
            r = c.get(rotta)
            dati = r.json()
        except Exception as e:
            esito(False, f"GET {rotta}", f"{type(e).__name__}: {e}")
            return 1
        ok = r.status_code == 200 and (chiave is None or chiave in dati)
        misura = f"{len(dati[chiave])} elementi" if chiave and isinstance(
            dati.get(chiave), list) else f"{r.status_code}"
        if not esito(ok, f"GET {rotta}", misura):
            return 1

    # 6. Difesa CSRF ancora in piedi sulle scritture.
    codice = c.put("/api/config/", json={}, headers={"Origin": "http://attaccante.example"}
                   ).status_code
    if not esito(codice == 403, "origine estranea respinta sulle scritture",
                 f"risponde {codice}"):
        return 1

    # 7. Stream live.
    if not args.no_ws:
        ok, dettaglio = asyncio.run(_websocket(base, cookie, args.timeout))
        if not esito(ok, "WebSocket /ws", dettaglio):
            return 1

    # 8. Frontend servito e non memorizzato in cache dal browser.
    r = c.get("/")
    versione = corpo.get("version", "")
    ok = r.status_code == 200 and f"?v={versione}" in r.text
    if not esito(ok, "frontend servito con gli asset versionati",
                 f"atteso ?v={versione}"):
        return 1

    c.post("/api/auth/logout", headers={"Origin": base})

    falliti = [t for ok, t in ESITI if not ok]
    print(f"\n{len(ESITI) - len(falliti)}/{len(ESITI)} controlli passati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
