"""
Mascheramento e ripristino dei segreti (services/config_store.py).

Sicurezza: la pagina Impostazioni mostra la config con i segreti mascherati e
la rimanda indietro cosi' com'e'. Al salvataggio i segreti vanno ripresi
dall'originale. Se il ripristino accoppiasse gli elementi **per posizione**,
riordinare o inserire un host SSH farebbe finire la password di un host su un
altro: un travaso silenzioso di credenziali fra macchine diverse.
"""
from __future__ import annotations

import pytest

from services.config_store import MASK, _has_masked_secret, _item_identity, _merge_secrets, _redact


# ── Mascheramento ──────────────────────────────────────────────────

def test_i_segreti_vengono_mascherati_a_ogni_livello():
    dentro = {"router": {"host": "192.0.2.1", "password": "segreta"},
              "docker": {"hosts": [{"name": "nas", "password": "altra"}]}}
    fuori = _redact(dentro)
    assert fuori["router"]["password"] == MASK
    assert fuori["docker"]["hosts"][0]["password"] == MASK
    assert fuori["router"]["host"] == "192.0.2.1"        # i non-segreti restano


def test_tutte_le_chiavi_segrete_sono_coperte():
    fuori = _redact({"password": "a", "luci_password": "b", "password_hash": "c"})
    assert set(fuori.values()) == {MASK}


def test_un_segreto_vuoto_resta_vuoto():
    # Mascherare il vuoto farebbe credere che una password sia impostata.
    fuori = _redact({"password": "", "luci_password": None})
    assert fuori == {"password": "", "luci_password": None}


def test_redact_non_muta_loriginale():
    dentro = {"router": {"password": "segreta"}}
    _redact(dentro)
    assert dentro["router"]["password"] == "segreta"


# ── Ripristino: la proprieta' di sicurezza ─────────────────────────

def _originale():
    return {"discovery": {"ssh": {"hosts": [
        {"ip": "192.0.2.10", "password": "password-del-dieci"},
        {"ip": "192.0.2.20", "password": "password-del-venti"},
    ]}}}


def _mascherato():
    return {"discovery": {"ssh": {"hosts": [
        {"ip": "192.0.2.10", "password": MASK},
        {"ip": "192.0.2.20", "password": MASK},
    ]}}}


def test_riordinare_gli_host_non_travasa_le_password():
    nuovo = _mascherato()
    nuovo["discovery"]["ssh"]["hosts"].reverse()
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    per_ip = {h["ip"]: h["password"] for h in out}
    assert per_ip == {"192.0.2.10": "password-del-dieci",
                      "192.0.2.20": "password-del-venti"}


def test_inserire_un_host_in_testa_non_sposta_le_password():
    nuovo = _mascherato()
    nuovo["discovery"]["ssh"]["hosts"].insert(0, {"ip": "192.0.2.5", "password": "nuova"})
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    per_ip = {h["ip"]: h["password"] for h in out}
    assert per_ip["192.0.2.5"] == "nuova"
    assert per_ip["192.0.2.10"] == "password-del-dieci"
    assert per_ip["192.0.2.20"] == "password-del-venti"


def test_un_host_nuovo_mascherato_non_eredita_il_segreto_di_nessuno():
    nuovo = {"discovery": {"ssh": {"hosts": [{"ip": "192.0.2.99", "password": MASK}]}}}
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    assert out[0]["password"] is None


def test_un_host_rimosso_non_fa_riapparire_il_suo_segreto():
    nuovo = {"discovery": {"ssh": {"hosts": [{"ip": "192.0.2.10", "password": MASK}]}}}
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    assert len(out) == 1 and out[0]["password"] == "password-del-dieci"


def test_una_password_cambiata_a_mano_vince_sulloriginale():
    nuovo = _mascherato()
    nuovo["discovery"]["ssh"]["hosts"][0]["password"] = "password-nuova"
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    assert out[0]["password"] == "password-nuova"


def test_svuotare_una_password_la_cancella_davvero():
    nuovo = _mascherato()
    nuovo["discovery"]["ssh"]["hosts"][0]["password"] = ""
    out = _merge_secrets(nuovo, _originale())["discovery"]["ssh"]["hosts"]
    assert out[0]["password"] == ""


