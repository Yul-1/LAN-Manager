"""
Canale nginx -> backend su unix socket (pentest 2026-09-23, F9).

Con uvicorn su 127.0.0.1:8000 e `network_mode: host`, qualunque processo
dell'host poteva collegarsi al backend e dichiarare l'IP client che voleva
con `X-Forwarded-For`. Ora il backend ascolta solo su un unix socket in una
cartella che raggiungono soltanto l'uid del container e nginx.

La parte delicata e' la coppia uvicorn/nginx, ed e' quella che questi test
tengono ferma: su unix socket uvicorn non conosce l'indirizzo del proxy e va
detto di fidarsi di tutti (`--forwarded-allow-ips "*"`); cosi' pero' prende la
voce PIU' A SINISTRA di X-Forwarded-For, quindi nginx deve scriverci solo
`$remote_addr`. Con `$proxy_add_x_forwarded_for` (accoda a quello che manda il
client) basterebbe un header finto per scegliersi l'IP.
"""
from __future__ import annotations

import re
import shlex
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import healthcheck

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "backend" / "Dockerfile"
CONF_NGINX = [REPO / "deploy" / "lanmng.nginx.conf",
              REPO / "nginx" / "default.conf.template"]


def _cmd_uvicorn() -> list[str]:
    riga = next(r for r in DOCKERFILE.read_text().splitlines() if r.startswith("CMD "))
    import json
    return json.loads(riga[len("CMD "):])


def test_uvicorn_ascolta_solo_sul_socket():
    cmd = _cmd_uvicorn()
    assert cmd[cmd.index("--uds") + 1] == healthcheck.SOCKET
    assert "--port" not in cmd and "--host" not in cmd
    # Senza "*" gli header del proxy non si applicano mai (client = None) e
    # tutte le richieste risulterebbero da "unknown": un solo rate-limit per tutti.
    assert "--proxy-headers" in cmd
    assert cmd[cmd.index("--forwarded-allow-ips") + 1] == "*"


def test_healthcheck_del_dockerfile_usa_lo_script():
    testo = DOCKERFILE.read_text()
    assert "127.0.0.1:8000" not in testo
    assert "healthcheck.py" in testo


@pytest.mark.parametrize("conf", CONF_NGINX, ids=lambda p: p.name)
def test_nginx_non_accoda_x_forwarded_for(conf):
    testo = conf.read_text()
    valori = re.findall(r"proxy_set_header\s+X-Forwarded-For\s+(\S+);", testo)
    assert valori, f"{conf.name}: nessun X-Forwarded-For impostato"
    assert set(valori) == {"$remote_addr"}, f"{conf.name}: {valori}"


@pytest.mark.parametrize("conf", CONF_NGINX, ids=lambda p: p.name)
def test_nginx_parla_col_socket(conf):
    testo = conf.read_text()
    assert f"server unix:{healthcheck.SOCKET};" in testo
    assert "127.0.0.1:8000" not in testo
    destinazioni = set(re.findall(r"proxy_pass\s+(\S+);", testo))
    assert destinazioni == {"http://lanmng_backend"}


async def _client_visto(xff: str) -> tuple | None:
    visto = {}

    async def app(scope, receive, send):
        visto["client"] = scope.get("client")

    mw = ProxyHeadersMiddleware(app, trusted_hosts="*")
    # Su unix socket uvicorn non ha l'indirizzo del peer: client = None.
    await mw({"type": "http", "client": None, "scheme": "http",
              "headers": [(b"x-forwarded-for", xff.encode())]}, None, None)
    return visto["client"]


async def test_su_socket_l_ip_arriva_da_nginx():
    assert await _client_visto("192.0.2.7") == ("192.0.2.7", 0)


async def test_con_asterisco_vince_la_voce_piu_a_sinistra():
    # E' il motivo per cui nginx deve sovrascrivere l'header e non accodare:
    # "finto, vero" darebbe ragione al client.
    assert await _client_visto("203.0.113.9, 192.0.2.7") == ("203.0.113.9", 0)


# ── healthcheck.py ─────────────────────────────────────────────────


@pytest.fixture
def server_finto():
    """Unix socket che risponde una volta con la riga di stato data.

    In /tmp e non in tmp_path: il percorso di un unix socket ha un limite di
    ~108 byte e i tmp_path di pytest ci vanno vicino."""
    cartella = Path(tempfile.mkdtemp(prefix="lanmng-hc-"))
    percorso = str(cartella / "b.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(percorso)
    srv.listen(1)

    def avvia(stato: bytes):
        def servi():
            conn, _ = srv.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 " + stato + b"\r\ncontent-length: 0\r\n\r\n")
        threading.Thread(target=servi, daemon=True).start()
        return percorso

    yield avvia
    srv.close()
    Path(percorso).unlink(missing_ok=True)
    cartella.rmdir()


def test_healthcheck_sano_con_200(server_finto):
    assert healthcheck.sano(server_finto(b"200 OK")) is True


def test_healthcheck_malato_con_500(server_finto):
    assert healthcheck.sano(server_finto(b"500 Internal Server Error")) is False


def test_healthcheck_malato_senza_socket(tmp_path):
    assert healthcheck.sano(str(tmp_path / "manca.sock")) is False
