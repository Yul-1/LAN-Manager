"""
services/nettools.py — Strumenti di rete eseguiti dal backend
=============================================================
ping, traceroute, dig, nmap e verifica di una porta TCP, lanciati dal
container (che vede la LAN in host-network) e restituiti come output grezzo.

Regole di sicurezza, tutte applicate qui e non nel router:
  - **mai una shell**: `create_subprocess_exec` con lista di argomenti, quindi
    non esistono metacaratteri da neutralizzare;
  - il bersaglio passa da `validate_target`, che accetta solo host/IP/CIDR e
    quindi non puo' diventare un'opzione (niente `-oN /etc/passwd`);
  - `target_blocked` esclude loopback, link-local (169.254.169.254 dei metadata
    cloud), multicast e riservati: gli IP privati restano ammessi, sono lo scopo
    dello strumento;
  - ogni tool ha un timeout e l'output e' troncato.
"""
from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
import time
from typing import AsyncIterator

from config import settings

log = logging.getLogger("nettools")

MAX_OUTPUT = 64 * 1024        # oltre non serve a nessuno, e non intasa il browser

# Tetti di tempo dei tool scritti in Python. Stanno qui, in un posto solo,
# perche' li usano sia l'implementazione sia `timeout_massimo`, che dice alla
# UI quanto puo' durare l'attesa: due numeri diversi farebbero promettere alla
# pagina un'attesa che non e' quella vera.
WHOIS_TIMEOUT = 8.0           # per interrogazione: se ne fanno due (rimando)
HTTP_TIMEOUT_MAX = 30
PORT_TIMEOUT_MAX = 15
SPEEDTEST_TIMEOUT = 60

# Host, IPv4/IPv6, con eventuale prefisso CIDR. Deve iniziare per lettera o
# cifra: cosi' un bersaglio non puo' mai essere letto come opzione da un tool.
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,253}(/\d{1,3})?$")

# Nome di interfaccia: lettere, cifre, punto, due punti, trattino (eth0, br-lan,
# eth0.10). Come per il bersaglio, non puo' diventare un'opzione di arping.
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,15}$")

# Tipi di record consentiti a dig (evita che il campo diventi un'opzione).
_DNS_TYPES = {"A", "AAAA", "PTR", "MX", "TXT", "NS", "CNAME", "SOA", "SRV"}

TOOLS = ("ping", "traceroute", "dig", "nmap", "port", "arping", "whois", "http",
         "speedtest")

# Tool che scrivono man mano (sono processi esterni): il loro output si puo'
# mandare alla pagina mentre esce. Gli altri sono scritti in Python e un
# risultato parziale non esiste (whois legge la risposta intera, la misura di
# velocita' ha senso solo a fine download): restano one-shot, e dirlo nel
# catalogo evita che la UI prometta uno streaming che non c'e'.
STREAM_TOOLS = ("ping", "traceroute", "dig", "nmap", "arping")

# Metodi HTTP ammessi dal tool "http": solo quelli che leggono, mai uno che
# scrive. Lo strumento serve a guardare, e da qui passa qualunque URL.
_HTTP_METHODS = ("GET", "HEAD")


class ToolError(ValueError):
    """Input rifiutato: il router la traduce in 400."""


def validate_target(target: str) -> str:
    target = (target or "").strip()
    if not target:
        raise ToolError("indica un host, un IP o una subnet")
    if not _TARGET_RE.match(target):
        raise ToolError("bersaglio non valido: sono ammessi host, IP o CIDR")
    return target


async def target_blocked(host: str) -> bool:
    """True se l'host risolve (anche) a un indirizzo speciale non consentito
    come destinazione: loopback, link-local (incluso il metadata cloud
    169.254.169.254), multicast, reserved, unspecified. Gli IP privati della LAN
    restano consentiti: monitorarli e' lo scopo del tool. Mitiga l'SSRF verso
    servizi solo-locali."""
    if not host:
        return False
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except Exception:
        return False  # non risolvibile: lascia fallire il probe normalmente
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified):
            return True
    return False


