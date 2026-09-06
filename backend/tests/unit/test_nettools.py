"""
Tool di rete (services/nettools.py).

Superficie sensibile: il bersaglio arriva dall'esterno e finisce in una riga di
comando. Non c'e' shell (i tool si lanciano con argv), ma il bersaglio non deve
comunque poter diventare un'**opzione** del tool, e i parametri numerici devono
restare dentro limiti sensati.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

from config import settings
from services import nettools
from services.nettools import ToolError, _argv, _is_ip, _host_only, target_blocked, validate_target


# ── validate_target ────────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "192.0.2.1", "example.com", "host-01.lan", "192.0.2.0/24",
    "2001:db8::1", "a", "10.0.0.1/8",
])
def test_bersagli_legittimi_accettati(target):
    assert validate_target(target) == target


@pytest.mark.parametrize("target", [
    "-oX /tmp/out",           # opzione travestita da bersaglio
    "--script=http-shellshock",
    "-192.0.2.1",
    "192.0.2.1; rm -rf /",    # separatore di comando
    "192.0.2.1 | cat /etc/passwd",
    "$(whoami)",
    "`id`",
    "192.0.2.1&&id",
    "host con spazi",
    "host\nnewline",
    "", "   ",
])
def test_bersagli_pericolosi_o_malformati_respinti(target):
    with pytest.raises(ToolError):
        validate_target(target)


def test_il_bersaglio_deve_iniziare_per_lettera_o_cifra():
    # E' la regola che impedisce a un bersaglio di essere letto come opzione.
    with pytest.raises(ToolError):
        validate_target("-x")
    with pytest.raises(ToolError):
        validate_target(".nascosto")


def test_un_bersaglio_troppo_lungo_viene_respinto():
    with pytest.raises(ToolError):
        validate_target("a" * 300)


def test_gli_spazi_ai_bordi_vengono_tolti():
    assert validate_target("  192.0.2.1  ") == "192.0.2.1"


def test_host_only_riduce_il_cidr_allindirizzo():
    assert _host_only("192.0.2.0/24") == "192.0.2.0"
    assert _host_only("example.com") == "example.com"


def test_riconoscimento_degli_ip():
    assert _is_ip("192.0.2.1") and _is_ip("2001:db8::1")
    assert not _is_ip("example.com") and not _is_ip("192.0.2.0/24")


# ── _argv: il bersaglio resta un argomento, mai un flag ────────────

def test_il_bersaglio_e_sempre_lultimo_argomento():
    for tool, opts in (("ping", {}), ("traceroute", {}), ("nmap", {})):
        args, _ = _argv(tool, "192.0.2.1", opts)
        assert args[-1] == "192.0.2.1"


def test_il_conteggio_dei_ping_e_limitato():
    assert _argv("ping", "192.0.2.1", {"count": 1})[0][3] == "1"
    assert _argv("ping", "192.0.2.1", {"count": 999})[0][3] == "10"
    assert _argv("ping", "192.0.2.1", {"count": 0})[0][3] == "1"
    assert _argv("ping", "192.0.2.1", {"count": -5})[0][3] == "1"


def test_gli_hop_del_traceroute_sono_limitati():
    args, _ = _argv("traceroute", "192.0.2.1", {"max_hops": 999})
    assert args[args.index("-m") + 1] == "30"
    args, _ = _argv("traceroute", "192.0.2.1", {"max_hops": 0})
    assert args[args.index("-m") + 1] == "1"


def test_il_timeout_cresce_col_lavoro_richiesto():
    _, t1 = _argv("ping", "192.0.2.1", {"count": 1})
    _, t10 = _argv("ping", "192.0.2.1", {"count": 10})
    assert t10 > t1


def test_dig_accetta_solo_i_tipi_in_elenco():
    args, _ = _argv("dig", "example.com", {"type": "MX"})
    assert args[-2:] == ["example.com", "MX"]
    for cattivo in ("ANY", "AXFR", "-x /etc/passwd", "', 'evil"):
        with pytest.raises(ToolError):
            _argv("dig", "example.com", {"type": cattivo})


def test_dig_su_un_ip_diventa_una_reverse():
    args, _ = _argv("dig", "192.0.2.1", {"type": "A"})
    assert "-x" in args and args[-1] == "192.0.2.1"
    args, _ = _argv("dig", "example.com", {"type": "PTR"})
    assert "-x" in args


def test_nmap_aggiunge_privileged_solo_se_configurato(monkeypatch):
    monkeypatch.setattr(settings.discovery.nmap, "privileged", True)
    assert "--privileged" in _argv("nmap", "192.0.2.1", {})[0]
    monkeypatch.setattr(settings.discovery.nmap, "privileged", False)
    assert "--privileged" not in _argv("nmap", "192.0.2.1", {})[0]


def test_nmap_aggiunge_sv_solo_su_richiesta():
    assert "-sV" in _argv("nmap", "192.0.2.1", {"services": True})[0]
    assert "-sV" not in _argv("nmap", "192.0.2.1", {})[0]


def test_un_tool_sconosciuto_viene_respinto():
    with pytest.raises(ToolError, match="tool sconosciuto"):
        _argv("rm", "192.0.2.1", {})


# ── target_blocked: mitigazione SSRF ───────────────────────────────

def _risolve_a(*indirizzi):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ind, 0)) for ind in indirizzi]
    return fake


@pytest.mark.parametrize("indirizzo", [
    "127.0.0.1",            # loopback
    "169.254.169.254",      # metadata cloud
    "224.0.0.1",            # multicast
    "0.0.0.0",              # unspecified
    "240.0.0.1",            # reserved
])
async def test_gli_indirizzi_speciali_sono_bloccati(monkeypatch, indirizzo):
    monkeypatch.setattr(nettools.socket, "getaddrinfo", _risolve_a(indirizzo))
    assert await target_blocked("qualsiasi.host") is True


async def test_gli_indirizzi_privati_della_lan_restano_consentiti(monkeypatch):
    # Monitorarli e' lo scopo del tool: bloccarli lo renderebbe inutile.
    monkeypatch.setattr(nettools.socket, "getaddrinfo", _risolve_a("192.168.1.10"))
    assert await target_blocked("nas.lan") is False


async def test_un_indirizzo_pubblico_non_e_bloccato(monkeypatch):
    monkeypatch.setattr(nettools.socket, "getaddrinfo", _risolve_a("93.184.216.34"))
    assert await target_blocked("example.com") is False


async def test_basta_un_indirizzo_speciale_fra_i_risolti(monkeypatch):
    # Un nome che risolve sia a un pubblico sia a loopback va bloccato lo stesso.
    monkeypatch.setattr(nettools.socket, "getaddrinfo",
                        _risolve_a("93.184.216.34", "127.0.0.1"))
    assert await target_blocked("doppio.example") is True


async def test_un_host_non_risolvibile_non_viene_bloccato(monkeypatch):
    def esplode(*a, **k):
        raise socket.gaierror("Name or service not known")
    monkeypatch.setattr(nettools.socket, "getaddrinfo", esplode)
    assert await target_blocked("non-esiste.invalid") is False


async def test_host_vuoto():
    assert await target_blocked("") is False


# ── Tool aggiunti su richiesta del proprietario (2026-09-03) ───────
#  whois, HTTP (quello che si farebbe con curl), misura di velocita' e ARP ping.

from services.nettools import (_esito, _http, _speedtest, _url_valido, _whois,
                               _whois_rimando, run_tool)


# arping

def test_arping_mette_il_bersaglio_in_fondo_e_limita_i_pacchetti():
    args, timeout = _argv("arping", "192.0.2.9", {"count": 99})
    assert args[0] == "arping" and args[-1] == "192.0.2.9"
    assert "10" in args, args                     # tetto a 10 pacchetti
    assert timeout > 10


def test_arping_accetta_uninterfaccia_solo_se_ha_la_forma_giusta():
    args, _ = _argv("arping", "192.0.2.9", {"iface": "br-lan"})
    assert args[-3:] == ["-I", "br-lan", "192.0.2.9"]
    with pytest.raises(ToolError):
        _argv("arping", "192.0.2.9", {"iface": "-oPwned"})


def test_arping_senza_interfaccia_non_passa_lopzione():
    args, _ = _argv("arping", "192.0.2.9", {})
    assert "-I" not in args


# whois

def test_il_rimando_del_whois_viene_seguito():
    risposta = "refer:        whois.ripe.net\n\ninetnum: 1.2.3.0 - 1.2.3.255\n"
    assert _whois_rimando(risposta) == "whois.ripe.net"
    assert _whois_rimando("whois:  whois.nic.it\n") == "whois.nic.it"
    assert _whois_rimando("niente da seguire\n") == ""


def test_un_rimando_malformato_non_diventa_un_bersaglio():
    # Il nome del server rimandato finisce in una connessione: deve avere la
    # stessa forma di un host, altrimenti si scarta.
    assert _whois_rimando("refer: ; rm -rf /\n") == ""


async def test_il_whois_di_un_ip_privato_lo_dice_invece_di_chiedere_in_giro():
    r = await _whois("192.0.2.1", {})
    assert r["exit_code"] == 1
    assert "privato" in r["output"]
    assert "registro" in r["output"]


# HTTP

@pytest.mark.parametrize("dato,atteso", [
    ("example.com", "http://example.com"),
    ("http://example.com/x", "http://example.com/x"),
    ("https://example.com", "https://example.com"),
])
def test_lurl_si_normalizza_come_farebbe_curl(dato, atteso):
    assert _url_valido(dato) == atteso


@pytest.mark.parametrize("dato", ["", "ftp://example.com", "file:///etc/passwd",
                                  "http://", "http://spazio nel nome/"])
def test_url_non_ammessi(dato):
    with pytest.raises(ToolError):
        _url_valido(dato)


async def test_http_rifiuta_i_metodi_che_scrivono():
    with pytest.raises(ToolError):
        await _http("http://example.com", {"method": "POST"})


async def test_http_non_va_verso_indirizzi_speciali(monkeypatch):
    monkeypatch.setattr(nettools, "target_blocked", _sempre_bloccato)
    with pytest.raises(ToolError):
        await _http("http://qualcosa.example", {})


async def _sempre_bloccato(host):
    return True


# Misura di velocita'

async def test_la_misura_di_velocita_non_accetta_piu_del_tetto(monkeypatch):
    visti = {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, metodo, url):
            visti["url"] = url
            return _Risposta()

    class _Risposta:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self, n):
            yield b"x" * 1000

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(settings.tools, "speedtest_url", "https://esempio/{bytes}")
    monkeypatch.setattr(settings.tools, "speedtest_max_mb", 20)
    r = await _speedtest({"mb": 999})
    assert visti["url"] == "https://esempio/20000000", "il tetto della config vince"
    assert "Mbit/s" in r["output"]
    # Quanto e' costato va detto: la WAN di casa e' a consumo.
    assert "traffico" in r["output"]


async def test_senza_indirizzo_configurato_la_misura_si_rifiuta(monkeypatch):
    monkeypatch.setattr(settings.tools, "speedtest_url", "")
    with pytest.raises(ToolError):
        await _speedtest({})


async def test_la_misura_di_velocita_non_vuole_un_bersaglio(monkeypatch):
    # `run_tool` non deve passare da validate_target: un campo vuoto e' giusto.
    async def finto(opts):
        return _esito("speedtest", "url", "GET url", "ok", True, 1)
    monkeypatch.setattr(nettools, "_speedtest", finto)
    r = await run_tool("speedtest", "", {})
    assert r["tool"] == "speedtest"


# ── Quanto puo' durare, e chi paga un input sbagliato ──────────────

def test_il_tempo_massimo_viene_dagli_stessi_timeout_dellesecuzione():
    from services.nettools import timeout_massimo

    # Non un numero riscritto a mano: e' quello che l'esecuzione userebbe con
    # le opzioni piu' costose ammesse.
    assert timeout_massimo("nmap") == _argv("nmap", "x", {"services": True})[1]
    assert timeout_massimo("ping") == _argv("ping", "x", {"count": 10})[1]
    assert timeout_massimo("arping") == _argv("arping", "x", {"count": 10})[1]
    # Anche i tool scritti in Python dichiarano il loro tetto.
    assert timeout_massimo("speedtest") >= 30
    assert timeout_massimo("whois") >= 10


def test_ogni_tool_dichiara_quanto_puo_durare():
    from services.nettools import TOOLS, timeout_massimo

    for t in TOOLS:
        assert timeout_massimo(t) > 0, t


def test_un_input_rifiutato_non_consuma_il_limite():
    # Dieci clic col campo vuoto bloccavano gli strumenti per un minuto.
    from services.ratelimit import RateLimiter

    rl = RateLimiter(max_hits=2, window_seconds=60)
    rl.hit("ip")
    rl.refund("ip")
    rl.hit("ip")
    rl.hit("ip")
    assert rl.allowed("ip") is False, "i colpi veri contano ancora"
    rl.refund("ip")
    assert rl.allowed("ip") is True


def test_il_rimborso_su_una_chiave_mai_vista_non_esplode():
    from services.ratelimit import RateLimiter

    RateLimiter(max_hits=1, window_seconds=60).refund("mai-visto")


# ── Esecuzione in diretta (run_tool_stream) ────────────────────────
#  Il difetto che chiude: un traceroute lungo o un nmap da tre minuti
#  lasciavano la pagina vuota fino all'ultimo secondo, perche' l'output
#  usciva tutto insieme alla fine. Qui il processo e' finto (nessun comando
#  vero, nessuna rete): quello che si verifica e' che i pezzi escano man mano,
#  che la validazione stia PRIMA del primo evento e che il processo non
#  sopravviva a chi lo stava guardando.

class _PipeFinta:
    """stdout di un processo: consegna i pezzi uno per volta, poi EOF."""

    def __init__(self, pezzi):
        self._pezzi = list(pezzi)

    async def read(self, n):
        return self._pezzi.pop(0) if self._pezzi else b""


class _ProcessoFinto:
    def __init__(self, pezzi, code=0):
        self.stdout = _PipeFinta(pezzi)
        self.returncode = None
        self._code = code
        self.ucciso = False

    def kill(self):
        self.ucciso = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._code
        return self.returncode


def _senza_rete(monkeypatch, processo):
    """Niente DNS e niente processi veri: resta solo la logica dello streaming."""
    async def _mai_bloccato(host):
        return False

    async def _exec(*args, **kw):
        return processo

    monkeypatch.setattr(nettools, "target_blocked", _mai_bloccato)
    monkeypatch.setattr(nettools.asyncio, "create_subprocess_exec", _exec)


async def test_lo_streaming_manda_i_pezzi_man_mano(monkeypatch):
    proc = _ProcessoFinto([b"primo\n", b"secondo\n"])
    _senza_rete(monkeypatch, proc)
    eventi = [ev async for ev in nettools.run_tool_stream("ping", "192.0.2.1", {})]

    assert eventi[0]["type"] == "start"
    assert eventi[0]["command"].startswith("ping ")
    testi = [e["text"] for e in eventi if e["type"] == "out"]
    assert testi == ["primo\n", "secondo\n"], "due letture, due eventi: non uno solo alla fine"
    assert eventi[-1] == {"type": "end", "tool": "ping", "target": "192.0.2.1",
                          "command": eventi[0]["command"], "exit_code": 0,
                          "timed_out": False, "truncated": False,
                          "duration_ms": eventi[-1]["duration_ms"]}


async def test_un_carattere_a_cavallo_di_due_pezzi_non_si_rompe(monkeypatch):
    # "è" in UTF-8 sono due byte: se il chunk taglia in mezzo, un decode
    # ingenuo lo sostituisce con un carattere rotto che resta li' per sempre.
    proc = _ProcessoFinto([b"perch\xc3", b"\xa9\n"])
    _senza_rete(monkeypatch, proc)
    testo = "".join(ev["text"] for ev in
                    [e async for e in nettools.run_tool_stream("ping", "192.0.2.1", {})]
                    if ev["type"] == "out")
    assert testo == "perché\n"


async def test_la_validazione_sta_prima_del_primo_evento(monkeypatch):
    # Chi consuma deve poter rispondere 400: se l'errore uscisse dopo il primo
    # evento, il corpo della risposta sarebbe gia' cominciato.
    proc = _ProcessoFinto([b"x"])
    _senza_rete(monkeypatch, proc)
    gen = nettools.run_tool_stream("dig", "esempio.it", {"type": "PIPPO"})
    with pytest.raises(ToolError):
        await gen.__anext__()
    assert not proc.ucciso and proc.returncode is None, "nessun processo era stato avviato"


async def test_un_tool_senza_output_progressivo_non_si_puo_streammare():
    gen = nettools.run_tool_stream("whois", "esempio.it", {})
    with pytest.raises(ToolError):
        await gen.__anext__()


async def test_loutput_troppo_lungo_si_tronca_e_il_processo_muore(monkeypatch):
    proc = _ProcessoFinto([b"a" * (nettools.MAX_OUTPUT + 10), b"ancora"])
    _senza_rete(monkeypatch, proc)
    eventi = [ev async for ev in nettools.run_tool_stream("ping", "192.0.2.1", {})]
    testo = "".join(e["text"] for e in eventi if e["type"] == "out")
    assert len(testo) <= nettools.MAX_OUTPUT + len("\n… output troncato …")
    assert testo.endswith("… output troncato …")
    assert eventi[-1]["truncated"] is True
    assert proc.ucciso, "continuare a leggere un output che non si mostra e' lavoro per nessuno"


async def test_il_tempo_massimo_interrompe_e_lo_dichiara(monkeypatch):
    class _PipeMuta:
        async def read(self, n):
            await asyncio.sleep(3600)      # non arriva mai niente

    proc = _ProcessoFinto([])
    proc.stdout = _PipeMuta()
    _senza_rete(monkeypatch, proc)
    # Il tetto viene da _argv: qui lo si abbassa fingendo il comando.
    monkeypatch.setattr(nettools, "_argv", lambda t, b, o: (["ping", b], 0.05))
    eventi = [ev async for ev in nettools.run_tool_stream("ping", "192.0.2.1", {})]
    assert eventi[-1]["timed_out"] is True
    assert eventi[-1]["exit_code"] is None
    assert "interrotto dopo" in eventi[-2]["text"]
    assert proc.ucciso


async def test_chi_smette_di_guardare_non_lascia_il_processo_a_girare(monkeypatch):
    # Pagina chiusa a meta' di un nmap: senza il kill, il comando continua per
    # tre minuti per nessuno.
    class _PipeLenta:
        async def read(self, n):
            await asyncio.sleep(3600)

    proc = _ProcessoFinto([])
    proc.stdout = _PipeLenta()
    _senza_rete(monkeypatch, proc)
    gen = nettools.run_tool_stream("nmap", "192.0.2.0/24", {})
    primo = await gen.__anext__()
    assert primo["type"] == "start"
    await gen.aclose()
    assert proc.ucciso
