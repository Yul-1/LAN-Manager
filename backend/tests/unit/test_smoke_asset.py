"""
Controllo degli asset nello smoke post-deploy.

Dal rilascio dell'i18n (2026-09-06) al 2026-09-24 i file della traduzione (i18n.js, i18n/*.js) non
sono mai arrivati in /var/www/lanmng: l'elenco del rsync nel README li
saltava. nginx rispondeva con index.html (fallback della SPA), il browser con
`nosniff` rifiutava lo script e la dashboard si fermava su "t is not defined".
Lo smoke guardava solo il ?v= dell'index, quindi passava lo stesso.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("smoke", REPO / "scripts" / "smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

INDEX = (REPO / "frontend" / "index.html").read_text()


def test_trova_tutti_gli_asset_dell_index():
    percorsi = {p.split("?")[0] for p in smoke.asset_locali(INDEX)}
    assert {"./i18n.js", "./i18n/en.js", "./i18n/it.js", "./app.js",
            "./styles.css"} <= percorsi
    # Ogni asset trovato esiste nel repo: altrimenti lo smoke lo darebbe
    # mancante anche con un deploy corretto.
    for p in percorsi:
        assert (REPO / "frontend" / p[2:]).is_file(), p


def test_asset_serviti_bene_passano():
    def scarica(p):
        return 200, ("text/css" if ".css" in p else "application/javascript")
    assert smoke.asset_sbagliati(INDEX, scarica) == []


def test_fallback_su_index_html_e_un_errore():
    def scarica(p):
        return 200, "text/html" if "i18n" in p else (
            "text/css" if ".css" in p else "application/javascript")
    sbagliati = smoke.asset_sbagliati(INDEX, scarica)
    assert len(sbagliati) == 3 and all("i18n" in s for s in sbagliati)


def test_asset_mancante_e_un_errore():
    def scarica(p):
        return (404, "text/html") if "app.js" in p else (
            200, "text/css" if ".css" in p else "application/javascript")
    assert len(smoke.asset_sbagliati(INDEX, scarica)) == 1