def _host_only(target: str) -> str:
    """Host da controllare con target_blocked: il CIDR si riduce all'indirizzo."""
    return target.split("/", 1)[0]


# ── Costruzione dei comandi ────────────────────────────────────────
#  Le opzioni sono fisse: dall'esterno arriva solo il bersaglio (validato) e
#  pochi parametri numerici o presi da un elenco chiuso.

def _argv(tool: str, target: str, opts: dict) -> tuple[list[str], int]:
    """(argomenti, timeout in secondi) per il tool richiesto."""
    if tool == "ping":
        count = min(max(int(opts.get("count", 4)), 1), 10)
        return ["ping", "-n", "-c", str(count), "-W", "2", target], count * 3 + 5
    if tool == "traceroute":
        hops = min(max(int(opts.get("max_hops", 20)), 1), 30)
        return ["traceroute", "-n", "-w", "2", "-q", "1", "-m", str(hops), target], hops * 3 + 10
    if tool == "dig":
        rtype = str(opts.get("type", "A")).upper()
        if rtype not in _DNS_TYPES:
            raise ToolError(f"tipo DNS non consentito (ammessi: {', '.join(sorted(_DNS_TYPES))})")
        args = ["dig", "+time=3", "+tries=1"]
        # Su un IP la domanda sensata e' il reverse: dig -x lo formula da solo.
        if rtype == "PTR" or _is_ip(target):
            return args + ["-x", target], 15
        return args + [target, rtype], 15
    if tool == "arping":
        count = min(max(int(opts.get("count", 3)), 1), 10)
        # -f no: si vogliono tutte le risposte, non solo la prima. -w e' il
        # tetto complessivo, perche' su un IP spento arping aspetterebbe a vuoto.
        args = ["arping", "-c", str(count), "-w", str(count * 2)]
        iface = str(opts.get("iface", "") or "").strip()
        if iface:
            if not _IFACE_RE.match(iface):
                raise ToolError("nome interfaccia non valido")
            args += ["-I", iface]
        return args + [target], count * 2 + 5
    if tool == "nmap":
        args = ["nmap", "-Pn", "-n", "-T4", "--top-ports", "50"]
        if settings.discovery.nmap.privileged:
            args.insert(1, "--privileged")
        if opts.get("services"):
            args.append("-sV")
        return args + [target], 180
    raise ToolError(f"tool sconosciuto: {tool}")


# Opzioni piu' costose che la UI puo' mandare: servono a calcolare quanto puo'
# durare un tool, senza riscrivere quei numeri una seconda volta.
_OPZIONI_PEGGIORI = {"ping": {"count": 10}, "traceroute": {"max_hops": 30},
                     "dig": {}, "nmap": {"services": True}, "arping": {"count": 10}}


def timeout_massimo(tool: str) -> int:
    """Attesa massima possibile per un tool, in secondi.

    La pagina la mostra mentre l'esecuzione e' in corso: davanti a un nmap che
    puo' durare tre minuti, "in corso…" senza un orizzonte sembra un blocco.
    """
    if tool in _OPZIONI_PEGGIORI:
        return _argv(tool, "x", _OPZIONI_PEGGIORI[tool])[1]
    return {"port": PORT_TIMEOUT_MAX,
            "whois": int(WHOIS_TIMEOUT * 2),
            "http": HTTP_TIMEOUT_MAX,
            "speedtest": SPEEDTEST_TIMEOUT}.get(tool, 30)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# ── Esecuzione ─────────────────────────────────────────────────────

