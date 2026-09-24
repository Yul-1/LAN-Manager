"""
Formato delle righe di audit (services/audit.py).

Pentest 2026-09-24 (G6): un comando digitato nel terminale come
`echo x"=fake injected=yes` chiudeva il campo `cmd` a meta' e aggiungeva una
coppia `injected=yes` mai scritta dal codice. Il registro deve restare un
elenco di coppie che il parser della pagina Log rilegge tali e quali.
"""
from __future__ import annotations

import re

import pytest

from services.audit import _fmt
from services.log_sources import _COPPIA_RE


def _coppie(riga: str) -> list[tuple[str, str]]:
    return _COPPIA_RE.findall(riga)


def _riga(**campi) -> str:
    return " ".join(f"{k}={_fmt(v)}" for k, v in campi.items())


def test_il_poc_del_pentest_non_aggiunge_campi():
    riga = _riga(ip="192.0.2.5", host="192.0.2.30",
                 cmd='echo inject_test"=fake_event injected=yes')
    assert [k for k, _ in _coppie(riga)] == ["ip", "host", "cmd"]


@pytest.mark.parametrize("valore", [
    'a"b', "a=b", "a\\b", 'fine\\"', 'x" y=z', "", "con spazi",
])
def test_i_valori_strani_tornano_indietro_uguali(valore):
    (chiave, grezzo), = _coppie(_riga(cmd=valore))
    assert chiave == "cmd"
    assert grezzo.startswith('"') and grezzo.endswith('"')
    assert re.sub(r'\\(.)', r"\1", grezzo[1:-1]) == valore


def test_i_valori_semplici_restano_senza_virgolette():
    assert _fmt("192.0.2.5") == "192.0.2.5"
    assert _fmt("terminal.comando") == "terminal.comando"
