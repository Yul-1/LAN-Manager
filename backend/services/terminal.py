"""
services/terminal.py — Sessioni SSH interattive (PTY) per la dashboard
======================================================================
Apre una shell su un host **gia' noto alla configurazione** e fa da ponte fra
il WebSocket del browser e il canale SSH.

Vincoli di sicurezza:
  - l'host arriva dal client ma deve stare nell'elenco di `allowed_hosts()`
    (router, host della discovery SSH, host systemd): nessuna destinazione
    arbitraria, nessuna credenziale passata dal browser;
  - i parametri SSH li risolve `services/ssh_hosts.resolve_ssh_target`, unica
    fonte per utente/porta/chiave;
  - apertura, chiusura e comandi digitati finiscono nel registro di audit;
  - sessione chiusa dopo `terminal.idle_timeout` secondi di inattivita' e
    numero massimo di sessioni contemporanee limitato.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import asyncssh

from config import settings
from services.audit import audit
from services.errors import exc_text
from services.ssh_hosts import live_ssh_config, resolve_ssh_target

log = logging.getLogger("terminal")

# Prompt di password tipici: la riga digitata subito dopo non va nell'audit.
# Il "for <qualcosa>" vale per entrambe le parole: ssh e ssh-add su una chiave
# cifrata stampano "Enter passphrase for key '/root/.ssh/id_ed25519':", e prima
# che il for fosse comune anche a passphrase quella riga non veniva riconosciuta,
# quindi la passphrase finiva in chiaro in config/audit.log.
_PASSWORD_PROMPT_RE = re.compile(
    r"(password|passphrase)(\s+for\s+[^:]*)?\s*:\s*$", re.I)

_active: int = 0


def allowed_hosts() -> list[dict]:
    """Host su cui e' consentito aprire una sessione, presi dalla config.

    `source` dice da dove arriva ciascuno, perche' un elenco senza spiegazione
    sembra arbitrario: "router" e "systemd" vengono da altre sezioni della
    configurazione e si cambiano da Impostazioni, gli "ssh" sono l'elenco
    gestibile dalla pagina Terminale (vedi routers/terminal.py).
    """
    hosts: dict[str, dict] = {}
    # Nomi dal catalogo dei dispositivi: l'elenco mostrava "192.0.2.10 —
    # user@192.0.2.10", cioe' l'indirizzo due volte e mai il nome della
    # macchina, che LANMng conosce gia'. Se il catalogo non e' leggibile si
    # ripiega sull'indirizzo: un nome mancante non deve togliere l'host.
    try:
        from services.device_store import get_device_store
        catalogo = get_device_store().catalog_by_ip()
    except Exception as e:                       # catalogo rotto o assente
        log.warning(f"catalogo dispositivi non leggibile per le etichette: {e}")
        catalogo = {}

    def add(host: str, label: str, source: str, editable: bool = False):
        host = (host or "").strip()
        if not host or host in hosts:
            return
        target = resolve_ssh_target(host)
        hosts[host] = {"host": host, "label": label or host,
                       "user": target.get("username", ""), "source": source,
                       "editable": editable}

    def nome_noto(ip: str) -> str:
        return (catalogo.get((ip or "").strip()) or {}).get("name", "")

    add(settings.router.host, settings.router.name or "router", "router")
    if settings.systemd.ssh_enabled:
        add(settings.systemd.ssh_host, "host di LANMng", "systemd")
    for h in live_ssh_config().hosts:
        add(h.ip, nome_noto(h.ip), "ssh", editable=True)
    return list(hosts.values())


def is_allowed(host: str) -> bool:
    return any(h["host"] == (host or "").strip() for h in allowed_hosts())


def available_keys() -> list[str]:
    """Chiavi private presenti nella cartella montata nel container.

    La UI le offre in elenco: chiedere di scrivere a mano un percorso porta
    dritti all'errore di incollare il *contenuto* della chiave al posto del file.
    La cartella non e' fissata nel codice: e' quella della chiave predefinita
    (o di quella del router), quindi segue la configurazione.
    """
    candidates = [live_ssh_config().default_key, settings.router.ssh_key,
                  settings.systemd.ssh_key]
    keys: list[str] = []
    for folder in {str(Path(c).parent) for c in candidates if c}:
        try:
            entries = sorted(Path(folder).iterdir())
        except OSError:
            continue
        for f in entries:
            if f.is_file() and f.suffix != ".pub" and os.access(f, os.R_OK):
                keys.append(str(f))
    return sorted(set(keys))


class TerminalSession:
    """Una shell remota collegata a un WebSocket."""

    def __init__(self, host: str, client_ip: str,
                 send: Callable[[dict], Awaitable[None]]):
        self.host = host
        self.client_ip = client_ip
        self.send = send
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._proc = None
        self._pump: Optional[asyncio.Task] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._last_activity = time.monotonic()
        self._started = time.monotonic()
        self._closed = False
        self._line = ""            # riga in composizione, per l'audit
        self._skip_line = False    # la prossima riga segue un prompt di password
        self._out_tail = ""        # coda dell'output: i prompt arrivano spesso spezzati
        self.user = ""

    # ── Ciclo di vita ──────────────────────────────────────────────

    async def open(self, cols: int, rows: int) -> None:
        global _active
        if not settings.terminal.enabled:
            raise PermissionError("terminale disattivato in configurazione")
        if not is_allowed(self.host):
            raise PermissionError("host non consentito")
        if _active >= settings.terminal.max_sessions:
            raise RuntimeError(f"troppe sessioni aperte (max {settings.terminal.max_sessions})")

        target = resolve_ssh_target(self.host)
        self.user = target.get("username", "")
        # Lo slot si prende PRIMA di connettersi: fra il controllo e la fine
        # dell'handshake ci sono due await, e altre richieste passerebbero tutte
        # dal controllo prima che una sola incrementi il contatore.
        _active += 1
        try:
            self._conn = await asyncssh.connect(**target, connect_timeout=10)
            self._proc = await self._conn.create_process(
                term_type=settings.terminal.term_type,
                term_size=(_clamp(cols, 20, 500), _clamp(rows, 5, 200)),
                encoding="utf-8", errors="replace",
            )
        except BaseException as e:
            # Se la shell non parte la connessione resterebbe aperta e senza
            # riferimenti: va chiusa qui, lo slot restituito.
            _active = max(_active - 1, 0)
            if self._conn:
                self._conn.close()
                self._conn = None
            if isinstance(e, FileNotFoundError):
                # asyncssh direbbe solo "[Errno 2] No such file or directory":
                # incomprensibile per chi ha sbagliato a compilare il campo chiave.
                raise RuntimeError(
                    f"chiave SSH non trovata: {e.filename or e}. Nel campo chiave va il "
                    f"percorso del file dentro il container, non il testo della chiave"
                ) from None
            raise
        audit("terminal.aperta", ip=self.client_ip, host=self.host, utente=self.user)
        self._pump = asyncio.create_task(self._pump_output())
        self._watchdog = asyncio.create_task(self._idle_watchdog())

    async def close(self, reason: str = "chiusa") -> None:
        global _active
        if self._closed:      # chiudono sia il pump sia il WebSocket: una riga sola
            return
        self._closed = True
        for task in (self._pump, self._watchdog):
            if task and not task.done():
                task.cancel()
        if self._proc:
            self._proc.close()
            self._proc = None
            _active = max(_active - 1, 0)
        if self._conn:
            self._conn.close()
            self._conn = None
        audit("terminal.chiusa", ip=self.client_ip, host=self.host,
              motivo=reason, durata_s=round(time.monotonic() - self._started))

    # ── Dati ───────────────────────────────────────────────────────

    async def write(self, data: str) -> None:
        if not self._proc:
            return
        self._last_activity = time.monotonic()
        self._track_input(data)
        self._proc.stdin.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if self._proc:
            self._last_activity = time.monotonic()
            self._proc.change_terminal_size(_clamp(cols, 20, 500), _clamp(rows, 5, 200))

    async def _pump_output(self) -> None:
        """Legge dalla shell e inoltra al browser finche' la sessione vive."""
        try:
            while self._proc:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                self._last_activity = time.monotonic()
                if settings.terminal.audit_commands:
                    self._note_output(chunk)
                await self.send({"type": "data", "data": chunk})
        except asyncio.CancelledError:
            raise               # chiusura pilotata da close(): niente da annunciare
        except Exception as e:
            log.info(f"sessione {self.host} interrotta: {exc_text(e)}")
        # Qui ci si arriva solo se la shell remota e' finita da sola.
        await self.send({"type": "closed", "reason": "sessione remota terminata"})
        await self.close("shell terminata")

    async def _idle_watchdog(self) -> None:
        timeout = max(settings.terminal.idle_timeout, 30)
        try:
            while True:
                await asyncio.sleep(15)
                if time.monotonic() - self._last_activity > timeout:
                    await self.send({"type": "closed",
                                     "reason": f"chiusa dopo {timeout}s di inattivita'"})
                    await self.close("timeout inattivita'")
                    return
        except asyncio.CancelledError:
            raise

    # ── Audit dei comandi ──────────────────────────────────────────

    def _note_output(self, chunk: str) -> None:
        """Riconosce i prompt di password nell'output della shell.

        Due accortezze imparate a spese dell'audit:
          - il prompt puo' arrivare spezzato fra due letture ("Passwor" + "d: "),
            quindi la ricerca gira su una coda cumulativa, non sul singolo pezzo;
          - una volta visto il prompt il flag NON si azzera al primo output
            successivo (un beep, un riposizionamento del cursore): resta finche'
            la riga non viene consumata, altrimenti basta un byte qualsiasi per
            far finire la password nel log.
        """
        self._out_tail = (self._out_tail + chunk)[-400:]
        if _PASSWORD_PROMPT_RE.search(self._out_tail):
            self._skip_line = True

    def _track_input(self, data: str) -> None:
        """Ricostruisce le righe digitate per il registro di audit.

        Con un PTY dal browser arrivano tasti, non comandi: la riga si accumula
        fino a Invio. Se il server aveva appena chiesto una password la riga non
        viene registrata (euristica dichiarata in config: `audit_commands`).
        """
        if not settings.terminal.audit_commands:
            return
        for ch in data:
            if ch in ("\r", "\n"):
                line = self._line.strip()
                self._line = ""
                if line and not self._skip_line:
                    audit("terminal.comando", ip=self.client_ip, host=self.host, cmd=line)
                # Riga consumata: si riparte puliti per la successiva.
                self._skip_line = False
                self._out_tail = ""
            elif ch in ("\x7f", "\b"):
                self._line = self._line[:-1]
            elif ch == "\x03":               # Ctrl+C: la riga viene abbandonata
                self._line = ""
            elif ch >= " ":
                self._line += ch
            if len(self._line) > 4096:       # incolli enormi: non li accumuliamo
                self._line = self._line[-4096:]


def _clamp(value, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low
