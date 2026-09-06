"""
services/config_store.py — Lettura/scrittura di config.yaml dalla UI
====================================================================
Permette di modificare la configurazione dalla dashboard:
  - lettura con i SEGRETI mascherati (password/hash) -> il browser non li vede
  - salvataggio con merge dei segreti (se lasci la maschera, resta il valore
    attuale), validazione (lo schema deve essere valido) e backup automatico

I segreti sono individuati per nome chiave (ovunque nell'albero), vedi
SECRET_KEYS. Le modifiche richiedono il riavvio del servizio per essere
applicate (la config viene caricata all'avvio).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

from config import Settings, settings

log = logging.getLogger("config-store")

# Chiavi il cui valore non va mai mostrato in chiaro nella UI.
SECRET_KEYS = {"password", "luci_password", "password_hash"}
MASK = "********"

# Campi che identificano un elemento di lista (es. un host), per ripristinare i
# segreti mascherati dall'elemento GIUSTO invece che per posizione.
_IDENTITY_KEYS = ("id", "name", "host", "ip", "local_name")


def _item_identity(item):
    if isinstance(item, dict):
        for k in _IDENTITY_KEYS:
            v = item.get(k)
            if v not in (None, "", MASK):
                return (k, v)
    return None


def _config_path() -> Path:
    return Path(os.environ.get("LAN_CONFIG_FILE", settings_config_default()))


def settings_config_default() -> str:
    # Stessa logica di config.py: /app/config oppure config/ del repo.
    base = "/app/config" if Path("/app/config").exists() else str(
        Path(__file__).resolve().parent.parent.parent / "config")
    return f"{base}/config.yaml"


def _redact(node):
    """Sostituisce ricorsivamente i valori segreti con la maschera."""
    if isinstance(node, dict):
        return {k: (MASK if (k in SECRET_KEYS and v not in (None, "")) else _redact(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(x) for x in node]
    return node


def _merge_secrets(new, original):
    """Dove il nuovo valore e' la maschera, ripristina dall'originale."""
    if isinstance(new, dict):
        out = {}
        orig = original if isinstance(original, dict) else {}
        for k, v in new.items():
            if k in SECRET_KEYS and v == MASK:
                out[k] = orig.get(k)
            else:
                out[k] = _merge_secrets(v, orig.get(k))
        return out
    if isinstance(new, list):
        orig = original if isinstance(original, list) else []
        # Ripristina i segreti mascherati matchando gli elementi per identita'
        # (es. host/ip), NON per posizione: cosi' un riordino/inserimento non fa
        # ereditare la password di un host a un altro. Fallback all'indice solo
        # per elementi privi di identita'.
        orig_by_id = {}
        for it in orig:
            ident = _item_identity(it)
            if ident is not None:
                orig_by_id[ident] = it
        out = []
        for i, v in enumerate(new):
            ident = _item_identity(v)
            if ident is not None:
                match = orig_by_id.get(ident)
                if match is None and _has_masked_secret(v):
                    log.warning(
                        "merge segreti: nessun elemento originale per %s, "
                        "il segreto mascherato non verra' ripristinato", ident)
            else:
                match = orig[i] if i < len(orig) else None
            out.append(_merge_secrets(v, match))
        return out
    return new


def _has_masked_secret(item) -> bool:
    """True se l'elemento contiene (a qualsiasi livello) un segreto mascherato."""
    if isinstance(item, dict):
        return any((k in SECRET_KEYS and v == MASK) or _has_masked_secret(v)
                   for k, v in item.items())
    if isinstance(item, list):
        return any(_has_masked_secret(x) for x in item)
    return False


# Sezioni di config.yaml editabili da form dedicati (chiave UI -> path puntato).
EDITABLE_SECTIONS = {
    "subnets": "subnets",
    "wg_peers": "wireguard.peer_names",
    "discovery_ssh": "discovery.ssh",
    "docker_hosts": "docker.hosts",
    "alerts_silenced": "alerts.silenced",
}

# Sezioni che il backend rilegge dal file mentre gira: salvarle NON richiede il
# riavvio del servizio, e dirlo lo stesso sarebbe una bugia in faccia all'utente
# (services/alerts.py:live_alerts_config).
LIVE_SECTIONS = {"alerts_silenced"}


def _dget(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _validate_section(name: str, value):
    """Controlli che lo schema di Settings non puo' fare: i tipi sono corretti,
    ma il valore non funzionerebbe sul campo. Riguarda i percorsi delle chiavi
    SSH, che devono esistere e essere leggibili nel container (senza questo,
    incollare il *contenuto* di una chiave viene accettato e la discovery - e
    Docker - falliscono su quell'host ad ogni ciclo), e il sistema dichiarato
    per ciascun host, che decide se mandargli comandi POSIX o PowerShell."""
    if name == "alerts_silenced":
        _validate_silenced(value)
        return
    if name != "discovery_ssh" or not isinstance(value, dict):
        return
    # Import differito: ssh_hosts rilegge la config da qui (evita il ciclo).
    from config import OS_HOST
    from services.ssh_hosts import validate_key_path

    # Solo la forma (contenuto incollato, percorso relativo): un file non ancora
    # copiato nella cartella montata non deve bloccare il salvataggio del resto.
    validate_key_path(value.get("default_key") or "", check_file=False)
    for host in (value.get("hosts") or []):
        if not isinstance(host, dict):
            continue
        if host.get("key"):
            try:
                validate_key_path(host["key"], check_file=False)
            except ValueError as e:
                raise ValueError(f"host {host.get('ip', '?')}: {e}")
        # Un `os` scritto male verrebbe scoperto solo al ciclo successivo, come
        # un host che non risponde: meglio rifiutare il salvataggio.
        if host.get("os") and str(host["os"]).strip().lower() not in OS_HOST:
            raise ValueError(f"host {host.get('ip', '?')}: os dev'essere uno fra "
                             f"{', '.join(OS_HOST)}, ricevuto {host['os']!r}")


def _validate_silenced(value):
    """Silenziamenti: nome di regola esistente e motivo scritto.

    Una regola inesistente verrebbe accettata dallo schema (e' solo una stringa)
    e non zittirebbe nulla, senza dirlo. Il motivo e' obbligatorio perche' fra
    sei mesi si deve poter capire perche' quell'avviso non suona piu'.
    """
    from services.alerts import CATALOGO      # import qui: alerts rilegge la config

    if not isinstance(value, list):
        raise ValueError("i silenziamenti devono essere un elenco")
    for i, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"silenziamento {i}: deve essere un oggetto")
        rule = (item.get("rule") or "").strip()
        if rule not in CATALOGO:
            valide = ", ".join(sorted(CATALOGO))
            raise ValueError(
                f"silenziamento {i}: regola sconosciuta '{rule}'. "
                f"Quelle valide sono: {valide}")
        if not (item.get("reason") or "").strip():
            raise ValueError(
                f"silenziamento {i} ({rule}): scrivi il motivo, serve a ricordare "
                f"fra sei mesi perche' questo avviso non suona piu'")


