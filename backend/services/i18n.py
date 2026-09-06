"""
services/i18n.py — Messaggi dell'API nella lingua di chi chiede
===============================================================
Il problema da risolvere non e' tradurre i router: e' che ~70 handler fanno
`detail=str(e)`, cioe' rimandano al client il testo di un'eccezione sollevata
molto piu' in basso (`nettools`, `device_store`, `config_store`). Tradurre il
router non servirebbe a niente: la frase e' gia' fissata al punto in cui
l'eccezione nasce.

Da qui **`LocalizedError`**: porta una chiave e i suoi parametri, e il suo
`__str__` rende nella lingua della richiesta corrente. I router restano
identici — `str(e)` continua a funzionare — ma il testo esce giusto.

La lingua sta in un `contextvar`: e' per-richiesta e non si mescola fra task
concorrenti, cosa che una variabile globale non garantirebbe con asyncio.

Gli alert (`services/alerts.py`) sono un caso a parte: nascono nel collector,
fuori da qualunque richiesta. Conservano chiave e parametri e vengono resi
quando l'API li serializza, non quando la regola scatta.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path

log = logging.getLogger("i18n")

LINGUE = ("en", "it")
RIPIEGO = "en"

_CATALOGHI: dict[str, dict[str, str]] = {}
_lingua: ContextVar[str] = ContextVar("lingua", default=RIPIEGO)

_DIR = Path(__file__).resolve().parent.parent / "i18n"


def _carica() -> None:
    """Carica i cataloghi una volta sola. Un catalogo illeggibile non deve
    impedire l'avvio: si logga e si va avanti con le chiavi nude, che si vedono
    e si correggono — meglio di un servizio che non parte."""
    for lingua in LINGUE:
        f = _DIR / f"{lingua}.json"
        try:
            _CATALOGHI[lingua] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning(f"catalogo {lingua} non caricato ({e}): si useranno le chiavi")
            _CATALOGHI[lingua] = {}


_carica()


def lingua_corrente() -> str:
    return _lingua.get()


def imposta_lingua(valore: str | None) -> str:
    """Imposta la lingua della richiesta corrente da un header Accept-Language.

    Accetta la forma completa (`it-IT,it;q=0.9,en;q=0.8`): si prende la prima
    lingua conosciuta in ordine di preferenza. Sconosciuta o assente -> inglese.
    """
    scelta = RIPIEGO
    for pezzo in (valore or "").split(","):
        tag = pezzo.split(";")[0].strip().lower().split("-")[0]
        if tag in LINGUE:
            scelta = tag
            break
    _lingua.set(scelta)
    return scelta


def t(chiave: str, lingua: str | None = None, **params) -> str:
    """Testo della chiave nella lingua indicata (o in quella della richiesta).

    Una chiave mancante ritorna la chiave stessa: un messaggio vuoto in
    risposta a un errore sarebbe peggio di una chiave visibile, che almeno
    dice cosa cercare.
    """
    lang = lingua or _lingua.get()
    testo = _CATALOGHI.get(lang, {}).get(chiave)
    if testo is None:
        testo = _CATALOGHI.get(RIPIEGO, {}).get(chiave)
    if testo is None:
        return chiave
    if params:
        try:
            return testo.format(**params)
        except (KeyError, IndexError) as e:
            # Un parametro mancante non deve far cadere la richiesta che stava
            # gia' segnalando un errore: si mostra il testo grezzo.
            log.warning(f"parametri incompleti per '{chiave}': {e}")
            return testo
    return testo


class LocalizedError(ValueError):
    """Errore con un testo che dipende dalla lingua della richiesta.

    Si solleva dove il problema viene rilevato, e `str(e)` produce la frase
    giusta ovunque venga interpolata — router compresi, che non cambiano.

    Eredita da **ValueError** di proposito: i router e i service catturano gia'
    `except ValueError` per trasformare un errore di validazione in un 400.
    Derivare da `Exception` avrebbe fatto sfuggire quelle eccezioni fino
    all'handler generico, che le trasforma in 500 — un errore dell'utente
    presentato come un guasto del server.
    """

    def __init__(self, chiave: str, **params):
        self.chiave = chiave
        self.params = params
        super().__init__(chiave)

    def __str__(self) -> str:
        return t(self.chiave, **self.params)

    def testo(self, lingua: str) -> str:
        """Reso in una lingua esplicita, per i test e per i contesti fuori
        richiesta (collector, alert)."""
        return t(self.chiave, lingua=lingua, **self.params)


def chiavi_note() -> set[str]:
    """Chiavi presenti nel catalogo di ripiego. Usata dai test per verificare
    che ogni chiave sollevata dal codice esista davvero."""
    return set(_CATALOGHI.get(RIPIEGO, {}))
