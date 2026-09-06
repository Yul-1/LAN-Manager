"""
services/history_store.py — Storico persistente delle serie temporali
=====================================================================
In origine ogni serie viveva solo in memoria: 120 punti di traffico nel
collector e ~1h di risorse per host. Un riavvio del container — cioe' ogni
aggiornamento dell'immagine — azzerava i grafici.

Qui la storia finisce su **SQLite** (stdlib, nessun servizio in piu' da
mantenere) in un file dentro la cartella config, che nel container e' l'unica
scrivibile (rootfs in sola lettura) ed e' anche l'unica che la procedura di
aggiornamento non tocca mai.

Due forme di dato, perche' sono di natura diversa:
  - **campionamenti** regolari (traffico, risorse host) -> tabelle con `span`,
    i secondi coperti dalla riga: 0 = punto grezzo, >0 = media prodotta dalla
    compattazione. Chi legge non deve distinguere;
  - **transizioni** di stato (device su/giu', servizi su/giu') -> tabelle di
    eventi, una riga solo quando lo stato cambia. Campionarle sarebbe uno
    spreco e non risponderebbe comunque alla domanda "da quando".

Regola non negoziabile: lo storico non puo' far cadere il monitoraggio. Se il
file e' corrotto, il disco e' pieno o la config lo spegne, ogni metodo diventa
un no-op e il backend si comporta esattamente come prima.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from config import settings

log = logging.getLogger("history")

# Colonne dei campionamenti, in un posto solo: le usano schema, insert,
# select e compattazione. Tenerle allineate a mano e' come si introducono
# le medie sulla colonna sbagliata.
_TRAFFIC_COLS = ("rx_bps", "tx_bps", "latency")
_HOST_COLS = ("cpu", "mem", "swap", "load1", "temp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic (
    t        INTEGER PRIMARY KEY,
    rx_bps   REAL,
    tx_bps   REAL,
    latency  REAL,
    span     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS host_metrics (
    host  TEXT    NOT NULL,
    t     INTEGER NOT NULL,
    cpu   REAL,
    mem   REAL,
    swap  REAL,
    load1 REAL,
    temp  REAL,
    span  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, t)
);

CREATE TABLE IF NOT EXISTS device_events (
    id      INTEGER PRIMARY KEY,
    t       INTEGER NOT NULL,
    dev_key TEXT    NOT NULL,
    name    TEXT,
    online  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_device_events ON device_events(dev_key, t);

CREATE TABLE IF NOT EXISTS service_events (
    id     INTEGER PRIMARY KEY,
    t      INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    host   TEXT,
    name   TEXT    NOT NULL,
    ok     INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_service_events ON service_events(kind, host, name, t);

CREATE TABLE IF NOT EXISTS log_lines (
    id    INTEGER PRIMARY KEY,
    t     INTEGER NOT NULL,
    kind  TEXT    NOT NULL,
    level TEXT    NOT NULL,
    src   TEXT,
    msg   TEXT    NOT NULL,
    host  TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_lines ON log_lines(kind, t);
"""


def _ms() -> int:
    return int(time.time() * 1000)


