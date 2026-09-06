"""
Indirizzi tenuti fuori dal port scan (services/discovery.py).

Il port scan di LANMng apriva e chiudeva una connessione TCP verso la porta 22
di ognuno dei sei indirizzi del router, ad ogni giro: il 2026-09-03 erano 226
righe su 243 nel syslog del router in 50 minuti, contro 17 login SSH veri. Con
un buffer da 64 KB quel rumore spingeva fuori gli eventi veri in meno di un'ora.

Nasconderle in pagina non risolveva niente, perche' le righe il router le
scriveva lo stesso: si tolgono alla fonte. Quello che questi test difendono e'
il vincolo che rende la cosa accettabile — **il router deve restare fra i
dispositivi trovati**, perche' `--exclude` lo toglierebbe da tutto lo scan.
"""
from __future__ import annotations

import pytest

from config import settings
from services import discovery
from services.discovery import NmapProvider, fuori_dal_port_scan
from services.registry import DeviceRegistry

ROUTER = "192.0.2.1"          # come in tests/conftest.py


class FintoCollector:
    """Il collector che dichiara le interfacce del router, senza raccogliere."""

    def __init__(self, interfaces):
        self._snap = {"interfaces": interfaces}

    def snapshot_ora(self):
        return self._snap


@pytest.fixture
def router_con_sei_indirizzi(monkeypatch):
    from services import collector as mod_collector
    finto = FintoCollector([
        {"name": "lan", "ip4": ["192.0.2.1", "192.0.2.129"]},
        {"name": "guest", "ip4": ["198.51.100.1"]},
        {"name": "wan", "ip4": []},
    ])
    monkeypatch.setattr(mod_collector, "get_collector", lambda: finto)


# ── Quali indirizzi restano fuori ──────────────────────────────────

def test_gli_indirizzi_del_router_si_leggono_a_runtime(router_con_sei_indirizzi):
    """Non stanno nel codice: quello configurato piu' quelli che il router
    dichiara sulle proprie interfacce. Se la rete cambia, l'elenco lo segue."""
    cfg = settings.discovery.nmap
    assert fuori_dal_port_scan(cfg) == [ROUTER, "192.0.2.129", "198.51.100.1"]


def test_senza_snapshot_resta_il_solo_indirizzo_di_configurazione(monkeypatch):
    """Primo scan, o router muto: meglio escludere poco che tirare a indovinare."""
    from services import collector as mod_collector
    monkeypatch.setattr(mod_collector, "get_collector", lambda: FintoCollector([]))
    assert fuori_dal_port_scan(settings.discovery.nmap) == [ROUTER]


def test_lo_stesso_indirizzo_non_compare_due_volte(monkeypatch):
    from services import collector as mod_collector
    monkeypatch.setattr(mod_collector, "get_collector",
                        lambda: FintoCollector([{"ip4": [ROUTER, ROUTER]}]))
    monkeypatch.setattr(settings.discovery.nmap, "port_scan_exclude", [ROUTER])
    assert fuori_dal_port_scan(settings.discovery.nmap) == [ROUTER]


def test_si_puo_escludere_a_mano_anche_altro(monkeypatch, router_con_sei_indirizzi):
    monkeypatch.setattr(settings.discovery.nmap, "port_scan_exclude", ["192.0.2.50"])
    assert fuori_dal_port_scan(settings.discovery.nmap)[0] == "192.0.2.50"


def test_spegnendo_lo_skip_del_router_non_si_esclude_piu_niente(
        monkeypatch, router_con_sei_indirizzi):
    monkeypatch.setattr(settings.discovery.nmap, "port_scan_skip_router", False)
    assert fuori_dal_port_scan(settings.discovery.nmap) == []


# ── Come si traduce nei comandi ────────────────────────────────────

@pytest.fixture
def nmap_finto(monkeypatch, router_con_sei_indirizzi):
    """Registra i comandi lanciati e risponde con un output greppable."""
    comandi: list[list[str]] = []

    async def finto_run(args, timeout=0):
        comandi.append(list(args))
        if "-sn" in args:
            # Il ripasso sugli esclusi: il router risponde al ping.
            return f"Host: {ROUTER} ()\tStatus: Up\n"
        return "Host: 192.0.2.50 ()\tStatus: Up\nHost: 192.0.2.50 ()\tPorts: 22/open/tcp//ssh///\n"

    monkeypatch.setattr(discovery, "_run_cmd", finto_run)
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "/usr/bin/nmap")
    return comandi


async def test_il_port_scan_esclude_gli_indirizzi_del_router(nmap_finto):
    await NmapProvider().run(DeviceRegistry())
    principale = nmap_finto[0]
    assert "--exclude" in principale
    esclusi = principale[principale.index("--exclude") + 1]
    assert esclusi == f"{ROUTER},192.0.2.129,198.51.100.1"
    assert "-sT" in principale, "il port scan sul resto della rete non si tocca"


async def test_il_router_resta_fra_i_dispositivi_trovati(nmap_finto):
    """`--exclude` lo toglie da TUTTO lo scan, ping compreso: senza il secondo
    passaggio il router sparirebbe dall'elenco dei dispositivi."""
    registry = DeviceRegistry()
    await NmapProvider().run(registry)

    ripasso = nmap_finto[1]
    assert "-sn" in ripasso and ROUTER in ripasso
    assert "-sT" not in ripasso, "sul router non si aprono connessioni TCP"
    assert any(ROUTER in d.ips for d in registry.all())


async def test_col_port_scan_spento_non_serve_escludere_nessuno(monkeypatch, nmap_finto):
    """Senza port scan non c'e' connessione TCP, quindi non c'e' rumore: un
    secondo comando sarebbe solo lavoro in piu' per il router."""
    monkeypatch.setattr(settings.discovery.nmap, "port_scan", False)
    await NmapProvider().run(DeviceRegistry())
    assert len(nmap_finto) == 1
    assert "--exclude" not in nmap_finto[0]


# ── Un hostname non deve poter far saltare tutto lo scan ───────────

def test_un_nome_non_indirizzo_resta_fuori_dall_esclusione(monkeypatch, caplog):
    """Provato con nmap 7.94: un nome che non si risolve non fa saltare
    l'esclusione, fa **uscire nmap** ("Error resolving name ... QUITTING!") e con
    lui l'intera discovery, in silenzio. `router.host` puo' essere un hostname, e
    il DNS che lo risolve e' spesso proprio il router che non risponde."""
    import logging
    from services import collector as mod_collector
    monkeypatch.setattr(mod_collector, "get_collector", lambda: FintoCollector([]))
    monkeypatch.setattr(settings.router, "host", "router.example.lan")
    monkeypatch.setattr(settings.discovery.nmap, "port_scan_exclude", ["192.0.2.7"])

    with caplog.at_level(logging.WARNING, logger="discovery"):
        assert fuori_dal_port_scan(settings.discovery.nmap) == ["192.0.2.7"]
    assert "router.example.lan" in caplog.text, "va detto perche' non lo si esclude"


def test_i_cidr_sono_indirizzi_validi(monkeypatch):
    from services import collector as mod_collector
    monkeypatch.setattr(mod_collector, "get_collector", lambda: FintoCollector([]))
    monkeypatch.setattr(settings.router, "host", "")
    monkeypatch.setattr(settings.discovery.nmap, "port_scan_exclude", ["192.0.2.0/24"])
    assert fuori_dal_port_scan(settings.discovery.nmap) == ["192.0.2.0/24"]
