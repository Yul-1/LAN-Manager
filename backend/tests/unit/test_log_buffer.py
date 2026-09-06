"""
Buffer in memoria dei log del backend (services/log_buffer.py).

Due proprieta' contano piu' delle altre. La prima: l'handler non deve MAI
sollevare, perche' non romperebbe se stesso ma la chiamata `log.qualcosa()` di
chi lo stava usando, cioe' potenzialmente qualunque punto del backend. La
seconda: i logger di uvicorn devono finirci dentro, perche' li' stanno le
richieste HTTP, cioe' proprio le righe che si vanno a cercare quando qualcosa
risponde 401 o 502 a sorpresa.
"""
from __future__ import annotations

import logging

import pytest

from services import log_buffer as LB


@pytest.fixture(autouse=True)
def buffer_pulito():
    # In esercizio `logging.basicConfig` porta la root a INFO (main.py). Nella
    # suite resta a WARNING, e un logger che filtra prima dell'handler farebbe
    # sembrare rotto il buffer: qui si riproduce la condizione vera.
    root = logging.getLogger()
    livello = root.level
    root.setLevel(logging.INFO)
    LB.uninstall()
    LB.get_log_buffer().clear()
    yield
    LB.uninstall()
    LB.get_log_buffer().clear()
    root.setLevel(livello)


def test_le_righe_arrivano_nel_buffer():
    LB.install()
    logging.getLogger("prova").warning("qualcosa non va")
    righe = LB.get_log_buffer().tail()
    assert righe[-1]["msg"] == "qualcosa non va"
    assert righe[-1]["level"] == "warn"
    assert righe[-1]["src"] == "prova"


def test_i_log_di_uvicorn_non_si_perdono():
    # uvicorn configura i propri logger con propagate=False: un handler messo
    # solo sulla root non vedrebbe una singola richiesta HTTP.
    uv = logging.getLogger("uvicorn.access")
    uv.propagate = False
    uv.setLevel(logging.INFO)
    LB.install()
    uv.info("GET /api/logs/ 200")
    assert any("GET /api/logs/" in r["msg"] for r in LB.get_log_buffer().tail())


def test_lo_stack_di_una_eccezione_finisce_in_pagina():
    LB.install()
    try:
        raise ValueError("rotto")
    except ValueError:
        logging.getLogger("prova").error("non gestita", exc_info=True)
    riga = LB.get_log_buffer().tail()[-1]
    assert "ValueError: rotto" in riga["exc"]


def test_l_handler_non_solleva_mai():
    """Un record il cui `getMessage()` esplode: succede davvero con un
    `log.info("%s", oggetto_rotto)`.

    Si chiama `emit()` a mano invece di passare da `logging`: con il logger vero
    a sollevare sarebbe l'handler di cattura di pytest, e il test misurerebbe
    quello invece del nostro.
    """
    class Cattivo:
        def __str__(self):
            raise RuntimeError("non stampabile")

    handler = LB.install()
    rotto = logging.LogRecord("prova", logging.INFO, __file__, 1, "%s", (Cattivo(),), None)
    handler.emit(rotto)                                  # non deve sollevare
    handler.emit(logging.LogRecord("prova", logging.INFO, __file__, 2,
                                   "questa passa", (), None))
    assert any(r["msg"] == "questa passa" for r in LB.get_log_buffer().tail())


def test_il_buffer_e_circolare():
    buf = LB.LogBuffer(maxlen=100)
    for i in range(300):
        buf.add({"msg": f"riga {i}", "level": "info", "src": "x", "ts_ms": i})
    righe = buf.tail()
    assert len(righe) == 100
    assert righe[0]["msg"] == "riga 200"


def test_il_seq_non_riparte_mai():
    # E' il numero su cui si regge il "segui in tempo reale": se ripartisse,
    # il tail rimanderebbe righe gia' viste ad ogni giro del buffer.
    buf = LB.LogBuffer(maxlen=10)
    for i in range(50):
        buf.add({"msg": str(i), "level": "info", "src": "x", "ts_ms": i})
    assert buf.last_seq() == 50
    assert buf.tail(after_seq=48) == buf.tail()[-2:]


def test_il_travaso_non_riscrive_le_stesse_righe():
    buf = LB.LogBuffer(maxlen=100)
    for i in range(5):
        buf.add({"msg": str(i), "level": "info", "src": "x", "ts_ms": i})
    assert len(buf.drain()) == 5
    assert buf.drain() == [], "un secondo giro duplicherebbe tutto nel database"
    buf.add({"msg": "nuova", "level": "info", "src": "x", "ts_ms": 9})
    assert [r["msg"] for r in buf.drain()] == ["nuova"]


def test_se_il_travaso_resta_indietro_non_insegue_righe_perdute():
    # Il buffer e' circolare: con il collector fermo le righe piu' vecchie
    # cadono fuori. Il segnaposto deve andare avanti lo stesso, altrimenti
    # resterebbe fermo a cercare righe che non esistono piu'.
    buf = LB.LogBuffer(maxlen=10)
    for i in range(100):
        buf.add({"msg": str(i), "level": "info", "src": "x", "ts_ms": i})
    primo = buf.drain()
    assert len(primo) == 10
    assert buf.drain() == []


def test_installare_due_volte_non_raddoppia_le_righe():
    LB.install()
    LB.install()
    logging.getLogger("prova").info("una sola volta")
    assert sum(1 for r in LB.get_log_buffer().tail()
               if r["msg"] == "una sola volta") == 1