class HistoryStore:
    """Storico su SQLite. Sincrono di proposito: i chiamanti async lo invocano
    con `asyncio.to_thread`, cosi' il modulo resta banale da leggere e da
    testare, e l'event loop non vede mai una fsync."""

    def __init__(self, path: Optional[str] = None, enabled: Optional[bool] = None):
        self.path = Path(path or settings.history_db)
        cfg = settings.history
        self.enabled = cfg.enabled if enabled is None else enabled
        self._db: Optional[sqlite3.Connection] = None
        # Una sola connessione condivisa fra il collector (che scrive da un
        # thread del pool di asyncio.to_thread) e le API (che leggono da un
        # altro). Il lock costa nulla a questo volume — una scrittura ogni
        # 10 secondi — e toglie di mezzo ogni dubbio sulla thread-safety.
        self._lock = threading.Lock()
        # Ultimo stato noto di device e servizi: serve a scrivere SOLO le
        # transizioni. Si ripopola dal database all'apertura, altrimenti ogni
        # riavvio del backend inventerebbe un cambio di stato per tutto.
        self._last_device: dict[str, bool] = {}
        self._last_service: dict[tuple, bool] = {}
        self._last_compact = 0.0
        if self.enabled:
            self._open()

    # ── Apertura / chiusura ──────────────────────────────────────────

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = self._connect()
        except (OSError, sqlite3.DatabaseError) as e:
            # Primo tentativo fallito: se il file c'e' ma non si apre e'
            # verosimilmente corrotto. Si mette da parte e si riparte da zero,
            # perche' perdere lo storico e' meno grave che perdere il servizio.
            log.error(f"storico non apribile ({self.path}): {e}")
            if self.path.exists() and self._quarantena():
                try:
                    self._db = self._connect()
                except (OSError, sqlite3.DatabaseError) as e2:
                    log.error(f"storico non ricreabile, disattivato: {e2}")
        if self._db is None:
            self.enabled = False
            return
        self._prime_stato()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        db.row_factory = sqlite3.Row
        # quick_check apre davvero le pagine: un file troncato o non-SQLite
        # solleva qui invece che a meta' di una scrittura.
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            db.close()
            raise sqlite3.DatabaseError("quick_check fallito")
        # WAL: le letture delle API non aspettano la scrittura del collector.
        # Sotto /mnt/c (sviluppo su WSL) il locking di DrvFs non lo regge: si
        # logga e si prosegue col journal predefinito invece di piantarsi.
        modo = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(modo).lower() != "wal":
            log.warning(f"WAL non disponibile su {self.path} (modo: {modo}); "
                        "si prosegue col journal predefinito")
        # NORMAL invece di FULL: in caso di spegnimento brutale si perde al
        # massimo l'ultima transazione di metriche. Un fsync ogni 10 secondi
        # sul disco del server non vale il dato che protegge.
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.commit()
        self._chmod()
        return db

    def _quarantena(self) -> bool:
        """Sposta un file illeggibile invece di cancellarlo: se serviva
        davvero, e' ancora li' da guardare."""
        rotto = self.path.with_name(f"{self.path.name}.corrotto-{int(time.time())}")
        try:
            self.path.rename(rotto)
            log.error(f"storico corrotto messo da parte come {rotto.name}, si riparte da zero")
            return True
        except OSError as e:
            log.error(f"storico corrotto non spostabile: {e}")
            return False

    def _chmod(self) -> None:
        """Il file e' la mappa temporale della rete di casa: leggibile solo dal
        proprietario. Riapplicato come in services/audit.py, cosi' un file
        ripristinato da backup con permessi larghi viene comunque richiuso."""
        for p in (self.path, self.path.with_name(self.path.name + "-wal"),
                  self.path.with_name(self.path.name + "-shm")):
            try:
                if p.exists() and (p.stat().st_mode & 0o777) != 0o600:
                    os.chmod(p, 0o600)
            except OSError as e:
                log.debug(f"chmod {p.name}: {e}")

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error as e:
                log.debug(f"chiusura storico: {e}")
            self._db = None

    # ── Scrittura ────────────────────────────────────────────────────

    def _write(self, sql: str, params) -> None:
        """Esegue una scrittura. Non solleva mai verso il collector: un disco
        pieno deve degradare lo storico, non fermare il monitoraggio."""
        if self._db is None:
            return
        try:
            with self._lock, self._db:
                if isinstance(params, list):
                    self._db.executemany(sql, params)
                else:
                    self._db.execute(sql, params)
        except sqlite3.Error as e:
            log.error(f"scrittura storico fallita: {e}")

    def add_traffic(self, point: dict) -> None:
        """Un punto di traffico. I None restano None: il buco onesto della
        serie in memoria (prima lettura, reboot del router, cambio interfaccia)
        deve restare un buco anche a grafico ricaricato, mai uno zero."""
        if self._db is None or not point:
            return
        self._write(
            "INSERT OR REPLACE INTO traffic (t, rx_bps, tx_bps, latency, span) "
            "VALUES (?, ?, ?, ?, 0)",
            (int(point.get("t") or _ms()), *(point.get(c) for c in _TRAFFIC_COLS)),
        )

    def add_host_points(self, points: dict[str, dict]) -> None:
        """Ultimo punto di ciascun host. `INSERT OR IGNORE` sulla chiave
        (host, t): se il collector ripassa sullo stesso campione — la raccolta
        ha un intervallo suo, indipendente dal ciclo lento — non si duplica."""
        if self._db is None or not points:
            return
        righe = [(ip, int(p.get("t") or _ms()), *(p.get(c) for c in _HOST_COLS))
                 for ip, p in points.items() if p]
        if righe:
            self._write(
                "INSERT OR IGNORE INTO host_metrics (host, t, cpu, mem, swap, load1, temp, span) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)", righe)

    def mark_devices(self, devices: list[dict]) -> None:
        """Registra i soli cambi di stato online/offline."""
        if self._db is None or not devices:
            return
        ora, righe = _ms(), []
        for d in devices:
            chiave = d.get("key")
            if not chiave:
                continue
            online = bool(d.get("online"))
            if self._last_device.get(chiave) == online:
                continue
            self._last_device[chiave] = online
            righe.append((ora, chiave, d.get("name") or "", int(online)))
        if righe:
            self._write("INSERT INTO device_events (t, dev_key, name, online) "
                        "VALUES (?, ?, ?, ?)", righe)

    def mark_services(self, overview: dict) -> None:
        """Idem per i servizi. L'identita' e' (kind, host, name): lo stesso
        nome puo' esistere su host diversi ed e' un servizio diverso."""
        if self._db is None or not overview:
            return
        ora, righe = _ms(), []
        for voce in _voci_servizi(overview):
            chiave = (voce["kind"], voce["host"], voce["name"])
            if self._last_service.get(chiave) == voce["ok"]:
                continue
            self._last_service[chiave] = voce["ok"]
            righe.append((ora, *chiave, int(voce["ok"]), voce["detail"]))
        if righe:
            self._write("INSERT INTO service_events (t, kind, host, name, ok, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?)", righe)

    def _prime_stato(self) -> None:
        """Ricarica l'ultimo stato noto di device e servizi dal database.

        Senza questo, ogni riavvio del backend scriverebbe una transizione per
        ogni entita' — e lo storico direbbe che tutta la rete si e' riaccesa
        insieme ad ogni aggiornamento dell'immagine."""
        if self._db is None:
            return
        try:
            with self._lock:
                for r in self._db.execute(
                        "SELECT dev_key, online FROM device_events "
                        "WHERE id IN (SELECT MAX(id) FROM device_events GROUP BY dev_key)"):
                    self._last_device[r["dev_key"]] = bool(r["online"])
                for r in self._db.execute(
                        "SELECT kind, host, name, ok FROM service_events "
                        "WHERE id IN (SELECT MAX(id) FROM service_events "
                        "             GROUP BY kind, host, name)"):
                    self._last_service[(r["kind"], r["host"], r["name"])] = bool(r["ok"])
        except sqlite3.Error as e:
            log.error(f"stato iniziale storico non ricaricato: {e}")

    # ── Lettura ──────────────────────────────────────────────────────

    def traffic(self, since_ms: int, max_points: int = 0) -> list[dict]:
        # Passo naturale della serie: il ciclo veloce del collector.
        return self._campioni("traffic", _TRAFFIC_COLS, since_ms, max_points,
                              passo_ms=settings.collect_interval_fast * 1000)

    def host_series(self, host: str, since_ms: int, max_points: int = 0) -> list[dict]:
        return self._campioni("host_metrics", _HOST_COLS, since_ms, max_points,
                              passo_ms=settings.host_metrics.interval * 1000,
                              dove="host = ?", args=(host,))

    def _campioni(self, tabella: str, colonne: tuple, since_ms: int,
                  max_points: int, passo_ms: int,
                  dove: str = "", args: tuple = ()) -> list[dict]:
        """Punti di una serie, a **passo fisso** su tutta la finestra chiesta.

        Il grafico del frontend dispone i punti per indice, non per timestamp
        (`drawLineChart`, xAt = i / (n-1)): restituire solo i punti esistenti
        farebbe collassare un'ora di backend spento nella larghezza di un
        pixel, cioe' un grafico che mente. Emettendo un bucket per ogni
        intervallo — con i campi a `None` dove non c'e' dato — indice e tempo
        tornano a coincidere e il buco resta visibile come tale.

        L'aggregazione e' una media per bucket, non un campionamento "uno ogni
        N": tenere una riga su venti farebbe sparire proprio i picchi per cui
        si guarda il grafico.
        """
        if self._db is None:
            return []
        fino = _ms()
        if since_ms >= fino:
            return []
        span = self._span(fino - since_ms, max_points, passo_ms)
        medie = ", ".join(f"AVG({c}) AS {c}" for c in colonne)
        filtro = "t >= ? AND t <= ?" + (f" AND {dove}" if dove else "")
        try:
            with self._lock:
                righe = self._db.execute(
                    f"SELECT (t / {span}) * {span} AS t, {medie} FROM {tabella} "
                    f"WHERE {filtro} GROUP BY t / {span} ORDER BY t",
                    (since_ms, fino, *args)).fetchall()
        except sqlite3.Error as e:
            log.error(f"lettura storico {tabella}: {e}")
            return []
        trovati = {int(r["t"]): dict(r) for r in righe}
        vuoto = {c: None for c in colonne}
        primo = (since_ms // span) * span
        return [trovati.get(t) or {"t": t, **vuoto}
                for t in range(primo, fino + 1, span)]

    @staticmethod
    def _span(finestra_ms: int, max_points: int, passo_ms: int) -> int:
        """Ampiezza del bucket: abbastanza larga da stare sotto `max_points`,
        mai piu' stretta del passo con cui la serie viene campionata.

        Il pavimento e' il **doppio** del passo naturale: con bucket larghi
        quanto l'intervallo di raccolta, il normale sfasamento fra un giro e
        l'altro lascerebbe ogni tanto un bucket vuoto, disegnando buchi che non
        esistono.

        Quel pavimento ha pero' a sua volta un tetto a un trentesimo della
        finestra, altrimenti un intervallo di raccolta molto lungo (o la config
        della suite di test, che lo mette a un giorno per non far mai partire
        una raccolta) ridurrebbe un grafico da un'ora a un punto solo. Coprire
        la finestra con bucket vuoti non nasconde nulla: dice la verita', cioe'
        che a quella risoluzione il dato non c'e'.
        """
        max_points = max_points or settings.history.max_points
        pavimento = min(max(passo_ms, 1000) * 2, max(finestra_ms // 30, 1000))
        return max(-(-finestra_ms // max(max_points, 1)), pavimento)

    def device_events(self, dev_key: str, since_ms: int) -> list[dict]:
        return self._eventi("SELECT t, online FROM device_events WHERE dev_key = ? AND t >= ? "
                            "ORDER BY t", (dev_key, since_ms))

    def service_events(self, kind: str, host: str, name: str, since_ms: int) -> list[dict]:
        return self._eventi(
            "SELECT t, ok, detail FROM service_events "
            "WHERE kind = ? AND host IS ? AND name = ? AND t >= ? ORDER BY t",
            (kind, host or "", name, since_ms))

    def device_states(self) -> list[dict]:
        """Ultima transizione nota di ogni dispositivo, anche fuori finestra.

        Serve alla UI per dire "offline da tre giorni" senza scaricare la
        cronologia completa e senza una richiesta per riga.
        """
        return self._eventi(
            "SELECT dev_key, name, online, t FROM device_events "
            "WHERE id IN (SELECT MAX(id) FROM device_events GROUP BY dev_key)", ())

    def service_states(self) -> list[dict]:
        return self._eventi(
            "SELECT kind, host, name, ok, t FROM service_events "
            "WHERE id IN (SELECT MAX(id) FROM service_events "
            "             GROUP BY kind, host, name)", ())

    def _eventi(self, sql: str, args: tuple) -> list[dict]:
        if self._db is None:
            return []
        try:
            with self._lock:
                return [dict(r) for r in self._db.execute(sql, args).fetchall()]
        except sqlite3.Error as e:
            log.error(f"lettura eventi storico: {e}")
            return []

    # ── Log ──────────────────────────────────────────────────────────
    # Forma da tabella di eventi come device_events/service_events: le righe di
    # log non si aggregano (la media di due messaggi non significa niente), si
    # scadono e basta. Si archivia solo cio' che altrimenti si perderebbe:
    # il log del backend e il registro di audit. Il syslog del ROUTER non entra
    # mai qui — quella macchina ha poche risorse ed e' gia' lei a tenerlo.

    def add_log_lines(self, kind: str, rows: list[dict]) -> None:
        """Righe di log in blocco. Chiamata dal collector, non dall'handler di
        logging: se `log.info()` toccasse il disco, un disco lento diventerebbe
        un freno su ogni riga di log del processo."""
        if self._db is None or not rows or not settings.logs.persist:
            return
        righe = [(int(r.get("ts_ms") or _ms()), kind, r.get("level") or "info",
                  r.get("src") or "", r.get("msg") or "", r.get("host") or "")
                 for r in rows]
        self._write(
            "INSERT INTO log_lines (t, kind, level, src, msg, host) "
            "VALUES (?, ?, ?, ?, ?, ?)", righe)

    def log_lines(self, kind: str, since_ms: int = 0, until_ms: Optional[int] = None,
                  limit: int = 0) -> list[dict]:
        """Righe archiviate, dalla piu' vecchia alla piu' recente.

        Il `limit` si applica prendendo le **ultime** righe della finestra e poi
        rimettendole in ordine: tagliare dall'inizio darebbe le piu' vecchie,
        cioe' l'esatto contrario di quello che serve guardando un log.
        """
        if self._db is None:
            return []
        sql = "SELECT t, level, src, msg, host FROM log_lines WHERE kind = ?"
        args: list = [kind]
        if since_ms:
            sql += " AND t >= ?"
            args.append(int(since_ms))
        if until_ms:
            sql += " AND t <= ?"
            args.append(int(until_ms))
        sql += " ORDER BY t DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        try:
            with self._lock:
                cur = self._db.execute(sql, tuple(args))
                righe = [{"t": r[0], "level": r[1], "src": r[2], "msg": r[3], "host": r[4]}
                         for r in cur.fetchall()]
        except sqlite3.Error as e:
            log.error(f"lettura log archiviati: {e}")
            return []
        righe.reverse()
        return righe

    # ── Manutenzione ─────────────────────────────────────────────────

    def due(self) -> bool:
        """Vero quando la compattazione e' dovuta. Il collector la chiama ad
        ogni ciclo lento, ma il lavoro vero gira al massimo una volta l'ora."""
        if self._db is None:
            return False
        atteso = max(settings.history.compact_every_minutes, 1) * 60
        return (time.monotonic() - self._last_compact) >= atteso

    def compact(self) -> None:
        """Aggrega i punti vecchi e cancella quelli oltre la finestra."""
        if self._db is None:
            return
        cfg = settings.history
        self._last_compact = time.monotonic()
        ora = _ms()
        soglia = ora - max(cfg.full_hours, 1) * 3600 * 1000
        scadenza = ora - max(cfg.retain_days, 1) * 86400 * 1000
        span = max(cfg.bucket_seconds, 1) * 1000
        try:
            with self._lock, self._db:
                self._aggrega("traffic", _TRAFFIC_COLS, ("t",), soglia, span)
                self._aggrega("host_metrics", _HOST_COLS, ("host", "t"), soglia, span)
                self._db.execute("DELETE FROM traffic WHERE t < ?", (scadenza,))
                self._db.execute("DELETE FROM host_metrics WHERE t < ?", (scadenza,))
                self._potatura_eventi(scadenza)
                self._potatura_log(ora)
        except sqlite3.Error as e:
            log.error(f"compattazione storico fallita: {e}")
            return
        # Niente VACUUM: riscrive l'intero file e richiede spazio libero pari
        # alla sua dimensione. Con un file da pochi MB le pagine liberate
        # vengono riusate e la crescita si ferma da sola.
        self._chmod()

    def _aggrega(self, tabella: str, colonne: tuple, chiave: tuple,
                 soglia: int, span: int) -> None:
        """Sostituisce i punti grezzi piu' vecchi di `soglia` con la loro media
        per bucket. `span` sulla riga dice quanti secondi copre, cosi' chi legge
        sa che e' una media e il volume si puo' ancora ricavare (bit/s * span)."""
        assert self._db is not None
        gruppo = ", ".join(("host",) if "host" in chiave else ()) or None
        campi = ("host, " if gruppo else "") + "t, " + ", ".join(colonne) + ", span"
        medie = ", ".join(f"AVG({c})" for c in colonne)
        self._db.execute(
            f"INSERT OR REPLACE INTO {tabella} ({campi}) "
            f"SELECT {'host, ' if gruppo else ''}(t / ?) * ?, {medie}, ? "
            f"FROM {tabella} WHERE span = 0 AND t < ? "
            f"GROUP BY {'host, ' if gruppo else ''}t / ?",
            (span, span, span // 1000, soglia, span))
        self._db.execute(f"DELETE FROM {tabella} WHERE span = 0 AND t < ?", (soglia,))

    def _potatura_log(self, ora: int) -> None:
        """Due finestre diverse, non una sola.

        Il log del backend serve a capire cosa e' successo di recente e scade in
        fretta. L'audit no: il sospetto arriva mesi dopo, quindi si tiene molto
        piu' a lungo. In ogni caso il file `audit.log` su disco resta completo —
        qui si potano solo le copie che rendono il registro cercabile in pagina.
        """
        assert self._db is not None
        cfg = settings.logs
        self._db.execute(
            "DELETE FROM log_lines WHERE kind = 'backend' AND t < ?",
            (ora - max(cfg.retain_days, 1) * 86400 * 1000,))
        self._db.execute(
            "DELETE FROM log_lines WHERE kind = 'audit' AND t < ?",
            (ora - max(cfg.audit_retain_days, 1) * 86400 * 1000,))
        # Tetto sulle righe del backend: la retention a giorni non limita un log
        # di cui non si controlla il ritmo (una libreria loquace, un host giu'
        # che fa ripetere lo stesso warning ad ogni ciclo). Si tengono le piu'
        # recenti, che sono quelle che si vanno a guardare.
        if cfg.max_rows > 0:
            self._db.execute(
                "DELETE FROM log_lines WHERE kind = 'backend' AND id NOT IN "
                "(SELECT id FROM log_lines WHERE kind = 'backend' "
                " ORDER BY id DESC LIMIT ?)", (int(cfg.max_rows),))

    def _potatura_eventi(self, scadenza: int) -> None:
        """Cancella gli eventi scaduti **tenendo sempre l'ultimo di ogni
        entita'**: senza quello si perderebbe la risposta a "da quando e' giu'"
        per un servizio caduto piu' di `retain_days` fa, che e' proprio il caso
        in cui la domanda si pone. Da non "semplificare" in una DELETE secca.
        """
        assert self._db is not None
        self._db.execute(
            "DELETE FROM device_events WHERE t < ? AND id NOT IN "
            "(SELECT MAX(id) FROM device_events GROUP BY dev_key)", (scadenza,))
        self._db.execute(
            "DELETE FROM service_events WHERE t < ? AND id NOT IN "
            "(SELECT MAX(id) FROM service_events GROUP BY kind, host, name)", (scadenza,))


def _voci_servizi(overview: dict) -> list[dict]:
    """Appiattisce la vista servizi nelle sue quattro famiglie.

    Docker, systemd e i servizi Windows portano anche l'host: lo stesso nome su
    macchine diverse e' un servizio diverso e va storicizzato separatamente.
    """
    voci = []
    for c in (overview.get("docker") or {}).get("containers") or []:
        # Attenzione ai nomi, che in ContainerInfo.to_dict() sono controintuitivi:
        # `running` e' il booleano, `status` lo stato macchina ("running",
        # "exited") e `state` la stringa leggibile ("Up 2 hours").
        voci.append({"kind": "docker", "host": c.get("host") or "", "name": c.get("name") or "",
                     "ok": bool(c.get("running")),
                     "detail": c.get("state") or c.get("status") or ""})
    for u in overview.get("systemd") or []:
        voci.append({"kind": "systemd", "host": u.get("host") or "", "name": u.get("name") or "",
                     "ok": bool(u.get("ok")), "detail": u.get("active_state") or ""})
    for w in overview.get("windows_services") or []:
        voci.append({"kind": "windows_service", "host": w.get("host") or "",
                     "name": w.get("name") or "", "ok": bool(w.get("ok")),
                     "detail": w.get("state") or ""})
    for c in overview.get("healthchecks") or []:
        voci.append({"kind": "healthcheck", "host": "", "name": c.get("name") or "",
                     "ok": bool(c.get("ok")), "detail": (c.get("detail") or "")[:200]})
    return [v for v in voci if v["name"]]


_store: Optional[HistoryStore] = None


def get_history() -> HistoryStore:
    global _store
    if _store is None:
        _store = HistoryStore()
    return _store
