"""
services/log_filter.py — Righe di log da non tenere
====================================================
Il problema e' stato misurato: con `httpx` gia' zitto il log del backend
cresce di ~26 righe al minuto, cioe' tocca il tetto di `logs.max_rows` in circa
32 ore. Oltre quel punto non si vede piu' indietro. E la retention a giorni non
serve a niente, perche' il ritmo non lo decide chi la configura.

Qui sta il filtro che permette di dire "questa frase non mi interessa". Una
riga scartata:

  - non entra nel buffer in memoria, quindi non finisce nell'archivio;
  - non compare in pagina, nemmeno se era gia' stata archiviata prima che la
    regola esistesse (il filtro si applica anche in lettura);
  - **resta in `docker logs`**, che e' un handler diverso da questo. Si smette
    di conservare il rumore, non di produrlo: se un giorno serve, e' ancora li'.

Due regole di sicurezza, non negoziabili:

  1. **L'audit non si filtra mai.** E' la traccia di chi ha fatto cosa, cioe'
     esattamente quello che si va a leggere quando qualcosa e' andato storto.
     Poterne nascondere una riga dalla configurazione sarebbe un buco.
  2. **Una regola vuota non scarta niente.** Senza questo, un `pattern: ""`
     lasciato per sbaglio nello YAML svuoterebbe la pagina Logs in silenzio.

Le regole si leggono all'avvio e si compilano una volta sola: cambiarle
richiede il riavvio del servizio, come il resto della configurazione.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from config import settings

log = logging.getLogger("log-filter")

# Sorgente che nessuna regola puo' toccare (vedi punto 1 in testa al file).
INTOCCABILE = "audit"

# Prefisso delle sorgenti journalctl: nelle regole si puo' scrivere "journal"
# secco per intenderle tutte, invece di elencare un host alla volta.
_JOURNAL = "journal"


@dataclass
class Regola:
    """Una `LogExclude` della config, con il pattern gia' compilato."""
    pattern: str
    src: str
    rx: Optional[re.Pattern]
    sources: tuple[str, ...]
    note: str

    def vale_per(self, source: str) -> bool:
        if not self.sources:
            return True
        s = (source or "").lower()
        for dichiarata in self.sources:
            if s == dichiarata:
                return True
            # "journal" nella regola vale per journal:<qualunque host>.
            if dichiarata == _JOURNAL and s.startswith(_JOURNAL + ":"):
                return True
        return False

    def scarta(self, riga: dict) -> bool:
        if self.src and self.src not in (riga.get("src") or "").lower():
            return False
        if not self.pattern and not self.rx:
            # Regola con il solo `src`: basta che il mittente combaci.
            return bool(self.src)
        # Il messaggio, e come ripiego la riga grezza: le sorgenti che non
        # sanno separare il testo dal resto (syslog fuori formato) hanno solo
        # `raw`, e li' un filtro deve poter comunque agire.
        testo = riga.get("msg") or riga.get("raw") or ""
        if self.rx:
            return bool(self.rx.search(testo))
        return self.pattern in testo.lower()


_regole: Optional[list[Regola]] = None
# Vero mentre le regole si stanno compilando. Serve contro un rientro non
# ovvio: `_compila()` scrive un warning quando una regola e' malformata, quel
# warning passa da `RingHandler`, che chiama `scarta()`, che chiama `regole()`
# — con `_regole` ancora a None. Senza questa guardia si ricompilerebbe da capo
# ad ogni riga, all'infinito.
_in_compilazione = False


def _compila(voce) -> Optional[Regola]:
    pattern = (getattr(voce, "pattern", "") or "").strip()
    src = (getattr(voce, "src", "") or "").strip().lower()
    if not pattern and not src:
        # Punto 2: una regola che non dice niente non puo' scartare tutto.
        log.warning("logs.exclude: regola senza 'pattern' ne' 'src', ignorata")
        return None
    rx = None
    if pattern and getattr(voce, "regex", False):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            # Una regex sbagliata si dichiara e si salta: applicarla a meta' o
            # trattarla come testo darebbe un filtro che nessuno ha scritto.
            log.warning(f"logs.exclude: regex non valida {pattern!r} ({e}), regola ignorata")
            return None
    return Regola(
        pattern="" if rx else pattern.lower(),
        src=src,
        rx=rx,
        sources=tuple(s.strip().lower() for s in (getattr(voce, "sources", []) or []) if s.strip()),
        note=(getattr(voce, "note", "") or "").strip(),
    )


def regole() -> list[Regola]:
    global _regole, _in_compilazione
    if _regole is None:
        if _in_compilazione:
            # Rientro dall'handler di logging: le regole non ci sono ancora, e
            # la riga che le sta facendo nascere si tiene.
            return []
        _in_compilazione = True
        try:
            _regole = [r for r in (_compila(v) for v in settings.logs.exclude
                                   if getattr(v, "enabled", True)) if r]
        finally:
            _in_compilazione = False
        if _regole:
            log.info(f"logs.exclude: {len(_regole)} regole di scarto attive")
    return _regole


def reset() -> None:
    """Dimentica le regole compilate. La usa la suite, e chi cambia la config
    a caldo nei test: in esercizio le regole si rileggono al riavvio."""
    global _regole, _in_compilazione
    _regole = None
    _in_compilazione = False


def scarta(riga: dict, source: str = "backend") -> bool:
    """True se questa riga non va tenuta.

    Non solleva mai: e' chiamata anche dall'handler di logging, dove
    un'eccezione romperebbe la `log.qualcosa()` di chi la sta usando (stessa
    regola di `RingHandler`). In caso di dubbio la riga si tiene.
    """
    try:
        if (source or "").lower() == INTOCCABILE:
            return False
        for r in regole():
            if r.vale_per(source) and r.scarta(riga):
                return True
    except Exception:
        return False
    return False


def pattern_per_grep(source: str) -> list[str]:
    """Pattern applicabili **sul router**, prima del suo `tail`.

    Sul router il taglio alle ultime N righe avviene la' (`logread | tail`),
    quindi filtrare qui dopo averle ricevute mostrerebbe venti righe invece di
    trecento: e' un difetto gia' misurato, al contrario. I pattern di
    testo semplice viaggiano percio' come `grep -v` insieme a quelli scritti
    nella pagina. Le regole `regex` e quelle legate a un `src` non sanno
    esprimersi in quel comando e restano al filtro in Python: su questa
    sorgente conviene scrivere pattern di testo.
    """
    return [r.pattern for r in regole()
            if r.pattern and not r.rx and not r.src and r.vale_per(source)]
