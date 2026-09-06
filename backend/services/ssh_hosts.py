"""
services/ssh_hosts.py — Risoluzione dei parametri SSH per host
==============================================================
Dato l'identificativo di un host (IP/hostname), ritorna i kwargs di
`asyncssh.connect`. Usato per monitorare servizi (systemd) su host diversi:
cerca l'host fra quelli noti alla discovery SSH (`discovery.ssh.hosts`) e usa
le sue credenziali, con fallback ai default della discovery; host_id vuoto =
host systemd di default (`settings.systemd`).

Qui vive anche l'altra meta' della domanda "come si parla a questo host": quale
**sistema** risponde dall'altra parte (`risolvi_os`) e come consegnargli uno
script PowerShell quando e' Windows (`comando_powershell`, `esegui_powershell`). Sta con le
credenziali e non nei singoli connettori perche' risorse, facts della discovery
e servizi Windows farebbero altrimenti tre copie della stessa sonda.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from config import SSHDiscovery, settings

log = logging.getLogger("ssh-hosts")

# Inizi tipici del *contenuto* di una chiave: il campo vuole un percorso, ma la
# cosa piu' naturale da fare e' incollarci la chiave.
_KEY_CONTENT_PREFIXES = ("ssh-", "ecdsa-", "sk-", "-----BEGIN")


def validate_key_path(key: str, check_file: bool = True) -> str:
    """Controlla che `key` sia il percorso di un file di chiave leggibile.

    Unica fonte della regola per tutte le vie da cui una chiave puo' entrare in
    configurazione (pagina Terminale ed editor Impostazioni): senza questo
    controllo il valore sbagliato viene accettato e l'errore compare solo dopo,
    ad ogni ciclo, come `[Errno 2] No such file or directory: 'ssh-ed25519 ...'`.
    Solleva ValueError con un messaggio comprensibile; ritorna il percorso.

    Con `check_file=False` si fermano solo i valori sbagliati *nella forma*
    (contenuto incollato, percorso relativo). Serve quando si salva una sezione
    intera di configurazione: un file non ancora copiato nella cartella montata
    non deve impedire di salvare il resto.
    """
    key = (key or "").strip()
    if not key:
        return ""
    if key.startswith(_KEY_CONTENT_PREFIXES):
        raise ValueError(
            "hai incollato il contenuto della chiave: qui va il percorso del file dentro "
            "il container (es. /app/ssh/id_ed25519), oppure lascia vuoto per usare quella "
            "predefinita")
    if not key.startswith("/"):
        raise ValueError(f"il percorso della chiave dev'essere assoluto: {key}")
    if not check_file:
        return key
    path = Path(key)
    if not path.is_file():
        raise ValueError(
            f"chiave inesistente nel container: {key}. Copiala nella cartella montata su "
            f"/app/ssh e indicala come /app/ssh/<nome>")
    if not os.access(path, os.R_OK):
        raise ValueError(
            f"chiave non leggibile dal servizio: {key}. Deve appartenere all'uid 1000 "
            f"con permessi 600")
    return key

# Cache della sezione riletta dal file, invalidata sul mtime.
_cache: dict[str, Any] = {"mtime": 0.0, "cfg": None}

# Sistema operativo scoperto per IP dagli host dichiarati "auto". Si azzera
# insieme alla cache della configurazione: e' l'unico momento in cui la risposta
# giusta puo' essere cambiata.
_os_cache: dict[str, str] = {}


def live_ssh_config() -> SSHDiscovery:
    """Sezione `discovery.ssh` **riletta dal file**, non quella caricata all'avvio.

    Gli host SSH sono l'elenco da cui dipendono il terminale e i facts della
    discovery: se per usarne uno nuovo servisse riavviare il servizio, aggiungerlo
    dalla UI sarebbe un mezzo servizio. Il file resta l'unica fonte; in caso di
    file illeggibile o non valido si ricade sulla config di avvio.
    """
    from services.config_store import get_config_store      # import qui: evita il ciclo

    try:
        path = get_config_store().path
        mtime = path.stat().st_mtime
        if _cache["cfg"] is not None and mtime == _cache["mtime"]:
            return _cache["cfg"]
        raw = yaml.safe_load(path.read_text()) or {}
        section = ((raw.get("discovery") or {}).get("ssh")) or {}
        cfg = SSHDiscovery(**section)
        _cache.update(mtime=mtime, cfg=cfg)
        # Il file e' cambiato: un `os:` corretto a mano o dalle Impostazioni deve
        # valere dal ciclo successivo, non al prossimo riavvio del servizio.
        _os_cache.clear()
        return cfg
    except Exception as e:
        log.warning(f"discovery.ssh non rileggibile dal file ({e}): uso la config di avvio")
        return settings.discovery.ssh


def resolve_ssh_target(host_id: str = "") -> dict[str, Any]:
    host_id = (host_id or "").strip()

    # Il router ha credenziali proprie in config (utente/chiave/password) e non
    # sta nell'elenco della discovery: va riconosciuto prima degli altri.
    if host_id and host_id == (settings.router.host or "").strip():
        rt = settings.router
        kw: dict[str, Any] = {
            "host": rt.host, "port": rt.port,
            "username": rt.user or "root",
            "known_hosts": None,
        }
        if rt.ssh_key:
            kw["client_keys"] = [rt.ssh_key]
        elif rt.password:
            kw["password"] = rt.password
        return kw

    # Host di default (nessun host indicato): usa la config systemd.
    if not host_id:
        sd = settings.systemd
        kw: dict[str, Any] = {
            "host": sd.ssh_host, "port": sd.ssh_port,
            "username": sd.ssh_user or live_ssh_config().default_user or "root",
            "known_hosts": None,
        }
        if sd.ssh_key:
            kw["client_keys"] = [sd.ssh_key]
        return kw

    # Host esplicito: cerca fra gli host della discovery SSH, poi i default.
    disc = live_ssh_config()
    match = next((h for h in disc.hosts if h.ip == host_id), None)
    kw = {
        "host": host_id,
        "port": (match.port if match else 22),
        "username": (getattr(match, "user", None) if match else None)
                    or disc.default_user or settings.systemd.ssh_user or "root",
        "known_hosts": None,
    }
    key = (getattr(match, "key", None) if match else None) or disc.default_key
    password = getattr(match, "password", None) if match else None
    if key:
        kw["client_keys"] = [key]
    elif password:
        kw["password"] = password
    return kw


# ── Sistema operativo dell'host ────────────────────────────────────

# Windows OpenSSH lancia i comandi tramite cmd.exe, la cui riga si ferma qui.
# E' l'unico vincolo reale sulla dimensione di uno script PowerShell.
TETTO_RIGA_COMANDO = 8191

# Risposte di `uname -s` che valgono "questo host parla POSIX".
_UNAME_POSIX = ("linux", "darwin", "freebsd", "openbsd", "netbsd", "sunos", "aix")


def comando_powershell(script: str) -> str:
    """Incapsula uno script PowerShell in una riga di comando eseguibile via SSH.

    Lo script viaggia in `-EncodedCommand` (base64 di UTF-16LE) e non su stdin,
    che pure sarebbe senza limiti di lunghezza: `-Command -` legge stdin come se
    fosse digitato e chiude un blocco multi-riga **solo** quando incontra una
    riga vuota. Dimenticarla non produce un errore, produce **nessun output** -
    misurato: uno script con un hashtable su piu' righe restituisce una risposta
    vuota, indistinguibile da un host muto. Il base64 non ha regole nascoste, e
    il suo unico limite - la lunghezza - e' verificabile qui e in un test.
    """
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    comando = f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {b64}"
    if len(comando) > TETTO_RIGA_COMANDO:
        raise ValueError(
            f"script PowerShell troppo lungo: {len(comando)} caratteri una volta "
            f"codificato, il tetto di cmd.exe e' {TETTO_RIGA_COMANDO}. Va accorciato: "
            f"oltre il tetto il comando arriverebbe troncato, senza alcun errore")
    return comando


async def esegui_powershell(conn, script: str, timeout: float = 20) -> str:
    """Esegue uno script PowerShell su una connessione SSH gia' aperta.

    `errors="replace"` non e' pignoleria: PowerShell 5.1 scrive nella codepage
    della console (CP850 sotto OpenSSH) e una sola stringa accentata - il nome
    italiano di un servizio - farebbe fallire la decodifica UTF-8 dell'intero
    lotto di quell'host, non solo di quella riga.
    """
    res = await asyncio.wait_for(
        conn.run(comando_powershell(script), check=False,
                 encoding="utf-8", errors="replace"),
        timeout=timeout)
    return res.stdout or ""


def os_dichiarato(ip: str) -> str:
    """Il valore di `os:` scritto in configurazione per quell'host, o "auto"."""
    match = next((h for h in live_ssh_config().hosts if h.ip == ip), None)
    return getattr(match, "os", None) or "auto"


