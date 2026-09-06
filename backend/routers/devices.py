"""routers/devices.py — Discovery, lista e gestione dispositivi."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from services.device_store import IDENTITY, get_device_store
from services.openwrt import get_luci
from services.ratelimit import RateLimiter
from services.scanner import get_scanner

router = APIRouter()

# Cooldown globale sullo scan nmap (evita flood di scansioni sulla rete).
_scan_rl = RateLimiter(max_hits=1, window_seconds=15)


async def _persist(mutate):
    """Esegue una mutazione del catalogo (I/O file su devices.yaml) e il refresh
    fuori dall'event loop: non blocca collector e WebSocket durante la scrittura.
    Al termine notifica i client, cosi' la modifica si vede subito in UI."""
    def work():
        result = mutate()
        get_scanner().refresh_catalog()
        return result
    result = await asyncio.to_thread(work)
    await get_scanner().publish_cached()
    return result


class DeviceEdit(BaseModel):
    # ip/mac sono modificabili: una VM che cambia indirizzo va spostata, non
    # duplicata. Assenti = invariati; stringa vuota = cancellati (ne deve
    # comunque restare almeno uno).
    ip: str | None = None
    mac: str | None = None
    name: str | None = None
    type: str | None = None
    os: str | None = None
    notes: str | None = None
    url: str | None = None
    services: list[str] | None = None


class DeviceAdd(BaseModel):
    mac: str = ""
    ip: str = ""
    name: str = ""
    type: str = "unknown"
    os: str = ""
    notes: str = ""
    url: str = ""


@router.get("/")
async def list_devices(subnet: str = "", status: str = "", include_hidden: bool = False):
    """
    Lista dispositivi dall'ultimo scan.
      subnet: lan0 | lan3 | vm2 | wg | docker
      status: online | offline
      include_hidden: includi anche i dispositivi nascosti
    """
    devices = get_scanner().get_cached()
    hidden_count = sum(1 for d in devices if d.hidden)
    if not include_hidden:
        devices = [d for d in devices if not d.hidden]
    if subnet:
        devices = [d for d in devices if d.subnet == subnet]
    if status == "online":
        devices = [d for d in devices if d.online]
    elif status == "offline":
        devices = [d for d in devices if not d.online]
    return {
        "devices": [d.to_dict() for d in devices],
        "total": len(devices),
        "online": sum(1 for d in devices if d.online),
        "hidden": hidden_count,
    }


@router.post("/")
async def add_device(body: DeviceAdd):
    """Aggiunge un dispositivo manuale (MAC e/o IP). Compare anche se spento."""
    fields = {k: v for k, v in body.model_dump().items()
              if k not in ("mac", "ip") and v not in ("", None)}
    try:
        entry = await _persist(lambda: get_device_store().add(
            mac=body.mac, ip=body.ip, fields=fields))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "added", "entry": entry}


@router.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Avvia un nuovo scan in background (con cooldown anti-abuso).

    Il risultato non va atteso qui: lo scanner pubblica sul WebSocket prima il
    risultato rapido (pochi secondi) e poi quello arricchito."""
    if not _scan_rl.allowed("scan"):
        raise HTTPException(status_code=429, detail="scan gia' avviato di recente, attendi",
                            headers={"Retry-After": str(_scan_rl.retry_after("scan"))})
    _scan_rl.hit("scan")
    background_tasks.add_task(get_scanner().scan)
    return {"status": "scan started"}


@router.get("/dhcp/leases")
async def dhcp_leases():
    """DHCP leases attivi dal router."""
    leases = await get_luci().get_dhcp_leases()
    return [
        {"mac": l.mac, "ip": l.ip, "hostname": l.hostname, "expires": l.expires}
        for l in leases
    ]


@router.get("/{key}")
async def get_device(key: str):
    """Dettagli di un device per IP o, per chi non ha indirizzo, per MAC."""
    k = key.upper().replace("-", ":")
    for d in get_scanner().get_cached():
        # Il MAC vale solo per i device senza IP: e' condivisibile (bridge),
        # cercarlo sempre poteva restituire la macchina sbagliata.
        if d.key == key or key in d.ips or (not d.ips and d.mac == k):
            return d.to_dict()
    raise HTTPException(status_code=404, detail="dispositivo non trovato")


@router.put("/{key}")
async def edit_device(key: str, body: DeviceEdit, background_tasks: BackgroundTasks):
    """Modifica i campi catalogo (indirizzi, nome/tipo/note/URL) di un dispositivo."""
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        entry = await _persist(lambda: get_device_store().upsert(key, fields))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Cambiare indirizzo sposta l'identita' del device: nella cache resterebbe
    # la riga al vecchio indirizzo, con il vecchio nome, fino al ciclo
    # successivo. Un rescan la ricostruisce da zero e la fa sparire subito.
    if any(f in fields for f in IDENTITY):
        background_tasks.add_task(get_scanner().scan)
    # La chiave puo' essere cambiata (IP corretto dalla UI): la UI la usa per le
    # chiamate successive, quindi va restituita quella nuova.
    return {"status": "ok", "key": entry.get("ip") or entry.get("mac") or key,
            "updated": list(fields.keys())}


@router.post("/{key}/hide")
async def hide_device(key: str):
    """Nasconde un dispositivo (resta escluso anche se ri-scoperto)."""
    await _persist(lambda: get_device_store().hide(key))
    return {"status": "hidden", "key": key}


@router.post("/{key}/unhide")
async def unhide_device(key: str):
    """Ripristina un dispositivo nascosto."""
    await _persist(lambda: get_device_store().unhide(key))
    return {"status": "visible", "key": key}


@router.delete("/{key}")
async def delete_device(key: str):
    """Rimuove l'entry dal catalogo e nasconde il dispositivo."""
    await _persist(lambda: get_device_store().delete(key))
    return {"status": "deleted", "key": key}
