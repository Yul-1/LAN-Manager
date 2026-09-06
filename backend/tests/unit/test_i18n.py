"""
Messaggi dell'API nella lingua di chi chiede (services/i18n.py).

Il punto delicato non e' la traduzione: e' che i router fanno `detail=str(e)`
su eccezioni sollevate molto piu' in basso. Se `LocalizedError` smettesse di
rendere nella lingua della richiesta, o smettesse di essere un `ValueError`,
non se ne accorgerebbe nessuno finche' un utente non vede un 500 al posto di
un 400.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.i18n import (LINGUE, LocalizedError, chiavi_note, imposta_lingua,
                           lingua_corrente, t)

CATALOGHI = Path(__file__).resolve().parents[2] / "i18n"


def _carica(lingua: str) -> dict:
    return json.loads((CATALOGHI / f"{lingua}.json").read_text(encoding="utf-8"))


# ── I cataloghi ────────────────────────────────────────────────────

def test_i_cataloghi_hanno_le_stesse_chiavi():
    """Una chiave presente in una lingua sola funziona finche' si guarda in
    quella lingua, poi ripiega in silenzio e il difetto lo scopre l'utente."""
    en, it = set(_carica("en")), set(_carica("it"))
    assert sorted(it - en) == [], "chiavi solo in italiano"
    assert sorted(en - it) == [], "chiavi solo in inglese"
    assert len(en) > 20, "il catalogo sembra troppo piccolo"


def test_i_segnaposto_combaciano_fra_le_lingue():
    """`{cidr}` in una lingua e `{rete}` nell'altra darebbe un messaggio con il
    segnaposto in chiaro, o un parametro mancante."""
    import re
    en, it = _carica("en"), _carica("it")
    for chiave, testo in en.items():
        a = set(re.findall(r"\{(\w+)\}", testo))
        b = set(re.findall(r"\{(\w+)\}", it[chiave]))
        assert a == b, f"{chiave}: segnaposto diversi fra en {a} e it {b}"


def test_nessun_catalogo_contiene_indirizzi():
    """E' testo che finisce in pagina e nell'export pubblico."""
    import re
    for lingua in LINGUE:
        testo = (CATALOGHI / f"{lingua}.json").read_text(encoding="utf-8")
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", testo), lingua


def test_ogni_chiave_usata_nel_codice_esiste():
    """Una chiave scritta male non esplode: esce il nome della chiave al posto
    del messaggio, e lo si scopre solo guardando quella schermata."""
    import re
    note = chiavi_note()
    radice = Path(__file__).resolve().parents[2]
    usate: set[str] = set()
    for f in list(radice.glob("routers/*.py")) + list(radice.glob("services/*.py")) \
            + list(radice.glob("middleware/*.py")):
        testo = f.read_text(encoding="utf-8")
        usate |= set(re.findall(r'(?:\bt|LocalizedError)\(\s*"((?:err|stato|sicurezza)\.[\w.]+)"',
                                testo))
    assert usate, "nessuna chiave trovata: controlla la regex"
    assert sorted(usate - note) == [], "chiavi usate nel codice ma assenti dal catalogo"


# ── Scelta della lingua ────────────────────────────────────────────

@pytest.mark.parametrize("header, atteso", [
    ("it-IT,it;q=0.9,en;q=0.8", "it"),
    ("en-US,en;q=0.9", "en"),
    ("it", "it"),
    ("de-DE,de;q=0.9", "en"),      # lingua che non abbiamo -> ripiego
    ("", "en"),
    (None, "en"),
    ("*", "en"),
])
def test_accept_language_viene_interpretato(header, atteso):
    assert imposta_lingua(header) == atteso
    assert lingua_corrente() == atteso


def test_la_prima_lingua_conosciuta_vince():
    """Con `de,it,en` la scelta e' l'italiano: il tedesco non ce l'abbiamo, ma
    l'ordine di preferenza dell'utente va rispettato per il resto."""
    assert imposta_lingua("de-DE,it;q=0.9,en;q=0.8") == "it"


# ── Resa dei messaggi ──────────────────────────────────────────────

def test_lo_stesso_errore_esce_nelle_due_lingue():
    e = LocalizedError("err.passwordCorta", n=6)
    assert e.testo("it") == "password troppo corta (minimo 6 caratteri)"
    assert e.testo("en") == "password too short (minimum 6 characters)"
    assert e.testo("it") != e.testo("en")


def test_localized_error_e_un_valueerror():
    """I router e i service catturano gia' `except ValueError` per rispondere
    400. Se questa gerarchia cambiasse, gli stessi errori diventerebbero 500:
    un errore dell'utente presentato come guasto del server."""
    assert issubclass(LocalizedError, ValueError)
    with pytest.raises(ValueError):
        raise LocalizedError("err.serveSubnet")


def test_str_segue_la_lingua_della_richiesta():
    """E' cio' che fa funzionare i ~70 `detail=str(e)` senza toccarli."""
    e = LocalizedError("err.credenziali")
    imposta_lingua("it")
    assert str(e) == "credenziali non valide"
    imposta_lingua("en")
    assert str(e) == "invalid credentials"


def test_una_chiave_sconosciuta_si_vede():
    """Meglio una chiave in pagina che un messaggio d'errore vuoto: la prima si
    corregge, il secondo non si nota."""
    assert t("chiave.inventata") == "chiave.inventata"


def test_un_parametro_mancante_non_fa_cadere_la_richiesta():
    """Stava gia' segnalando un errore: farne cadere la risposta sarebbe peggio
    del messaggio imperfetto."""
    assert "{" in t("err.passwordCorta")          # senza n=, il segnaposto resta
    assert t("err.subnetNonValida", cidr="x") == "invalid subnet: x"


def test_la_lingua_non_si_mescola_fra_richieste_concorrenti():
    """La lingua sta in un contextvar proprio per questo: con una variabile
    globale, due richieste in parallelo si sovrascriverebbero a vicenda."""
    async def richiesta(lingua, chiave):
        imposta_lingua(lingua)
        await asyncio.sleep(0)                    # cede il controllo all'altra
        return t(chiave)

    async def prova():
        return await asyncio.gather(
            richiesta("it", "err.credenziali"),
            richiesta("en", "err.credenziali"),
        )

    it, en = asyncio.run(prova())
    assert it == "credenziali non valide"
    assert en == "invalid credentials"