def test_il_ripristino_funziona_anche_fuori_dalle_liste():
    out = _merge_secrets({"router": {"host": "192.0.2.1", "password": MASK}},
                         {"router": {"host": "192.0.2.1", "password": "segreta"}})
    assert out["router"]["password"] == "segreta"


def test_originale_assente_non_solleva():
    out = _merge_secrets({"router": {"password": MASK}}, {})
    assert out["router"]["password"] is None


# ── Identita' degli elementi di lista ──────────────────────────────

def test_lidentita_usa_il_primo_campo_disponibile():
    assert _item_identity({"ip": "192.0.2.10"}) == ("ip", "192.0.2.10")
    assert _item_identity({"name": "nas", "ip": "192.0.2.10"}) == ("name", "nas")


def test_un_elemento_senza_identita_vale_none():
    assert _item_identity({"password": "x"}) is None
    assert _item_identity("non-un-dict") is None


def test_lidentita_non_puo_essere_la_maschera():
    # Altrimenti due elementi mascherati risulterebbero lo stesso elemento.
    assert _item_identity({"name": MASK, "ip": "192.0.2.10"}) == ("ip", "192.0.2.10")


def test_riconosce_un_segreto_mascherato_annidato():
    assert _has_masked_secret({"a": {"b": [{"password": MASK}]}})
    assert not _has_masked_secret({"a": {"b": [{"password": "vera"}]}})


def test_un_guasto_interno_non_diventa_colpa_di_chi_salva(monkeypatch):
    """`except Exception -> ValueError("Configurazione non valida")` trasformava
    qualunque bug interno in un 400 che accusava l'utente di aver scritto una
    configurazione sbagliata. Ora solo gli errori di schema lo fanno."""
    import services.config_store as mod

    def esplode(**_):
        raise OSError("disco pieno")

    monkeypatch.setattr(mod, "Settings", esplode)
    with pytest.raises(OSError):
        mod.ConfigStore().save_yaml("router:\n  host: 192.0.2.1\n")


# ── Sezione alerts_silenced ────────────────────────────────────────

def _store(tmp_path, monkeypatch):
    import services.config_store as mod

    store = mod.ConfigStore()
    monkeypatch.setattr(store, "path", tmp_path / "config.yaml")
    return store


def test_silenziare_un_alert_non_richiede_il_riavvio(tmp_path, monkeypatch):
    # La sezione la rilegge il backend da solo: dire "riavvia il servizio"
    # smentirebbe in UI una promessa che il codice mantiene.
    esito = _store(tmp_path, monkeypatch).save_section("alerts_silenced", [
        {"rule": "docker.restart_loop", "subject": "nas/uno",
         "reason": "lo riavvio a mano"}])
    assert esito == {"ok": True, "restart_required": False}


def test_le_altre_sezioni_continuano_a_richiedere_il_riavvio(tmp_path, monkeypatch):
    esito = _store(tmp_path, monkeypatch).save_section(
        "subnets", [{"cidr": "192.0.2.0/24", "label": "test"}])
    assert esito["restart_required"] is True


def test_una_regola_inesistente_viene_rifiutata(tmp_path, monkeypatch):
    # Lo schema la accetterebbe (e' solo una stringa) e non zittirebbe nulla,
    # senza dire niente a chi ha salvato.
    with pytest.raises(ValueError) as e:
        _store(tmp_path, monkeypatch).save_section("alerts_silenced", [
            {"rule": "docker.inventata", "reason": "boh"}])
    assert "sconosciuta" in str(e.value)


def test_un_silenziamento_senza_motivo_viene_rifiutato(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as e:
        _store(tmp_path, monkeypatch).save_section("alerts_silenced", [
            {"rule": "docker.restart_loop", "subject": "nas/uno", "reason": "  "}])
    assert "motivo" in str(e.value)


def test_il_silenziamento_salvato_si_rilegge(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.save_section("alerts_silenced", [
        {"rule": "docker.restart_loop", "subject": "nas/uno", "reason": "lo so"}])
    assert store.read_section("alerts_silenced")[0]["reason"] == "lo so"
