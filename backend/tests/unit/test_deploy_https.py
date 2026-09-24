"""
Vhost dell'nginx host in HTTPS (Fase 22).

Fino alla 0.9.x la dashboard si serviva in chiaro su :81: password di login e
cookie di sessione viaggiavano leggibili sulla LAN e sulla VPN (pentest
2026-09-23, F8). Ora l'unico server che fa da proxy al backend e' quello su
:443; :80 e :81 esistono solo per rimandare all'HTTPS.

I test tengono ferme tre cose che una modifica distratta al vhost romperebbe
senza errori visibili: nessuna via in chiaro verso il backend, il redirect che
punta al nome servito (non a un altro), e `X-Forwarded-Proto` sul server TLS,
da cui dipende il flag Secure del cookie (routers/auth.py).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
VHOST = REPO / "deploy" / "lanmng.nginx.conf"


def _server_blocks(testo: str) -> list[str]:
    """Corpo di ogni blocco `server { ... }` di primo livello.

    Scorre le graffe invece di usare una regex: le location annidate hanno le
    loro. I commenti si tolgono prima, perche' possono contenere graffe."""
    testo = re.sub(r"#[^\n]*", "", testo)
    blocchi = []
    for m in re.finditer(r"^server\s*\{", testo, re.M):
        livello, i = 1, m.end()
        while livello:
            livello += {"{": 1, "}": -1}.get(testo[i], 0)
            i += 1
        blocchi.append(testo[m.end():i - 1])
    return blocchi


def _porte(blocco: str) -> set[str]:
    return {re.sub(r"^\[::\]:", "", p) for p in re.findall(r"^\s*listen\s+([^\s;]+)", blocco, re.M)}


def _nomi(blocco: str) -> list[str]:
    return re.findall(r"^\s*server_name\s+([^;]+);", blocco, re.M)


BLOCCHI = _server_blocks(VHOST.read_text())
TLS = [b for b in BLOCCHI if "443" in _porte(b)]
IN_CHIARO = [b for b in BLOCCHI if _porte(b) & {"80", "81"}]


def test_un_solo_server_tls_con_certificato():
    assert len(TLS) == 1
    (b,) = TLS
    assert re.search(r"^\s*listen\s+443\s+ssl\b", b, re.M)
    assert "include /etc/nginx/snippets/lanmng-tls.conf;" in b


def test_il_server_tls_serve_tutta_la_dashboard():
    (b,) = TLS
    for location in ("location / ", "location /ws ", "location /api/ ", "location = /health "):
        assert location in b, location


def test_il_server_tls_dichiara_lo_schema_al_backend():
    (b,) = TLS
    valori = re.findall(r"proxy_set_header\s+X-Forwarded-Proto\s+(\S+);", b)
    # Uno per /ws e uno per /api/: senza, il cookie perderebbe Secure.
    assert valori == ["$scheme", "$scheme"]


def test_80_e_81_rimandano_solo_all_https():
    porte = set().union(*(_porte(b) for b in IN_CHIARO))
    assert {"80", "81"} <= porte
    for b in IN_CHIARO:
        assert "proxy_pass" not in b and "root " not in b
        assert re.search(r"^\s*return\s+301\s+https://", b, re.M), b


def test_il_redirect_punta_al_nome_servito():
    (nome_tls,) = _nomi(TLS[0])
    for b in IN_CHIARO:
        assert _nomi(b) == [nome_tls]
        destinazione = re.search(r"return\s+301\s+https://([^$/;]+)", b).group(1)
        assert destinazione == nome_tls
