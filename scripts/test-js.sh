#!/usr/bin/env bash
# Test dell'emulatore ANSI (frontend/terminal.js) con il runner integrato di node.
#
# Sono test di sviluppo locale: dove node non e' installato (tipicamente il
# server) lo script esce 0 dichiarandolo, cosi' non fa fallire una verifica.
# Nessun package.json e nessun npm install: si usa "node --test".
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NODE="$(command -v node || true)"
# Sotto WSL spesso c'e' solo il node di Windows: funziona, purche' i percorsi
# passati siano relativi a una cartella sotto /mnt/c (che node.exe vede come C:).
if [[ -z "$NODE" && -x "/mnt/c/Program Files/nodejs/node.exe" ]]; then
    NODE="/mnt/c/Program Files/nodejs/node.exe"
fi
if [[ -z "$NODE" ]]; then
    echo "node non disponibile: test dell'emulatore ANSI saltati"
    exit 0
fi

# I file si elencano uno per uno: passare la cartella non funziona con node.exe
# invocato da WSL (prova a caricarla come modulo invece di esplorarla).
exec "$NODE" --test frontend/tests/*.test.mjs
