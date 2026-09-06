"""
Aggancio dei test JavaScript dell'emulatore ANSI alla suite Python.

Servono a poter lanciare `pytest` una volta sola e avere tutto. Dove node non
c'e' — a partire da homeserver — il test si salta da solo: nessuna lista di
esclusione da tenere aggiornata.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEST_JS = REPO / "frontend" / "tests"

# Sotto WSL di norma c'e' solo il node di Windows: va bene, purche' i percorsi
# passati restino relativi a una cartella sotto /mnt/c.
NODE_WINDOWS = Path("/mnt/c/Program Files/nodejs/node.exe")


def _node() -> str | None:
    trovato = shutil.which("node")
    if trovato:
        return trovato
    return str(NODE_WINDOWS) if NODE_WINDOWS.is_file() else None


@pytest.mark.skipif(_node() is None, reason="node non disponibile su questa macchina")
def test_emulatore_ansi():
    file_test = sorted(str(p.relative_to(REPO)) for p in TEST_JS.glob("*.test.mjs"))
    assert file_test, "nessun file di test JS trovato"
    esito = subprocess.run([_node(), "--test", *file_test], cwd=REPO,
                           capture_output=True, text=True, timeout=120)
    assert esito.returncode == 0, esito.stdout[-4000:] + esito.stderr[-2000:]