async def run_tool(tool: str, target: str, opts: dict | None = None) -> dict:
    """Esegue un tool e ritorna l'output. Solleva ToolError su input rifiutato."""
    opts = opts or {}
    if tool not in TOOLS:
        raise ToolError(f"tool sconosciuto: {tool}")

    # Due tool non hanno un "bersaglio" nella forma host/IP: la misura di
    # velocita' non ne ha affatto (l'indirizzo sta in configurazione) e HTTP
    # vuole un URL intero. Validano per conto loro.
    if tool == "speedtest":
        return await _speedtest(opts)
    if tool == "http":
        return await _http(target, opts)

    target = validate_target(target)
    if await target_blocked(_host_only(target)):
        raise ToolError("bersaglio non consentito (indirizzo speciale o locale)")

    if tool == "port":
        return await _tcp_port(target, opts)
    if tool == "whois":
        return await _whois(target, opts)

    args, timeout = _argv(tool, target, opts)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise ToolError(f"{args[0]} non e' installato nell'immagine")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        code, timed_out = None, True

    text = (out or b"").decode(errors="replace")
    truncated = len(text) > MAX_OUTPUT
    if truncated:
        text = text[:MAX_OUTPUT] + "\n… output troncato …"
    if timed_out:
        text += f"\n… interrotto dopo {timeout}s …"
    return {
        "tool": tool,
        "target": target,
        "command": " ".join(args),
        "output": text,
        "exit_code": code,
        "timed_out": timed_out,
        "truncated": truncated,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


# ── Esecuzione in diretta ──────────────────────────────────────────
#  Stesse regole di run_tool (nessuna shell, bersaglio validato, timeout,
#  troncamento): cambia solo che l'output esce mentre il processo scrive,
#  invece che tutto insieme alla fine. Un traceroute lungo o un nmap da tre
#  minuti mostravano una schermata vuota fino all'ultimo secondo.

CHUNK = 4096          # quanto si legge per volta dalla pipe del processo


async def _termina(proc) -> None:
    """Uccide il processo e ne raccoglie l'esito, tollerando che sia gia' morto.

    L'ordine conta: il segnale si manda **per primo** e senza await, perche'
    questa funzione viene chiamata anche mentre il task e' in cancellazione
    (browser scollegato). Se la raccolta dopo viene interrotta, pazienza: il
    processo e' comunque gia' condannato.
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return                       # finito da solo fra il controllo e il segnale
    except Exception as e:
        log.debug(f"kill del processo: {e}")
        return
    try:
        await proc.wait()
    except Exception as e:
        log.debug(f"attesa del processo dopo kill: {e}")


async def run_tool_stream(tool: str, target: str,
                          opts: dict | None = None) -> AsyncIterator[dict]:
    """Esegue un tool emettendo eventi: `start`, poi `out` a ogni pezzo di
    output, infine `end` con l'esito.

    Tutta la validazione avviene **prima** del primo `start`: chi consuma il
    generatore puo' quindi intercettare `ToolError` sul primo `__anext__()` e
    rispondere con un 400 vero, invece che con un errore infilato dentro un
    corpo gia' iniziato.
    """
    opts = opts or {}
    if tool not in STREAM_TOOLS:
        raise ToolError(f"{tool} non produce output progressivo")

    target = validate_target(target)
    if await target_blocked(_host_only(target)):
        raise ToolError("bersaglio non consentito (indirizzo speciale o locale)")
    args, timeout = _argv(tool, target, opts)

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise ToolError(f"{args[0]} non e' installato nell'immagine")

    # I chunk tagliano i caratteri multibyte a meta': un decoder incrementale
    # tiene il pezzo in sospeso invece di sostituirlo con un carattere rotto.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    scadenza = started + timeout
    inviati, troncato, scaduto = 0, False, False
    # Il `try` comincia subito dopo la creazione del processo, e il primo evento
    # sta dentro: chiudere il generatore mentre e' fermo sullo `start` (la pagina
    # chiusa appena partito il comando) deve comunque passare dal `finally`.
    try:
        yield {"type": "start", "tool": tool, "target": target,
               "command": " ".join(args), "timeout": timeout}
        while True:
            rimasto = scadenza - time.monotonic()
            if rimasto <= 0:
                scaduto = True
                break
            try:
                pezzo = await asyncio.wait_for(proc.stdout.read(CHUNK), timeout=rimasto)
            except asyncio.TimeoutError:
                scaduto = True
                break
            if not pezzo:
                break
            testo = decoder.decode(pezzo)
            if not testo:
                continue
            if inviati + len(testo) > MAX_OUTPUT:
                testo = testo[:max(MAX_OUTPUT - inviati, 0)]
                troncato = True
            if testo:
                inviati += len(testo)
                yield {"type": "out", "text": testo}
            if troncato:
                break

        if scaduto or troncato:
            await _termina(proc)
            code = None
        else:
            resto = decoder.decode(b"", True)
            if resto:
                yield {"type": "out", "text": resto}
            code = await proc.wait()

        if scaduto:
            yield {"type": "out", "text": f"\n… interrotto dopo {timeout}s …"}
        elif troncato:
            yield {"type": "out", "text": "\n… output troncato …"}

        yield {"type": "end", "tool": tool, "target": target,
               "command": " ".join(args), "exit_code": code, "timed_out": scaduto,
               "truncated": troncato,
               "duration_ms": round((time.monotonic() - started) * 1000)}
    finally:
        # Chi legge se n'e' andato (pagina chiusa, cambio strumento): senza
        # questo un nmap continuerebbe a girare per tre minuti per nessuno.
        await _termina(proc)


async def _tcp_port(target: str, opts: dict) -> dict:
    """Connessione TCP: dice se la porta accetta connessioni, senza subprocess."""
    try:
        port = int(opts.get("port", 0))
    except (TypeError, ValueError):
        raise ToolError("porta non valida")
    if not 1 <= port <= 65535:
        raise ToolError("porta fuori intervallo (1-65535)")
    timeout = min(max(float(opts.get("timeout", 3)), 1), PORT_TIMEOUT_MAX)

    started = time.monotonic()
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(target, port), timeout)
        ok, detail = True, "porta aperta"
    except asyncio.TimeoutError:
        ok, detail = False, f"nessuna risposta entro {timeout:g}s (filtrata?)"
    except OSError as e:
        ok, detail = False, f"connessione rifiutata ({e.strerror or e})"
    finally:
        if writer:
            writer.close()
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        "tool": "port",
        "target": target,
        "command": f"tcp connect {target}:{port}",
        "output": f"{target}:{port} — {detail} ({elapsed} ms)",
        "exit_code": 0 if ok else 1,
        "timed_out": False,
        "truncated": False,
        "duration_ms": elapsed,
    }


def _esito(tool: str, target: str, comando: str, output: str, ok: bool,
           ms: int, troncato: bool = False) -> dict:
    """Stessa forma dei tool eseguiti come processo: la UI ne conosce una sola."""
    return {
        "tool": tool, "target": target, "command": comando,
        "output": output[:MAX_OUTPUT] + ("\n… output troncato …" if len(output) > MAX_OUTPUT else ""),
        "exit_code": 0 if ok else 1,
        "timed_out": False,
        "truncated": troncato or len(output) > MAX_OUTPUT,
        "duration_ms": ms,
    }


# ── whois ──────────────────────────────────────────────────────────
#  Protocollo whois (RFC 3912): si apre una TCP sulla 43, si manda la domanda
#  seguita da CRLF e si legge finche' il server chiude. Nessun pacchetto da
#  installare nell'immagine: sarebbero 3 MB per fare questo.

async def _whois_query(server: str, domanda: str, timeout: float = WHOIS_TIMEOUT) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(server, 43), timeout)
    try:
        writer.write((domanda + "\r\n").encode())
        await writer.drain()
        dati = await asyncio.wait_for(reader.read(MAX_OUTPUT), timeout)
    finally:
        writer.close()
    return dati.decode(errors="replace")


def _whois_rimando(risposta: str) -> str:
    """Server a cui il registro rimanda (`refer:` per gli IP, `whois:` per i
    TLD). Senza seguirlo, di un dominio si otterrebbe solo il nome del registro
    e mai i dati veri."""
    for riga in risposta.splitlines():
        chiave, _, valore = riga.partition(":")
        if chiave.strip().lower() in ("refer", "whois") and valore.strip():
            v = valore.strip()
            return v if _TARGET_RE.match(v) else ""
    return ""


async def _whois(target: str, opts: dict) -> dict:
    started = time.monotonic()
    if _is_ip(target):
        addr = ipaddress.ip_address(target)
        if addr.is_private:
            return _esito("whois", target, f"whois {target}",
                          f"{target} e' un indirizzo privato: non esiste un registro "
                          f"pubblico che dica di chi e'. Il whois ha senso sugli "
                          f"indirizzi che si vedono da internet.", False,
                          round((time.monotonic() - started) * 1000))
    partenza = settings.tools.whois_server or "whois.iana.org"
    pezzi = []
    try:
        prima = await _whois_query(partenza, target)
    except (OSError, asyncio.TimeoutError) as e:
        return _esito("whois", target, f"whois -h {partenza} {target}",
                      f"{partenza} non risponde: {e}", False,
                      round((time.monotonic() - started) * 1000))
    pezzi.append(f"===== {partenza} =====\n{prima.strip()}")
    rimando = _whois_rimando(prima)
    if rimando and rimando != partenza:
        try:
            seconda = await _whois_query(rimando, target)
            pezzi.append(f"\n===== {rimando} =====\n{seconda.strip()}")
        except (OSError, asyncio.TimeoutError) as e:
            pezzi.append(f"\n===== {rimando} =====\n(non risponde: {e})")
    return _esito("whois", target,
                  f"whois -h {partenza} {target}" + (f" → {rimando}" if rimando else ""),
                  "\n".join(pezzi), True, round((time.monotonic() - started) * 1000))


# ── HTTP (quello che si farebbe con curl) ──────────────────────────

def _url_valido(target: str) -> str:
    """URL normalizzato. Senza schema si assume http://, come fa curl."""
    from urllib.parse import urlsplit

    grezzo = (target or "").strip()
    if not grezzo:
        raise ToolError("indica un URL (o un host)")
    if "://" not in grezzo:
        grezzo = "http://" + grezzo
    parti = urlsplit(grezzo)
    if parti.scheme not in ("http", "https"):
        raise ToolError("sono ammessi solo http:// e https://")
    if not parti.hostname:
        raise ToolError("URL senza host")
    if not _TARGET_RE.match(parti.hostname):
        raise ToolError("host non valido nell'URL")
    return grezzo


async def _http(target: str, opts: dict) -> dict:
    from urllib.parse import urlsplit

    import httpx

    url = _url_valido(target)
    host = urlsplit(url).hostname or ""
    if await target_blocked(host):
        raise ToolError("bersaglio non consentito (indirizzo speciale o locale)")
    metodo = str(opts.get("method", "GET")).upper()
    if metodo not in _HTTP_METHODS:
        raise ToolError(f"metodo non consentito (ammessi: {', '.join(_HTTP_METHODS)})")
    segui = bool(opts.get("follow", True))
    timeout = min(max(float(opts.get("timeout", 10)), 1), HTTP_TIMEOUT_MAX)

    started = time.monotonic()
    righe: list[str] = []
    try:
        async with httpx.AsyncClient(follow_redirects=segui, timeout=timeout,
                                     verify=bool(opts.get("verify", True))) as client:
            r = await client.request(metodo, url)
    except httpx.HTTPError as e:
        return _esito("http", url, f"{metodo} {url}",
                      f"richiesta fallita: {e.__class__.__name__}: {e}", False,
                      round((time.monotonic() - started) * 1000))
    ms = round((time.monotonic() - started) * 1000)
    # La catena di redirect e' la meta' della risposta a "perche' non funziona":
    # senza, un 200 finale nasconde tre salti e magari un downgrade a http.
    for passo in r.history:
        righe.append(f"{passo.status_code} {passo.reason_phrase}  {passo.request.url}"
                     f"  →  {passo.headers.get('location', '')}")
    righe.append(f"{r.status_code} {r.reason_phrase}  {r.request.url}")
    righe.append("")
    for k in ("server", "content-type", "content-length", "location", "cache-control"):
        if k in r.headers:
            righe.append(f"{k}: {r.headers[k]}")
    corpo = ""
    if metodo == "GET":
        testo = r.text if "text" in r.headers.get("content-type", "") \
            or "json" in r.headers.get("content-type", "") else ""
        corpo = testo[:2000]
        if corpo:
            righe.append("")
            righe.append("--- primi 2000 caratteri ---")
            righe.append(corpo)
        elif r.content:
            righe.append("")
            righe.append(f"(corpo non testuale, {len(r.content)} byte)")
    return _esito("http", url, f"{metodo} {url}", "\n".join(righe),
                  r.status_code < 400, ms)


# ── Misura di velocita' ────────────────────────────────────────────

async def _speedtest(opts: dict) -> dict:
    """Scarica N MB dall'indirizzo in configurazione e misura quanto ci mette.

    Si misura dal container, quindi il percorso e' lo stesso del traffico di
    casa: LAN, router, WAN. E si dichiara quanto traffico e' costato: su una
    WAN a consumo (qui e' un 4G) non e' un dettaglio.
    """
    import httpx

    cfg = settings.tools
    tetto = max(int(cfg.speedtest_max_mb or 50), 1)
    try:
        mb = int(opts.get("mb", cfg.speedtest_mb or 5))
    except (TypeError, ValueError):
        raise ToolError("quantita' non valida")
    mb = min(max(mb, 1), tetto)
    modello = cfg.speedtest_url or ""
    if not modello:
        raise ToolError("nessun indirizzo per la misura: impostalo in tools.speedtest_url")
    url = modello.replace("{bytes}", str(mb * 1_000_000)).replace("{mb}", str(mb))

    started = time.monotonic()
    scaricati, primo_byte = 0, None
    try:
        async with httpx.AsyncClient(timeout=SPEEDTEST_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    return _esito("speedtest", url, f"GET {url}",
                                  f"il server ha risposto {r.status_code}", False,
                                  round((time.monotonic() - started) * 1000))
                async for chunk in r.aiter_bytes(65536):
                    if primo_byte is None:
                        primo_byte = time.monotonic()
                    scaricati += len(chunk)      # si butta: serve solo il tempo
    except httpx.HTTPError as e:
        return _esito("speedtest", url, f"GET {url}",
                      f"misura interrotta dopo {scaricati / 1e6:.1f} MB: "
                      f"{e.__class__.__name__}: {e}", False,
                      round((time.monotonic() - started) * 1000))
    fine = time.monotonic()
    ms = round((fine - started) * 1000)
    # Il tempo si conta dal primo byte: prima ci sono DNS, TCP e TLS, che sono
    # latenza, non banda. Contarli dentro farebbe sembrare la linea piu' lenta.
    secondi = max(fine - (primo_byte or started), 1e-6)
    mbit = scaricati * 8 / 1e6 / secondi
    ttfb = round(((primo_byte or fine) - started) * 1000)
    righe = [
        f"scaricati {scaricati / 1e6:.1f} MB in {secondi:.2f}s",
        f"velocita' in discesa: {mbit:.1f} Mbit/s",
        f"attesa prima del primo byte: {ttfb} ms (DNS + TCP + TLS, non e' banda)",
        "",
        f"sorgente: {url}",
        "misura fatta dal container, quindi per la stessa strada del traffico di",
        f"casa (LAN, router, WAN). Costa traffico: questa misura ne ha usati "
        f"{scaricati / 1e6:.1f} MB.",
    ]
    return _esito("speedtest", url, f"GET {url}", "\n".join(righe), scaricati > 0, ms)
