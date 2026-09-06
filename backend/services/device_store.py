"""
services/device_store.py — Gestione persistente del catalogo dispositivi
========================================================================
Legge/scrive config/devices.yaml. Permette dalla UI di:
  - modificare nome/tipo/note/URL di un dispositivo (catalogo)
  - nascondere un dispositivo (resta escluso anche se la discovery lo ritrova)

Struttura del file:
    devices:
      - ip: "192.0.2.2"       # identita' della entry
        mac: "AA:BB:CC:..."   # facoltativo, descrittivo
        name: "..."
        type: "..."
        ...
    hidden:                    # IP (o MAC, per le entry senza IP) da escludere
      - "192.0.2.50"
      - "AA:BB:CC:..."

**Una entry si identifica con l'IP**; il MAC identifica solo le entry che un IP
non ce l'hanno (dispositivo noto ma senza indirizzo fisso). Su una entry con IP
il MAC e' un dato descrittivo e puo' ripetersi: una VM in bridge su Wi-Fi si
presenta al router con il MAC della scheda dell'host, quindi lo stesso MAC
appartiene a macchine diverse. Vedi `_identifiers` e `services/registry.py`.

Scrittura atomica con backup .bak. Se il file reale non esiste, parte
dal template .example (se presente) cosi' le modifiche dell'utente
vengono comunque persistite sul file reale.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from config import settings

log = logging.getLogger("device-store")

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")

# Campi del catalogo modificabili dalla UI.
EDITABLE = ("name", "type", "os", "notes", "url", "services")

# Campi che identificano l'entry: si modificano anche loro dalla UI, ma passano
# da un percorso a parte (validazione + controllo di conflitto + spostamento del
# flag "nascosto"), perche' cambiarli significa spostare l'identita' del device.
IDENTITY = ("ip", "mac")


def _normalize(identifier: str) -> str:
    """MAC -> forma canonica (upper + separatore ':'); IP/altro invariato.
    Cosi' un MAC scritto con trattini nel catalogo combacia con quelli prodotti
    da ARP/DHCP (che usano ':') per dedup e per la lista dei nascosti."""
    ident = (identifier or "").strip()
    return ident.upper().replace("-", ":") if _MAC_RE.match(ident) else ident


def _is_mac(identifier: str) -> bool:
    return bool(_MAC_RE.match((identifier or "").strip()))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").strip())
        return True
    except ValueError:
        return False


class DeviceStore:

    def __init__(self):
        self.path = Path(settings.devices_catalog)
        self._data: dict = {"devices": [], "hidden": []}
        self.reload()

    # ── Caricamento ────────────────────────────────────────────────

    def reload(self):
        src = self.path
        if not src.exists():
            example = src.with_name(src.stem + ".example" + src.suffix)
            src = example if example.exists() else None
        if src and src.exists():
            with open(src) as f:
                data = yaml.safe_load(f) or {}
            self._data = {
                "devices": data.get("devices", []) or [],
                "hidden": [str(x) for x in (data.get("hidden", []) or [])],
            }
        else:
            self._data = {"devices": [], "hidden": []}

    # ── Lookup per lo scanner ──────────────────────────────────────

    def catalog_by_mac(self) -> dict[str, dict]:
        """Solo le entry identificate dal **solo** MAC, senza un IP proprio.

        Una entry che porta anche l'IP descrive il dispositivo a quell'indirizzo:
        applicarla per MAC a un altro indirizzo e' sbagliato, perche' un MAC puo'
        essere condiviso (scheda in bridge su Wi-Fi). Succedeva davvero: una VM
        nuova su 192.0.2.15 ereditava nome, OS e note di host-w, che vive
        su 192.0.2.11 con la stessa scheda fisica."""
        out = {}
        for e in self._data["devices"]:
            mac = _normalize(e.get("mac") or "")
            if mac and not (e.get("ip") or "").strip():
                out[mac] = e
        return out

    def catalog_by_ip(self) -> dict[str, dict]:
        out = {}
        for e in self._data["devices"]:
            ip = (e.get("ip") or "").strip()
            if ip:
                out[ip] = e
        return out

    def hidden_set(self) -> set[str]:
        return {_normalize(x) for x in self._data["hidden"]}

    def entries(self) -> list[dict]:
        """Tutte le entry del catalogo (per il seeding dello scanner:
        i device aggiunti a mano compaiono anche se spenti/non scoperti)."""
        return list(self._data["devices"])

    # ── Mutazioni (persistite) ─────────────────────────────────────

    def add(self, mac: str = "", ip: str = "", fields: dict | None = None) -> dict:
        """Aggiunge un dispositivo manuale identificato da MAC e/o IP."""
        mac = _normalize(mac or "")
        ip = (ip or "").strip()
        if not mac and not ip:
            raise ValueError("serve almeno un MAC o un IP")
        # "Aggiungi" crea: se l'indirizzo e' gia' in catalogo si ferma e lo dice.
        # Prima si cercava solo per MAC, quindi un IP gia' presente generava una
        # seconda entry sullo stesso indirizzo (catalogo ambiguo); aggiornare in
        # silenzio l'altro dispositivo sarebbe altrettanto sorprendente, perche'
        # ne riscriverebbe nome e note.
        # Si cerca per l'identificatore che la nuova entry avra' davvero: con un
        # IP e' l'IP, e il MAC condiviso con un'altra entry non blocca piu' la
        # creazione (vedi _identifiers).
        esistente = self._find(ip) if ip else self._find(mac)
        if esistente is not None:
            nome = esistente.get("name") or "senza nome"
            raise ValueError(f"{ip or mac} e' gia' del dispositivo '{nome}': "
                             f"modificalo invece di aggiungerlo")
        entry: dict = {}
        # Stessa strada della modifica: valida gli indirizzi e rifiuta quelli
        # gia' di un altro dispositivo.
        self._set_identity(entry, {"ip": ip, "mac": mac})
        self._data["devices"].append(entry)
        entry.update({k: v for k, v in (fields or {}).items() if k in EDITABLE})
        self._save()
        return entry

    def upsert(self, identifier: str, fields: dict) -> dict:
        """Crea/aggiorna l'entry catalogo per un MAC o IP.

        `fields` puo' contenere anche `ip`/`mac`: in quel caso l'entry viene
        spostata su quell'indirizzo invece di crearne una seconda (serve quando
        una VM cambia IP o si corregge un indirizzo sbagliato). Solleva
        ValueError su indirizzo non valido o gia' di un altro dispositivo."""
        ident = _normalize(identifier)
        key = "mac" if _is_mac(ident) else "ip"
        entry = self._find(ident)
        if entry is None:
            entry = {key: ident}
            self._data["devices"].append(entry)
        self._set_identity(entry, fields)
        clean = {k: v for k, v in fields.items() if k in EDITABLE}
        entry.update(clean)
        self._save()
        return entry

    def _set_identity(self, entry: dict, fields: dict):
        """Applica i nuovi ip/mac a una entry esistente, se sono stati mandati.

        Un campo assente (None) lascia il valore com'e'; una stringa vuota lo
        cancella, purche' resti almeno un identificatore."""
        if fields.get("ip") is None and fields.get("mac") is None:
            return

        ip = (fields["ip"] if fields.get("ip") is not None else entry.get("ip") or "").strip()
        mac = _normalize(fields["mac"] if fields.get("mac") is not None
                         else entry.get("mac") or "")
        if ip and not _is_ip(ip):
            raise ValueError(f"IP non valido: {ip}")
        if mac and not _is_mac(mac):
            raise ValueError(f"MAC non valido: {mac}")
        if not ip and not mac:
            raise ValueError("un dispositivo deve avere almeno un IP o un MAC")

        # Due entry con lo stesso identificatore renderebbero ambiguo il
        # catalogo: non si saprebbe quale nome applicare al device trovato
        # dalla discovery. Il MAC si controlla solo se la entry resta senza IP:
        # con un IP e' descrittivo e puo' ripetersi (schede in bridge).
        for other in self._data["devices"]:
            if other is entry:
                continue
            altrui = self._identifiers(other)
            nome = other.get("name") or "senza nome"
            if ip and ip in altrui:
                raise ValueError(f"{ip} e' gia' del dispositivo '{nome}'")
            if mac and not ip and mac in altrui:
                raise ValueError(f"{mac} e' gia' del dispositivo '{nome}'")

        vecchi = self._identifiers(entry)
        if ip:
            entry["ip"] = ip
        else:
            entry.pop("ip", None)
        if mac:
            entry["mac"] = mac
        else:
            entry.pop("mac", None)

        # "Nascosto" e' una preferenza sul dispositivo, non sull'indirizzo: se
        # l'entry si sposta, la lista dei nascosti la segue (altrimenti il
        # device ricomparirebbe da solo al primo scan dopo la modifica).
        nascosti = {_normalize(x) for x in self._data["hidden"]}
        if vecchi & nascosti and not (self._identifiers(entry) & nascosti):
            self._data["hidden"] = [x for x in self._data["hidden"]
                                    if _normalize(x) not in vecchi]
            self._data["hidden"].append(ip or mac)
            log.info(f"nascosto spostato da {sorted(vecchi)} a {ip or mac}")

    def hide(self, identifier: str):
        ident = _normalize(identifier)
        if ident not in self.hidden_set():
            self._data["hidden"].append(ident)
            self._save()

    def unhide(self, identifier: str):
        ident = _normalize(identifier)
        self._data["hidden"] = [x for x in self._data["hidden"] if _normalize(x) != ident]
        self._save()

    def delete(self, identifier: str):
        """Rimuove l'entry dal catalogo e nasconde il dispositivo."""
        ident = _normalize(identifier)
        self._data["devices"] = [
            e for e in self._data["devices"] if ident not in self._identifiers(e)
        ]
        self.hide(ident)   # include _save()

    # ── Interni ────────────────────────────────────────────────────

    @staticmethod
    def _identifiers(entry: dict) -> set[str]:
        """Gli indirizzi con cui la entry si identifica: l'IP se ce l'ha,
        altrimenti il MAC.

        Il MAC di una entry che ha gia' un IP e' un dato descrittivo, non una
        identita': puo' essere condiviso (una VM in bridge su Wi-Fi mostra il
        MAC della scheda dell'host). Trattarlo come identita' impediva di dare
        un nome proprio a un indirizzo nuovo — la UI ricompila il MAC osservato
        e il salvataggio moriva su "e' gia' del dispositivo X" — ed e' la stessa
        regola con cui lo scanner applica il catalogo."""
        ident = (entry.get("ip") or "").strip() or entry.get("mac") or ""
        return {_normalize(ident)} if ident else set()

    def _find(self, ident: str) -> dict | None:
        for e in self._data["devices"]:
            if ident in self._identifiers(e):
                return e
        return None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        header = ("# devices.yaml — catalogo dispositivi LANMng\n"
                  "# Gestito anche dalla UI (Dispositivi). Backup automatico in devices.yaml.bak\n\n")
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(header)
                yaml.safe_dump(self._data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, self.path)
            log.info(f"devices.yaml salvato ({len(self._data['devices'])} entry, "
                     f"{len(self._data['hidden'])} nascosti)")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


_store: DeviceStore | None = None


def get_device_store() -> DeviceStore:
    global _store
    if _store is None:
        _store = DeviceStore()
    return _store
