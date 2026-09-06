"""
services/bootstrap.py — Primo avvio guidato
============================================
Quando `config/config.yaml` non esiste, l'app non sa niente della rete su cui
gira: `subnets` e' vuota, quindi la discovery non cerca da nessuna parte e la
dashboard resta vuota senza spiegare perche'. Prima l'unica via era copiare un
template e modificarlo a mano, cioe' esattamente cio' che questo progetto vuole
evitare ("tutto configurabile dalla UI").

Questo modulo fa due cose e nient'altro:
  - dice se il primo avvio serve (`serve_setup`);
  - **propone** le subnet leggendo le interfacce dell'host, e scrive il
    `config.yaml` iniziale (`scrivi_config`).

Non duplica logica esistente: le subnet candidate escono da
`host_inspector`, la scrittura passa da `config_store` (validazione con lo
schema Pydantic, backup atomico) e la password dal `secrets_store`.

La proposta non e' una scelta: l'utente puo' toglierla o aggiungerne altre. Se
le interfacce non si leggono, si torna una lista vuota e lo si dice — mai una
subnet inventata, che manderebbe la discovery a scansionare una rete a caso.
"""
from __future__ import annotations

import ipaddress
import logging
from pathlib import Path

from config import settings
from services.config_store import get_config_store
from services.i18n import LocalizedError
from services.host_inspector import _interfaces_ip, _interfaces_nmap, _network_of

log = logging.getLogger("bootstrap")

# Interfacce da non proporre: sono reti di container o di virtualizzazione, non
# la LAN che l'utente vuole monitorare. Proporle riempirebbe la mappa di
# indirizzi che non gli appartengono.
_ESCLUSE = ("lo", "docker", "br-", "veth", "virbr", "vmnet", "tun", "tap",
            "wg", "zt", "tailscale")

# Colori della tavolozza, assegnati in ordine alle subnet proposte: la UI li usa
# per mappa e legenda e senza un valore la scheda resta grigia.
_COLORI = ["#5b9cf0", "#5bd97f", "#e6a94d", "#c77dff", "#4dd0e1", "#ff8a80"]


def serve_setup() -> bool:
    """True se il primo avvio non e' ancora stato fatto.

    Il segnale e' l'assenza del file di configurazione, non una chiave dentro
    di esso: se il file c'e', qualcuno l'ha gia' scritto (dalla UI o a mano) e
    la procedura guidata non deve piu' comparire — sarebbe un modo di
    riconfigurare il servizio senza passare dal login.
    """
    return not Path(get_config_store().path).exists()


async def subnet_candidate() -> list[dict]:
    """Subnet ricavate dalle interfacce dell'host, come proposta per il wizard.

    Ritorna `[{cidr, label, color, scan, iface}]`. Vuota se le interfacce non
    si leggono: meglio nessuna proposta che una sbagliata.
    """
    try:
        ifaces = await _interfaces_ip()
        if not ifaces:
            ifaces = await _interfaces_nmap()
    except Exception as e:                      # noqa: BLE001 - una sorgente sola
        log.warning(f"interfacce non leggibili, nessuna subnet proposta: {e}")
        return []

    viste: dict[str, str] = {}                  # cidr -> nome interfaccia
    for iface in ifaces:
        nome = iface.get("name", "")
        if not nome or nome.startswith(_ESCLUSE):
            continue
        for addr in iface.get("addresses", []):
            if addr.get("family") not in ("inet", ""):
                continue
            cidr = _network_of(addr.get("ip", ""), addr.get("prefix"))
            if not cidr:
                continue
            try:
                rete = ipaddress.ip_network(cidr)
            except ValueError:
                continue
            # Solo spazi privati: un indirizzo pubblico sull'host e' l'uplink,
            # e scansionarlo vorrebbe dire scansionare internet.
            if not rete.is_private or rete.is_loopback or rete.is_link_local:
                continue
            # Una /32 non e' una rete da scansionare: e' un singolo indirizzo
            # (tipico delle interfacce VPN punto-punto).
            if rete.prefixlen >= 31:
                continue
            viste.setdefault(cidr, nome)

    return [
        {"cidr": cidr, "label": _etichetta(i), "color": _COLORI[i % len(_COLORI)],
         "scan": True, "iface": iface}
        for i, (cidr, iface) in enumerate(sorted(viste.items()))
    ]


def _etichetta(indice: int) -> str:
    """Nome breve della subnet in UI. La prima e' "LAN": e' quella su cui
    l'host sta, cioe' quella che l'utente chiama cosi'."""
    return "LAN" if indice == 0 else f"LAN {indice + 1}"


def scrivi_config(subnets: list[dict], router: dict | None = None) -> dict:
    """Scrive il `config.yaml` iniziale e ritorna cio' che e' stato salvato.

    Solleva `ValueError` se il primo avvio e' gia' stato fatto (il file esiste)
    o se i dati non sono validi. Il router e' facoltativo: senza, restano
    attive discovery locale e monitoraggio Docker, che non lo richiedono.
    """
    if not serve_setup():
        raise LocalizedError("err.setupGiaFatto")
    if not subnets:
        raise LocalizedError("err.serveSubnet")

    pulite = []
    for sn in subnets:
        cidr = str((sn or {}).get("cidr", "")).strip()
        try:
            rete = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise LocalizedError("err.subnetNonValida", cidr=cidr or "?") from None
        if rete.prefixlen >= 31:
            raise LocalizedError("err.subnetSingoloIndirizzo", cidr=cidr)
        voce = {"cidr": str(rete), "label": str(sn.get("label") or "LAN").strip(),
                "scan": bool(sn.get("scan", True))}
        if sn.get("color"):
            voce["color"] = str(sn["color"])
        pulite.append(voce)

    dati: dict = {"subnets": pulite}
    if router and str(router.get("host", "")).strip():
        blocco = {"host": str(router["host"]).strip()}
        for campo in ("user", "ssh_key"):
            if router.get(campo):
                blocco[campo] = str(router[campo]).strip()
        if router.get("port"):
            blocco["port"] = int(router["port"])
        dati["router"] = blocco

    # Da qui in poi e' config_store a comandare: valida con lo schema Pydantic
    # e scrive in modo atomico. Se lo schema rifiuta, il file non nasce.
    get_config_store().scrivi_iniziale(dati)
    log.info(f"primo avvio: config.yaml creato con {len(pulite)} subnet"
             f"{' e il router' if 'router' in dati else ''}")
    return dati
