"""
Prova che la suite NON stia leggendo la configurazione vera del repo.

Va per primo: se questi controlli non passano, ogni altro test della suite e'
inaffidabile, perche' starebbe girando contro gli IP e le credenziali di casa
invece che contro i valori finti del conftest.
"""
from __future__ import annotations

from pathlib import Path

from config import settings

from tests.conftest import TMP

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def test_la_config_caricata_e_quella_di_test():
    assert settings.app_name == "LANMng-test"
    assert settings.router.host == "192.0.2.1"      # indirizzo documentativo, RFC 5737
    assert [s.cidr for s in settings.subnets] == ["192.0.2.0/24"]


def test_i_cataloghi_puntano_al_tmpdir_non_al_repo():
    for percorso in (settings.devices_catalog, settings.services_catalog,
                     settings.audit_log):
        assert Path(percorso).parent == TMP, f"{percorso} non e' nel tmpdir"
        assert Path(percorso).parent != REPO_CONFIG


def test_la_secret_key_e_quella_fissata_e_non_viene_generata():
    # Se fosse il default, il lifespan la genererebbe e la scriverebbe su disco.
    assert settings.secret_key == "chiave-di-test-non-usare-in-produzione"


def test_nessun_segreto_reale_e_finito_nella_config_di_test():
    testo = (TMP / "config.yaml").read_text()
    for parola in ("password", "ssh_key", "192.168."):
        assert parola not in testo, f"la config di test contiene '{parola}'"