def _dset(d: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# Contatore dei salvataggi. Chi tiene in cache una sezione riletta dal file
# (services/alerts.py) non puo' fidarsi del solo mtime: due scritture dentro la
# stessa tacca dell'orologio del filesystem sono indistinguibili, e la seconda
# resterebbe invisibile fino al riavvio. Qui il salvataggio si dichiara.
_generazione = 0


def generazione() -> int:
    return _generazione


class ConfigStore:

    def __init__(self):
        self.path = _config_path()

    # ── Sezioni strutturate (editor dedicati) ──────────────────────

    def read_section(self, name: str):
        """Valore (redatto) di una sezione editabile; solleva KeyError se ignota."""
        dotted = EDITABLE_SECTIONS[name]
        return _redact(_dget(self._load_raw(), dotted))

    def save_section(self, name: str, value) -> dict:
        """Salva una sola sezione, ripristinando i segreti mascherati, validando
        l'intero schema e facendo backup. `restart_required` e' falso per le
        sezioni che il backend rilegge da solo (LIVE_SECTIONS)."""
        dotted = EDITABLE_SECTIONS[name]
        raw = self._load_raw()
        merged_value = _merge_secrets(value, _dget(raw, dotted))
        _validate_section(name, merged_value)
        _dset(raw, dotted, merged_value)
        try:
            Settings(**raw)
        except (ValidationError, ValueError, TypeError) as e:
            # Solo gli errori dello schema diventano "colpa dell'utente": prima
            # `except Exception` trasformava qualunque bug interno in un 400
            # che accusava chi stava salvando.
            raise ValueError(f"Configurazione non valida: {e}")
        self._write_with_backup(raw)
        return {"ok": True, "restart_required": name not in LIVE_SECTIONS}

    # ── Lettura ────────────────────────────────────────────────────

    def _load_raw(self) -> dict:
        src = self.path
        if not src.exists():
            example = src.with_name(src.stem + ".example" + src.suffix)
            src = example if example.exists() else None
        if src and src.exists():
            with open(src) as f:
                return yaml.safe_load(f) or {}
        return {}

    def read_redacted_yaml(self) -> str:
        data = _redact(self._load_raw())
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

    # ── Scrittura ──────────────────────────────────────────────────

    def save_yaml(self, text: str) -> dict:
        """
        Valida e salva. Ritorna {ok, restart_required} oppure solleva
        ValueError con un messaggio leggibile (YAML invalido o schema errato).
        """
        try:
            new_data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML non valido: {e}")
        if not isinstance(new_data, dict):
            raise ValueError("La configurazione deve essere un oggetto YAML (chiave: valore).")

        merged = _merge_secrets(new_data, self._load_raw())

        # Validazione schema: deve costruire un oggetto Settings senza errori.
        try:
            Settings(**merged)
        except (ValidationError, ValueError, TypeError) as e:
            raise ValueError(f"Configurazione non valida: {e}")

        self._write_with_backup(merged)
        return {"ok": True, "restart_required": True}

    def _write_with_backup(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.path, self.path.with_name(f"config.{stamp}.bak.yaml"))
        header = ("# config.yaml — configurazione LANMng\n"
                  "# Modificabile dalla UI (Impostazioni). Backup automatici: config.*.bak.yaml\n"
                  "# Le modifiche richiedono il riavvio del servizio.\n\n")
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(header)
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, self.path)
            global _generazione
            _generazione += 1
            log.info("config.yaml salvato")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ── Backup ─────────────────────────────────────────────────────

    def list_backups(self) -> list[dict]:
        out = []
        for p in sorted(self.path.parent.glob("config.*.bak.yaml"), reverse=True):
            out.append({"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
        return out

    def restore_backup(self, name: str) -> dict:
        # Sicurezza: solo file di backup nella cartella config, niente path traversal.
        if "/" in name or "\\" in name or not name.startswith("config.") or not name.endswith(".bak.yaml"):
            raise ValueError("Nome backup non valido.")
        bak = self.path.parent / name
        if not bak.exists():
            raise ValueError("Backup non trovato.")
        with open(bak) as f:
            data = yaml.safe_load(f) or {}
        self._write_with_backup(data)
        return {"ok": True, "restored": name, "restart_required": True}


_store: ConfigStore | None = None


def get_config_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store
