"""
services/log_buffer.py — I log del backend, tenuti in memoria
=============================================================
In origine i log di LANMng si guardavano solo con `docker logs`: dal
server, da terminale, e comunque destinati a sparire insieme all'immagine al
primo aggiornamento. Qui un handler di `logging` scrive ogni riga in un buffer
circolare, cosi' la pagina Logs puo' mostrarli senza leggere il file del
container e senza dipendere dal motore che lo ospita.

Il buffer e' anche la coda da cui il collector pesca le righe da archiviare
(`services/history_store.add_log_lines`): la scrittura su disco NON avviene
qui. Se `log.info()` toccasse il database, un disco lento diventerebbe un
freno su ogni riga di log del processo.

Ogni riga porta un numero progressivo (`seq`) che non riparte mai: e' cosi' che
il "segui in tempo reale" chiede "cosa e' arrivato dopo la N" senza doversi
rileggere tutto il buffer ad ogni giro.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

from config import settings
from services.log_filter import scarta

# Livelli di `logging` -> i quattro livelli della pagina. CRITICAL sta con
# ERROR: la pagina distingue cio' che va guardato da cio' che non va guardato,
# e una quinta sfumatura non aiuterebbe nessuno.
_MAP = {
    logging.CRITICAL: "error",
    logging.ERROR: "error",
    logging.WARNING: "warn",
    logging.INFO: "info",
    logging.DEBUG: "debug",
}

# I logger che uvicorn configura con `propagate=False`: un handler messo solo
# sulla root non li vedrebbe, e sono proprio quelli che servono quando si
# indaga un 502 o un 401 a sorpresa.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _livello(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class LogBuffer:
    """Buffer circolare condiviso. Thread-safe: i record arrivano dal thread
    dell'event loop ma anche dai `to_thread` del collector."""

    def __init__(self, maxlen: Optional[int] = None):
        self._lock = threading.Lock()
        # Il pavimento vale per il valore che arriva dalla config: un
        # `buffer_lines: 5` sarebbe una pagina Logs vuota senza spiegazione.
        # Un maxlen passato a mano si rispetta com'e'.
        capienza = int(maxlen) if maxlen else max(100, int(settings.logs.buffer_lines))
        self._righe: deque = deque(maxlen=capienza)
        self._seq = 0
        # Da dove ripartira' il prossimo travaso verso il database.
        self._flushed = 0

    def add(self, riga: dict) -> None:
        with self._lock:
            self._seq += 1
            riga["seq"] = self._seq
            self._righe.append(riga)

    def tail(self, limit: int = 0, after_seq: int = 0) -> list[dict]:
        """Righe piu' recenti, o quelle dopo `after_seq` (usato dal tail)."""
        with self._lock:
            righe = list(self._righe)
        if after_seq:
            righe = [r for r in righe if r.get("seq", 0) > after_seq]
        if limit and len(righe) > limit:
            righe = righe[-limit:]
        return righe

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def drain(self) -> list[dict]:
        """Righe non ancora archiviate. Le consuma: chiamarla due volte di fila
        non riscrive le stesse righe nel database."""
        with self._lock:
            righe = [r for r in self._righe if r.get("seq", 0) > self._flushed]
            if righe:
                self._flushed = righe[-1]["seq"]
            # Il buffer e' circolare: se il travaso resta indietro piu' della
            # sua capienza, le righe piu' vecchie sono gia' cadute fuori. Il
            # segnaposto va comunque avanti, altrimenti resterebbe fermo a
            # inseguire righe che non esistono piu'.
            elif self._righe:
                self._flushed = max(self._flushed, self._righe[-1].get("seq", 0))
        return righe

    def clear(self) -> None:
        with self._lock:
            self._righe.clear()
            self._seq = 0
            self._flushed = 0


class RingHandler(logging.Handler):
    """Handler che versa i record nel buffer.

    Non solleva mai. Un handler che solleva non rompe se stesso: rompe la
    chiamata `log.qualcosa()` di chi lo stava usando, cioe' potenzialmente
    qualunque punto del backend. Vale la stessa regola di `services/audit.py`:
    meglio perdere una riga che far cadere chi la stava scrivendo.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Un errore dentro questo modulo verrebbe rilogato e rientrerebbe
            # qui: si evita il giro alla radice.
            if record.name == __name__ or record.name == "log-buffer":
                return
            riga = {
                "ts_ms": int(record.created * 1000),
                "level": _MAP.get(record.levelno) or _livello(record.levelno),
                "src": record.name,
                "msg": record.getMessage(),
                "exc": self.format_exception(record),
            }
            # Le righe scartate si fermano qui, prima del buffer: e' quello che
            # rende il filtro utile davvero, perche' il buffer e' anche la coda
            # da cui si archivia. Restano comunque in `docker logs`, che passa
            # da un altro handler (vedi services/log_filter.py).
            if scarta(riga, "backend"):
                return
            get_log_buffer().add(riga)
        except Exception:
            # Nemmeno `handleError` (che stampa su stderr) e' desiderabile: con
            # un buffer pieno di record malformati riempirebbe i log veri.
            pass

    @staticmethod
    def format_exception(record: logging.LogRecord) -> str:
        """Lo stack, quando c'e'. `log.error(..., exc_info=True)` e' il modo in
        cui il backend registra le eccezioni non gestite (main.py): senza
        questo, in pagina resterebbe il solo messaggio."""
        if not record.exc_info:
            return ""
        try:
            return logging.Formatter().formatException(record.exc_info)
        except Exception:
            return ""

    def handleError(self, record):        # noqa: N802 (nome imposto da logging)
        pass


_buffer: Optional[LogBuffer] = None
_handler: Optional[RingHandler] = None


def get_log_buffer() -> LogBuffer:
    global _buffer
    if _buffer is None:
        _buffer = LogBuffer()
    return _buffer


def install(level: int = logging.NOTSET) -> RingHandler:
    """Aggancia l'handler alla root e ai logger di uvicorn.

    Idempotente: chiamarla due volte non raddoppia le righe.
    """
    global _handler
    if _handler is None:
        _handler = RingHandler(level=level)
    for nome in ("",) + UVICORN_LOGGERS:
        logger = logging.getLogger(nome)
        if _handler not in logger.handlers:
            logger.addHandler(_handler)
    return _handler


def uninstall() -> None:
    """Stacca l'handler. Serve alla suite: un handler lasciato attaccato fa
    finire nel buffer i log di ogni test successivo."""
    global _handler
    if _handler is None:
        return
    for nome in ("",) + UVICORN_LOGGERS:
        logging.getLogger(nome).removeHandler(_handler)
    _handler = None
