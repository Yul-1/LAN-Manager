"""
Identita' delle entry del catalogo dispositivi (services/device_store.py).

La regola, gemella di quella del registry: **l'IP identifica, il MAC descrive**.
Il MAC di una entry che ha gia' un IP puo' ripetersi (una VM in bridge su Wi-Fi
mostra il MAC della scheda dell'host), quindi trattarlo come identita' impediva
di dare un nome a un indirizzo nuovo: il salvataggio moriva su "e' gia' del
dispositivo X".

`DeviceStore.__init__` legge il devices.yaml vero, quindi qui si costruisce con
`__new__` e si popola `_data` a mano: nessun accesso al filesystem.
"""
from __future__ import annotations

import pytest

from services.device_store import DeviceStore, _is_ip, _is_mac, _normalize

MAC = "AA:BB:CC:DD:EE:FF"


def _store(devices=None, hidden=None):
    s = DeviceStore.__new__(DeviceStore)
    s._data = {"devices": list(devices or []), "hidden": list(hidden or [])}
    return s


# ── _identifiers: chiamabile senza costruire lo store ──────────────

def test_una_entry_con_ip_si_identifica_con_lip():
    assert DeviceStore._identifiers({"ip": "192.0.2.10", "mac": MAC}) == {"192.0.2.10"}


def test_una_entry_senza_ip_si_identifica_col_mac():
    assert DeviceStore._identifiers({"mac": "aa-bb-cc-dd-ee-ff"}) == {MAC}


def test_una_entry_senza_niente_non_ha_identita():
    assert DeviceStore._identifiers({}) == set()
    assert DeviceStore._identifiers({"ip": "   ", "name": "x"}) == set()


def test_normalizzazione():
    assert _normalize("aa-bb-cc-dd-ee-ff") == MAC
    assert _normalize("192.0.2.10") == "192.0.2.10"      # gli IP non si toccano
    assert _is_mac(MAC) and not _is_mac("192.0.2.10")
    assert _is_ip("192.0.2.10") and not _is_ip(MAC)


# ── _set_identity ──────────────────────────────────────────────────

def test_un_campo_assente_lascia_il_valore_com_e():
    entry = {"ip": "192.0.2.10", "mac": MAC, "name": "NAS"}
    _store([entry])._set_identity(entry, {"name": "Altro"})
    assert entry["ip"] == "192.0.2.10" and entry["mac"] == MAC


def test_una_stringa_vuota_cancella_lidentificatore():
    entry = {"ip": "192.0.2.10", "mac": MAC}
    _store([entry])._set_identity(entry, {"ip": ""})
    assert "ip" not in entry and entry["mac"] == MAC


def test_non_si_puo_restare_senza_alcun_identificatore():
    entry = {"ip": "192.0.2.10", "mac": MAC}
    with pytest.raises(ValueError, match="almeno un IP o un MAC"):
        _store([entry])._set_identity(entry, {"ip": "", "mac": ""})


def test_ip_e_mac_invalidi_vengono_respinti():
    entry = {"ip": "192.0.2.10"}
    s = _store([entry])
    with pytest.raises(ValueError, match="IP non valido"):
        s._set_identity(entry, {"ip": "non-un-ip"})
    with pytest.raises(ValueError, match="MAC non valido"):
        s._set_identity(entry, {"mac": "non-un-mac"})


def test_due_entry_non_possono_avere_lo_stesso_ip():
    altra = {"ip": "192.0.2.20", "name": "Altro"}
    entry = {"ip": "192.0.2.10"}
    with pytest.raises(ValueError, match="gia' del dispositivo 'Altro'"):
        _store([altra, entry])._set_identity(entry, {"ip": "192.0.2.20"})


def test_lo_stesso_mac_e_ammesso_se_le_entry_hanno_un_ip():
    # La regressione: due indirizzi dietro la stessa scheda in bridge.
    altra = {"ip": "192.0.2.20", "mac": MAC, "name": "Host"}
    entry = {"ip": "192.0.2.10"}
    _store([altra, entry])._set_identity(entry, {"mac": MAC})
    assert entry["mac"] == MAC and entry["ip"] == "192.0.2.10"


def test_il_mac_duplicato_e_respinto_solo_fra_entry_senza_ip():
    altra = {"mac": MAC, "name": "Solo MAC"}
    entry = {"mac": "11:22:33:44:55:66"}
    with pytest.raises(ValueError, match="gia' del dispositivo 'Solo MAC'"):
        _store([altra, entry])._set_identity(entry, {"mac": MAC})


def test_una_entry_non_confligge_con_se_stessa():
    entry = {"ip": "192.0.2.10", "mac": MAC}
    _store([entry])._set_identity(entry, {"ip": "192.0.2.10"})
    assert entry["ip"] == "192.0.2.10"


# ── "Nascosto" segue il dispositivo ────────────────────────────────

def test_il_nascosto_segue_lentry_che_cambia_indirizzo():
    # Altrimenti il device ricomparirebbe da solo al primo scan dopo la modifica.
    entry = {"ip": "192.0.2.10"}
    s = _store([entry], hidden=["192.0.2.10"])
    s._set_identity(entry, {"ip": "192.0.2.11"})
    assert s._data["hidden"] == ["192.0.2.11"]


def test_una_entry_non_nascosta_non_finisce_fra_i_nascosti():
    entry = {"ip": "192.0.2.10"}
    s = _store([entry], hidden=["192.0.2.99"])
    s._set_identity(entry, {"ip": "192.0.2.11"})
    assert s._data["hidden"] == ["192.0.2.99"]


def test_passare_da_solo_mac_a_ip_porta_dietro_il_nascosto():
    entry = {"mac": MAC}
    s = _store([entry], hidden=[MAC])
    s._set_identity(entry, {"ip": "192.0.2.10"})
    assert s._data["hidden"] == ["192.0.2.10"]
