# LANMng backend — immagine di produzione
# Build dal root del repo:  docker build -t lanmng-backend -f backend/Dockerfile .
#
# Base image fissata per digest: il tag e' mutabile e una build futura potrebbe
# tirare giu' un'immagine diversa senza che nulla lo registri. Aggiornarla e'
# quindi una scelta esplicita (vedi README, manutenzione periodica):
#   docker buildx imagetools inspect python:3.11-slim-bookworm
FROM python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91

# Con rootfs read-only Python tenterebbe di scrivere __pycache__ accanto ai
# sorgenti ad ogni import; unbuffered serve perche' i log arrivino allo stdout
# subito, senza perdere righe se il container viene ucciso (limite di memoria).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dipendenze di sistema dei provider discovery (nmap, snmp) e tool di rete.
#
# nmap come non-root: cap_add nel compose popola solo il bounding set, non
# rende le capability effettive per un utente non-root (Docker non imposta le
# ambient cap). Le file-capability sul binario colmano il gap: con NET_RAW/
# NET_ADMIN nel bounding set, nmap puo' creare raw socket anche da non-root.
#
# Il setcap sta nello STESSO layer dell'install: gli xattr di un file che vive
# in un layer sottostante costringono OverlayFS a duplicare l'intero binario nel
# layer nuovo (2,8MB per nmap), e nel deploy senza registry viaggia proprio il
# tar di docker save. libcap2-bin resta installato: iputils-ping ci dipende
# (il suo postinst usa setcap per dare cap_net_raw a ping) e disinstallarlo
# porterebbe via il ping, cioe' il ping sweep. Misurato: senza, il container
# non ha piu' /usr/bin/ping.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nmap \
        snmp \
        iputils-ping \
        iputils-arping \
        traceroute \
        iproute2 \
        dnsutils \
        openssh-client \
        libcap2-bin \
        ca-certificates \
    && setcap cap_net_raw,cap_net_admin+eip /usr/bin/nmap \
    && setcap cap_net_raw+ep /usr/bin/arping \
    && rm -rf /var/lib/apt/lists/*

# Utente applicativo non privilegiato (uid/gid 1000 = primo utente tipico
# dell'host: semplifica i permessi sulla cartella config montata in rw).
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

# Cartella del socket del backend. Nello scenario generico ci si monta sopra un
# volume con nome, che al primo uso eredita proprietario e permessi da qui:
# senza, sarebbe di root e appuser non potrebbe creare il socket.
RUN mkdir -p /run/lanmng && chown appuser:appuser /run/lanmng && chmod 0755 /run/lanmng

WORKDIR /app

# Prima le dipendenze (layer cache), poi il codice.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Codice applicativo. La config reale e i segreti NON entrano nell'immagine:
# vengono montati a runtime su /app/config.
COPY backend/ /app/backend/
COPY VERSION /app/VERSION

USER appuser

# Nessuna porta: il backend ascolta solo sul socket (vedi CMD).

# Healthcheck senza dipendenze extra (curl non installato): GET /health sul
# socket con la sola stdlib, vedi backend/healthcheck.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python /app/backend/healthcheck.py || exit 1

# Unix socket e non una porta su loopback: con network_mode host quella porta la
# raggiunge qualunque processo dell'host, e ognuno poteva dichiarare l'IP client
# che voleva con X-Forwarded-For (pentest 2026-09-23, F9). Il socket sta in una
# cartella che raggiungono solo appuser e nginx (deploy/lanmng.tmpfiles.conf).
#
# --forwarded-allow-ips "*" e' obbligato: su unix socket uvicorn non conosce
# l'indirizzo del proxy e con una lista di IP non applicherebbe mai gli header.
# Il prezzo e' che con "*" uvicorn prende la voce piu' a SINISTRA di
# X-Forwarded-For: nginx deve quindi impostarlo a $remote_addr e mai accodare
# ($proxy_add_x_forwarded_for), altrimenti l'IP lo sceglie il client.
CMD ["uvicorn", "main:app", "--app-dir", "/app/backend", "--uds", "/run/lanmng/backend.sock", "--proxy-headers", "--forwarded-allow-ips", "*"]
