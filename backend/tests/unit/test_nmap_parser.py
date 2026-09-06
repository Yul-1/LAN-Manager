"""
Parser dell'output greppable di nmap (services/discovery.py).

La fixture e' una cattura reale di `nmap -T4 -oG - -n -sT -p <porte>` con gli
indirizzi riscritti sulle reti di documentazione.
"""
from __future__ import annotations

from pathlib import Path

from services.discovery import _parse_nmap_greppable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _testo():
    return (FIXTURES / "nmap_greppable.txt").read_text()


def _per_ip(voci):
    return {v["ip"]: v for v in voci}


# ── Estrazione dei campi ───────────────────────────────────────────

def test_gli_host_spenti_sono_esclusi():
    ip_trovati = {v["ip"] for v in _parse_nmap_greppable(_testo())}
    assert "192.0.2.99" not in ip_trovati        # unica riga "Status: Down"
    assert {"192.0.2.1", "192.0.2.30", "192.0.2.50"} <= ip_trovati


def test_le_porte_aperte_sono_raccolte_e_quelle_chiuse_no():
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert unite["192.0.2.1"]["ports"] == [22, 53, 80, 443]
    assert unite["192.0.2.30"]["ports"] == [22, 80, 443]
    # nella riga c'erano anche 3000/closed, 8000/closed, ...


def test_lhostname_fra_parentesi_viene_letto():
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert unite["192.0.2.50"]["hostname"] == "stampante.lan"


def test_le_parentesi_vuote_non_producono_un_hostname():
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert "hostname" not in unite["192.0.2.1"]


def test_mac_e_vendor_vengono_separati():
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert unite["192.0.2.51"]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert unite["192.0.2.51"]["vendor"] == "Esempio Networks"


def test_los_si_ferma_al_tabulatore():
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert unite["192.0.2.52"]["os"] == "Linux 5.X"      # non "Linux 5.X\tSeq Index: 260"


def test_le_righe_di_commento_sono_ignorate():
    # L'output vero comincia e finisce con righe "# Nmap ...".
    assert all(not v["ip"].startswith("#") for v in _parse_nmap_greppable(_testo()))


def test_testo_vuoto_o_spazzatura_non_solleva():
    assert _parse_nmap_greppable("") == []
    assert _parse_nmap_greppable("righe\nsenza\nsenso") == []


def test_riga_troncata_a_meta_non_solleva():
    assert _parse_nmap_greppable("Host: 192.0.2.7 (") == [{"ip": "192.0.2.7", "ports": []}]


# ── Comportamento attuale da conoscere ─────────────────────────────

def test_le_due_righe_dello_stesso_host_danno_una_voce_sola():
    """Con `port_scan` acceso nmap stampa DUE righe `Host:` per ogni indirizzo,
    una `Status:` e una `Ports:`. Vanno fuse: prima non lo erano, e il conteggio
    loggato (`nmap: N host`) risultava doppio — e' il motivo per cui il log
    riportava 12-14 host su una rete che ne aveva 7.
    """
    voci = _parse_nmap_greppable(_testo())
    ip_distinti = {v["ip"] for v in voci}
    assert len(voci) == len(ip_distinti)


def test_la_fusione_conserva_i_campi_di_entrambe_le_righe():
    # Lo Status porta hostname e MAC, il Ports porta le porte: si perdono
    # entrambi se la fusione tiene solo l'ultima riga vista.
    unite = _per_ip(_parse_nmap_greppable(_testo()))
    assert unite["192.0.2.1"]["ports"] == [22, 53, 80, 443]
    assert unite["192.0.2.50"]["hostname"] == "stampante.lan"


def test_le_porte_ripetute_non_si_accumulano():
    testo = ("Host: 192.0.2.7 ()\tPorts: 22/open/tcp//ssh///\n"
             "Host: 192.0.2.7 ()\tPorts: 22/open/tcp//ssh///, 80/open/tcp//http///\n")
    assert _parse_nmap_greppable(testo)[0]["ports"] == [22, 80]
