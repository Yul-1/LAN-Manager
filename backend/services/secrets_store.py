"""
services/secrets_store.py — Gestione dei segreti in config/secrets.env
=====================================================================
I segreti (password) NON stanno in config.yaml ma in un file separato
stile .env, con permessi restrittivi (600), che il backend legge come
variabili d'ambiente (priorita' sopra config.yaml).

La UI puo' impostarli ma NON puo' rileggerli: l'API espone solo lo stato
"impostato / non impostato", mai il valore.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("secrets-store")

# Campi gestiti: id usato dalla UI -> variabile d'ambiente (convenzione LAN_*).
SECRET_FIELDS = [
    {"id": "router_password",      "env": "LAN_ROUTER__PASSWORD",
     "label": "Password SSH router"},
    {"id": "router_luci_password", "env": "LAN_ROUTER__LUCI_PASSWORD",
     "label": "Password LuCI router"},
    {"id": "admin_password_hash",  "env": "LAN_AUTH__PASSWORD_HASH",
     "label": "Hash password admin (login)"},
]
_BY_ID = {f["id"]: f for f in SECRET_FIELDS}


def _env_path() -> Path:
    return Path(os.environ.get("LAN_ENV_FILE", _default_path()))


def _default_path() -> str:
    base = "/app/config" if Path("/app/config").exists() else str(
        Path(__file__).resolve().parent.parent.parent / "config")
    return f"{base}/secrets.env"


class SecretsStore:

    def __init__(self):
        self.path = _env_path()

    # ── Lettura (solo per uso interno: parsing KEY=VALUE) ──────────

    def _read(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if not self.path.exists():
            return out
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = _dotenv_unquote(v.strip())
        return out

    def value(self, env_key: str) -> str:
        """Valore corrente di un segreto per uso INTERNO (mai esposto via API):
        priorita' env di processo, poi secrets.env. Serve all'auth per leggere
        l'hash password aggiornato senza riavvio."""
        return os.environ.get(env_key) or self._read().get(env_key, "")

    # ── Stato per la UI (mai i valori) ─────────────────────────────

    def status(self) -> list[dict]:
        current = self._read()
        return [
            {"id": f["id"], "label": f["label"], "set": bool(current.get(f["env"]))}
            for f in SECRET_FIELDS
        ]

    # ── Scrittura ──────────────────────────────────────────────────

    def update(self, values: dict[str, str]) -> dict:
        """
        values: {id: nuovo_valore}. Un valore vuoto/assente lascia invariato;
        il valore "__CLEAR__" rimuove il segreto. Ritorna lo stato aggiornato.
        """
        current = self._read()
        changed = []
        for fid, val in values.items():
            field = _BY_ID.get(fid)
            if field is None or val is None:
                continue
            env = field["env"]
            if val == "__CLEAR__":
                current.pop(env, None)
                changed.append(fid)
            elif val != "":
                current[env] = val
                changed.append(fid)
        if changed:
            self._write(current)
        return {"ok": True, "changed": changed, "restart_required": True}

    def ensure(self, env_key: str, factory) -> str:
        """Ritorna il valore di `env_key`; se assente (sia in env sia nel file),
        lo genera con `factory()`, lo persiste in secrets.env (0600) e lo rende
        subito visibile nel processo. Idempotente. Serve al bootstrap della
        secret_key: nessun segreto di default hardcoded in produzione."""
        existing = self.value(env_key)
        if existing:
            return existing
        val = factory()
        current = self._read()
        current[env_key] = val
        self._write(current)
        os.environ[env_key] = val
        log.info(f"{env_key} generato e persistito in secrets.env")
        return val

    def _write(self, data: dict[str, str]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = ("# secrets.env — segreti LANMng (NON su git).\n"
                  "# Gestito dalla pagina Impostazioni. Permessi 600.\n"
                  "# Variabili LAN_* che sovrascrivono config.yaml.\n\n")
        # Quoting double-quoted compatibile con python-dotenv: gestisce
        # spazi, '#', '$', virgolette e newline senza rompere il parser.
        body = "".join(f"{k}={_dotenv_quote(v)}\n" for k, v in sorted(data.items()))
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(header + body)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            log.info(f"secrets.env aggiornato ({len(data)} segreti)")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# ── Helper di quoting per il formato python-dotenv ────────────────
# Usiamo double-quote con backslash-escape: e' la forma piu' tollerante
# (gestisce spazi, '#', '$', newline). Compatibile con python-dotenv usato
# internamente da pydantic-settings per leggere il file `secrets.env`.

def _dotenv_quote(v: str) -> str:
    """Quota un valore per `secrets.env`. Stringa vuota -> coppia di quote."""
    if v is None:
        return '""'
    escaped = (
        v.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _dotenv_unquote(v: str) -> str:
    """Inverso di _dotenv_quote. Tollera valori non quotati (compatibilita')."""
    if not v:
        return ""
    # Single-quoted: nessuna interpretazione interna.
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1]
    # Double-quoted: gestisce gli escape \\, \", \n, \r.
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        out, i = [], 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                nx = inner[i + 1]
                out.append({"n": "\n", "r": "\r", '"': '"', "\\": "\\"}.get(nx, nx))
                i += 2
            else:
                out.append(inner[i])
                i += 1
        return "".join(out)
    return v


_store: SecretsStore | None = None


def get_secrets_store() -> SecretsStore:
    global _store
    if _store is None:
        _store = SecretsStore()
    return _store