async def risolvi_os(ip: str, conn) -> str:
    """Che sistema parla l'host: "linux", "windows" oppure "unknown".

    Prende una connessione **gia' aperta** apposta: tutti i chiamanti (risorse,
    facts della discovery, servizi Windows) ne aprono comunque una per host,
    quindi la sonda non ne costa mai una in piu'.

    Quando la configurazione lo dichiara non si sonda niente. Con "auto" si
    sonda una volta sola e si ricorda; un esito incerto **non** si memorizza,
    cosi' un host che ha risposto male una volta viene riprovato invece di
    restare classificato male fino al riavvio.
    """
    dichiarato = os_dichiarato(ip)
    if dichiarato in ("linux", "windows"):
        return dichiarato
    memorizzato = _os_cache.get(ip)
    if memorizzato:
        return memorizzato
    trovato = await _sonda_os(ip, conn)
    if trovato != "unknown":
        _os_cache[ip] = trovato
    return trovato


async def _sonda_os(ip: str, conn) -> str:
    """Due domande, in ordine di costo. Non si indovina mai il terzo caso.

    `uname -s` risponde su ogni Unix. Su Windows la shell di default via SSH e'
    cmd.exe, che non conosce il comando e si lamenta **sullo stderr**: qui il
    segnale e' lo stdout vuoto, non un errore. Solo allora si chiede a PowerShell.
    """
    try:
        res = await asyncio.wait_for(
            conn.run("uname -s", check=False, encoding="utf-8", errors="replace"),
            timeout=10)
        nome = (res.stdout or "").strip().split()
        if nome and nome[0].lower() in _UNAME_POSIX:
            return "linux"
    except Exception as e:
        log.debug(f"sonda uname su {ip}: {e}")
    try:
        if "windows_nt" in (await esegui_powershell(conn, "$env:OS", timeout=15)).strip().lower():
            return "windows"
    except Exception as e:
        log.debug(f"sonda PowerShell su {ip}: {e}")
    log.warning(f"sistema operativo di {ip} non riconosciuto: ne' uname ne' PowerShell "
                f"hanno risposto. Dichiaralo con `os:` in discovery.ssh.hosts")
    return "unknown"
