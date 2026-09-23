"""healthcheck.py — Controllo di salute del container: GET /health sul socket.

Il backend non ascolta su nessuna porta TCP (vedi il CMD del Dockerfile), quindi
il vecchio `urllib.request.urlopen('http://127.0.0.1:8000/health')` non ha piu'
niente a cui collegarsi. urllib non parla con gli unix socket: la richiesta si
scrive a mano, solo stdlib, perche' l'immagine non ha curl.

Uso: python healthcheck.py [percorso-socket]   (exit 0 = sano, 1 = no)
"""
from __future__ import annotations

import socket
import sys

SOCKET = "/run/lanmng/backend.sock"
TIMEOUT = 4.0   # sotto i 5s del timeout del healthcheck Docker


def sano(percorso: str = SOCKET) -> bool:
    """True se il backend risponde 200 su /health."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(percorso)
            s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            riga = s.recv(64).split(b"\r\n", 1)[0]
    except OSError as e:
        print(f"healthcheck: {percorso}: {e}", file=sys.stderr)
        return False
    parti = riga.split()
    if len(parti) < 2 or parti[1] != b"200":
        print(f"healthcheck: risposta inattesa {riga!r}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    sys.exit(0 if sano(sys.argv[1] if len(sys.argv) > 1 else SOCKET) else 1)
