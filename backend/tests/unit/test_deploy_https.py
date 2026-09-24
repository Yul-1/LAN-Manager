"""
Vhost dell'nginx host in HTTPS (Fase 22).

Fino alla 0.9.x la dashboard si serviva in chiaro su :81: password di login e
cookie di sessione viaggiavano leggibili sulla LAN e sulla VPN (pentest
2026-09-23, F8). Ora l'unico server che fa da proxy al backend e' quello su
:443; :80 e :81 esistono solo per rimandare all'HTTPS.

Dal 2026-09-24 lo stesso server risponde anche su :81 in TLS, per IP: un PC
che non usa il DNS locale non risolveva il nome e, rimandato li' da :81,
restava fuori dalla dashboard.

I test tengono ferme le cose che una modifica distratta al vhost romperebbe
senza errori visibili: nessuna via in chiaro verso il backend, :81 in TLS con
il redirect dell'HTTP sulla stessa porta, il redirect di :80 verso il nome
servito, e `X-Forwarded-Proto` sul server TLS, da cui dipende il flag Secure
del cookie (routers/auth.py).
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


def _listen(blocco: str) -> list[tuple[str, bool]]:
    """(porta, ssl) per ogni `listen` del blocco, IPv4 e IPv6 insieme."""
    out = []
    for porta, resto in re.findall(r"^\s*listen\s+([^\s;]+)([^;]*);", blocco, re.M):
        out.append((re.sub(r"^\[::\]:", "", porta), "ssl" in resto.split()))
    return out


def _porte(blocco: str, ssl: bool) -> set[str]:
    return {p for p, s in _listen(blocco) if s == ssl}


def _nomi(blocco: str) -> list[str]:
    return re.findall(r"^\s*server_name\s+([^;]+);", blocco, re.M)


BLOCCHI = _server_blocks(VHOST.read_text())
TLS = [b for b in BLOCCHI if _porte(b, ssl=True)]
IN_CHIARO = [b for b in BLOCCHI if _porte(b, ssl=False)]


def test_un_solo_server_tls_con_certificato():
    assert len(TLS) == 1
    (b,) = TLS
    assert "include /etc/nginx/snippets/lanmng-tls.conf;" in b


def test_il_server_tls_ascolta_su_443_e_81_solo_in_tls():
    (b,) = TLS
    assert _porte(b, ssl=True) == {"443", "81"}
    assert not _porte(b, ssl=False)
    # Ogni porta sia in IPv4 sia in IPv6.
    assert len(_listen(b)) == 4


def test_http_in_chiaro_su_81_torna_in_https_sulla_stessa_porta():
    (b,) = TLS
    assert re.search(r"^\s*error_page\s+497\s+=301\s+https://\$host:\$server_port\$request_uri;",
                     b, re.M)


def test_nessuna_porta_81_in_chiaro():
    assert all("81" not in _porte(b, ssl=False) for b in BLOCCHI)


def test_il_server_tls_serve_tutta_la_dashboard():
    (b,) = TLS
    for location in ("location / ", "location /ws ", "location /api/ ", "location = /health "):
        assert location in b, location


def test_il_server_tls_dichiara_lo_schema_al_backend():
    (b,) = TLS
    valori = re.findall(r"proxy_set_header\s+X-Forwarded-Proto\s+(\S+);", b)
    # Uno per /ws e uno per /api/: senza, il cookie perderebbe Secure.
    assert valori == ["$scheme", "$scheme"]


def test_80_rimanda_solo_all_https_del_nome_servito():
    (nome_tls,) = _nomi(TLS[0])
    assert IN_CHIARO
    for b in IN_CHIARO:
        assert _porte(b, ssl=False) == {"80"}
        assert "proxy_pass" not in b and "root " not in b
        assert _nomi(b) == [nome_tls]
        destinazione = re.search(r"^\s*return\s+301\s+https://([^$/;]+)", b, re.M).group(1)
        assert destinazione == nome_tls
