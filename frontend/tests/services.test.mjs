/* ===================================================================
   services.test.mjs — Monitoraggio · Servizi

   Il difetto da cui parte questa pagina: togliendo `WireGuard relay (EC2)`
   dal monitoraggio, il pulsante ✕ sembrava non fare nulla. La cancellazione
   funzionava — il backend riscriveva `services.yaml` — ma la pagina si
   ridisegnava dallo snapshot in memoria, che ricalcola i servizi solo nel
   giro lento (60 secondi). Per un minuto la riga restava li', senza nessuna
   conferma, e l'unica conclusione possibile era che il pulsante fosse rotto.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES };";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", title: "",
    hidden: false, checked: false, style: {}, dataset: {}, disabled: false,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) {
      if (!memo.has(sel)) memo.set(sel, elemento());
      return memo.get(sel);
    },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, removeEventListener() {},
    appendChild(f) { return f; }, append() {}, remove() {}, focus() {},
    closest: () => null, getContext: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto() {
  const chiamate = [];
  const toasts = [];
  const nodi = new Map();
  const dammi = (sel) => {
    if (!nodi.has(sel)) nodi.set(sel, elemento());
    return nodi.get(sel);
  };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: {
      body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
      querySelector: dammi, querySelectorAll: () => [], addEventListener() {},
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    confirm: () => true,
    fetch: async (url, opt) => {
      chiamate.push({ url, metodo: (opt || {}).method || "GET" });
      return { ok: true, status: 200, text: async () => "{}",
               headers: { get: () => "application/json" } };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  // Siamo sulla pagina Servizi: `renderIfLive` deve aggiornare questa, non
  // provare a ridisegnare i grafici della dashboard.
  ctx.__interni.state.route = "services";
  ctx.toast = (testo, o) => toasts.push({ testo, level: (o || {}).level });
  return { ctx, chiamate, toasts, dammi };
}

function snap() {
  return {
    services: {
      summary: { total: 4, ok: 3, down: 1 },
      docker: {
        containers: [
          { kind: "docker", name: "portainer", label: "Portainer", running: true,
            status: "running", host: "user", pinned: true, dashboard: true,
            url: "http://192.0.2.9:9000", image: "portainer/portainer-ce", ports: [] },
          { kind: "docker", name: "searxng", label: "searxng", running: true,
            status: "running", host: "user", pinned: false, dashboard: false,
            url: "", image: "searxng/searxng", ports: [] },
        ],
        hosts: [{ name: "user", reachable: true }],
        summary: { running: 2, stopped: 0, total: 2 },
      },
      systemd: [
        { kind: "systemd", name: "docker.service", label: "Docker Engine", ok: true,
          available: true, active_state: "active", sub_state: "running",
          critical: true, dashboard: true, host: "" },
        { kind: "systemd", name: "cron.service", label: "Cron", ok: false,
          available: true, active_state: "failed", sub_state: "failed",
          critical: false, dashboard: false, host: "" },
      ],
      windows_services: [
        { kind: "windows_service", name: "Spooler", label: "Coda di stampa",
          host: "192.0.2.12", ok: false, available: true, state: "stopped",
          start_type: "Automatic", critical: true, dashboard: true },
        { kind: "windows_service", name: "NonEsisto", label: "NonEsisto",
          host: "192.0.2.12", ok: false, available: true, state: "not-found",
          start_type: "", critical: false, dashboard: false },
        { kind: "windows_service", name: "sshd", label: "OpenSSH SSH Server",
          host: "192.0.2.12", ok: false, available: false, state: "unknown",
          error: "connessione rifiutata", critical: false, dashboard: false },
      ],
      healthchecks: [
        { kind: "healthcheck", name: "Router LuCI", type: "http", target: "http://192.0.2.1",
          ok: true, latency_ms: 12, detail: "HTTP 200", dashboard: true },
      ],
    },
    alerts: [], alerts_summary: {}, sources: {}, collector: {},
  };
}


// ── Il difetto del pulsante ✕ ──────────────────────────────────────

test("togliere un servizio lo fa sparire subito, non fra un minuto", async () => {
  const { ctx, toasts } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  await ctx.removeService("systemd", "cron.service");
  assert.deepEqual(s.services.systemd.map(u => u.name), ["docker.service"],
    "la riga deve sparire dallo snapshot in memoria, non attendere il giro lento");
  assert.equal(toasts.length, 1, "e l'esito va detto: senza conferma sembra rotto");
  assert.equal(toasts[0].level, "ok");
});

test("i conteggi seguono la rimozione", async () => {
  const { ctx } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  await ctx.removeService("systemd", "cron.service");   // era down
  assert.deepEqual(s.services.summary, { total: 3, ok: 3, down: 0 });
  await ctx.removeService("http", "Router LuCI");        // era ok
  assert.deepEqual(s.services.summary, { total: 2, ok: 2, down: 0 });
});

test("togliere il pin di un container non fa sparire il container", async () => {
  // I container sono scoperti da soli: il catalogo da' solo nome, URL e la
  // scelta per la dashboard. Farlo sparire dall'elenco sarebbe una bugia.
  const { ctx, toasts } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  await ctx.removeService("docker", "portainer");
  const c = s.services.docker.containers.find(x => x.name === "portainer");
  assert.ok(c, "il container resta in elenco");
  assert.equal(c.pinned, false);
  assert.equal(c.dashboard, false, "e sparisce anche dalla dashboard");
  assert.equal(c.url, "");
  assert.equal(c.label, "portainer", "torna al nome vero");
  assert.equal(s.services.summary.total, 4, "i conteggi non cambiano");
  assert.ok(toasts[0].testo.includes("il container resta"));
});

test("dopo la rimozione si chiede al backend di ricalcolare subito", async () => {
  const { ctx, chiamate } = contesto();
  ctx.__interni.state.snap = snap();
  await ctx.removeService("systemd", "cron.service");
  const url = chiamate.map(c => c.url).join(" ");
  assert.ok(url.includes("/api/services/config/systemd/cron.service"));
  assert.ok(url.includes("/api/services/refresh"),
    "cosi' la verita' arriva in due secondi invece che al prossimo giro lento");
});

test("se la cancellazione fallisce non si finge che sia andata", async () => {
  const { ctx, toasts } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  ctx.api = async () => ({ ok: false, error: { kind: "rete", messaggio: "backend giu'" } });
  let errori = 0;
  ctx.toastErrore = () => { errori += 1; };
  await ctx.removeService("systemd", "cron.service");
  assert.equal(s.services.systemd.length, 2, "la riga resta: non e' stata rimossa davvero");
  assert.equal(errori, 1);
  assert.equal(toasts.length, 0, "e nessuna conferma di successo");
});

test("senza conferma non si cancella niente", async () => {
  const { ctx, chiamate } = contesto();
  ctx.confirm = () => false;
  ctx.__interni.state.snap = snap();
  await ctx.removeService("systemd", "cron.service");
  assert.equal(chiamate.length, 0);
});


// ── L'aggiunta aveva lo stesso difetto ─────────────────────────────

test("il pin di un container scoperto si fa dalla sua riga", () => {
  // Prima l'unico modo era "+ Monitora servizio" e riscrivere a mano il nome
  // esatto del container, che sta gia' nella riga accanto.
  const { ctx } = contesto();
  const html = ctx.svcDocker(snap());
  assert.ok(html.includes('data-svc-pin="searxng"'), "il container non pinnato offre il pulsante");
  assert.ok(!html.includes('data-svc-pin="portainer"'), "quello gia' pinnato no");
  assert.ok(html.includes('data-svc-del="portainer"'));
});

test("chi e' in dashboard lo dichiara nell'elenco", () => {
  const { ctx } = contesto();
  assert.ok(ctx.svcDocker(snap()).includes(">dashboard<"));
  assert.ok(ctx.svcSystemd(snap()).includes(">dashboard<"));
  assert.ok(ctx.svcHealth(snap()).includes(">dashboard<"));
});


// ── Aggiornamento parziale ─────────────────────────────────────────

test("la pagina si aggiorna per parti", () => {
  const { ctx, dammi } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  const view = dammi("#view");
  ctx.pageServices(view, s);
  const dopoIlRender = view.innerHTML;
  assert.equal(ctx.refreshServices(view, s), true);
  assert.equal(view.innerHTML, dopoIlRender,
    "il refresh non deve riscrivere il contenitore della pagina");
  assert.equal(typeof ctx.__interni.PAGES.services.refresh, "function");
});

test("i riquadri in alto non si dichiarano verdi senza dati", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = {};
  const html = ctx.svcKpi({});
  assert.ok(html.includes("in attesa…"));
  assert.ok(!html.includes("val green"));
});

test("lo stato delle unit e degli healthcheck e' un chip leggibile", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = snap();
  assert.ok(ctx.svcSystemd(snap()).includes("in errore"), "failed si legge in italiano");
  assert.ok(ctx.svcHealth(snap()).includes("12 ms"));
});


// ── Servizi Windows ────────────────────────────────────────────────

test("un servizio Windows fermo non viene letto come un healthcheck", () => {
  /* Il difetto che questo test blocca: senza un ramo suo in statoLeggibile la
     voce cade in fondo, fra gli healthcheck, e la colonna stato scrive
     "NaN ms" per un servizio che non ha nessuna latenza da mostrare. */
  const { ctx } = contesto();
  const [fermo, mancante, ignoto] = snap().services.windows_services;
  assert.equal(ctx.statoLeggibile(fermo), "fermo");
  assert.equal(ctx.statoLeggibile(mancante), "non installato");
  assert.equal(ctx.statoLeggibile(ignoto), "sconosciuto");
  for (const w of [fermo, mancante, ignoto]) {
    assert.ok(!ctx.chipStato(w).includes("NaN"), "nessuna latenza inventata");
  }
});

test("un host che non risponde non diventa un servizio fermo", () => {
  // "Non so" e "so che e' fermo" devono restare due cose distinte anche a
  // colpo d'occhio: il chip ignoto e' grigio, non rosso.
  const { ctx } = contesto();
  const ignoto = snap().services.windows_services[2];
  assert.ok(ctx.chipStato(ignoto).includes("chip ignoto"));
  assert.ok(ctx.chipStato(snap().services.windows_services[0]).includes("chip giu"));
});

test("la tabella dice host, nome breve e tipo di avvio", () => {
  const { ctx } = contesto();
  const html = ctx.svcWindows(snap());
  assert.ok(html.includes("192.0.2.12"), "l'host e' sempre mostrato: e' obbligatorio");
  assert.ok(html.includes("Coda di stampa"));
  assert.ok(html.includes("avvio Automatic"));
  assert.ok(html.includes('data-kind="windows_service"'));
});

test("senza servizi Windows la scheda lo dice invece di sparire", () => {
  const { ctx } = contesto();
  assert.ok(ctx.svcWindows({ services: {} }).includes("Nessun servizio Windows"));
});

test("i servizi Windows scelti compaiono in dashboard", () => {
  const { ctx } = contesto();
  const html = ctx.servicesMini(snap());
  assert.ok(html.includes("Coda di stampa"), "la voce con la spunta c'e'");
  assert.ok(!html.includes("NonEsisto"), "quella senza spunta no");
});

test("i servizi host si contano insieme, senza un quinto riquadro", () => {
  // La griglia dei KPI e' a quattro: un riquadro in piu' romperebbe la riga.
  const { ctx } = contesto();
  const html = ctx.svcKpi(snap());
  assert.ok(html.includes("servizi host attivi"));
  // 1 systemd attiva su 2, piu' 0 servizi Windows attivi su 3.
  assert.ok(html.includes(">1<span class=\"unit\">/5</span>"), html);
});

test("togliere un servizio Windows lo fa sparire subito", async () => {
  const { ctx } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  await ctx.removeService("windows_service", "Spooler");
  assert.equal(s.services.windows_services.length, 2);
  assert.ok(!s.services.windows_services.some(w => w.name === "Spooler"));
  // Era gia' non-ok: cala il totale e cala il conteggio dei non rispondenti.
  assert.equal(s.services.summary.total, 3);
  assert.equal(s.services.summary.down, 0);
});


/* ── L'update che arriva subito dopo (2026-09-04) ───────────────────
   Il difetto segnalato dal proprietario: cancellato un servizio, la riga
   spariva e **tornava un attimo dopo**. La rimozione ottimistica qui sotto
   funzionava; quello che la disfaceva era il giro veloce del collector, che
   ogni 10 secondi ribroadcasta l'intero snapshot e fa `state.snap = msg.data`.
   Finche' il backend non toglieva il servizio anche dalla sua vista — cosa che
   faceva solo nel giro lento, fino a 60 secondi dopo — l'update rimetteva la
   riga al suo posto.

   La correzione sta sul server (`collector.dimentica_servizio`, chiamata dentro
   la DELETE). Questi test descrivono il contratto dalla parte del client. */

test("l'update che segue la cancellazione non fa tornare la riga", async () => {
  const { ctx } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  await ctx.removeService("systemd", "cron.service");
  assert.equal(s.services.systemd.length, 1, "presupposto: la riga e' sparita");

  // Il backend ha potato la sua vista: l'update che arriva non la contiene.
  const dalServer = snap();
  dalServer.services.systemd = dalServer.services.systemd.filter(
    x => x.name !== "cron.service");
  ctx.__interni.state.snap = dalServer;

  const nomi = ctx.__interni.state.snap.services.systemd.map(x => x.name);
  assert.ok(!nomi.includes("cron.service"), "la riga cancellata e' tornata");
});

test("se il ricalcolo e' in cooldown lo si dice invece di tacere", async () => {
  // Il cooldown e' `max_hits=1` su 10 secondi ed e' condiviso con "Aggiorna
  // ora" e col salvataggio di un servizio: due azioni ravvicinate danno 429.
  // La cancellazione e' comunque avvenuta; sono gli stati a restare vecchi.
  const { ctx, toasts } = contesto();
  ctx.__interni.state.snap = snap();
  const vera = ctx.api;
  ctx.api = async (url, opt) => (url.includes("/refresh")
    ? { ok: false, error: { kind: "http", messaggio: "429" } }
    : vera(url, opt));

  await ctx.removeService("systemd", "cron.service");
  const testi = toasts.map(t => t.testo).join(" | ");
  assert.match(testi, /non e' piu' monitorato/, "la cancellazione e' avvenuta");
  assert.match(testi, /prossimo giro/, "e il ritardo degli stati va detto");
});

test("una cancellazione a vuoto non si dichiara riuscita", async () => {
  // Il backend risponde 404 quando nel catalogo non c'era niente da togliere
  // (un container mai pinnato, o una voce che sta solo nel file di esempio).
  // Prima rispondeva 200 con `count: 0` e la pagina diceva "fatto".
  const { ctx, toasts } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  ctx.api = async () => ({ ok: false, status: 404,
                           error: { kind: "http", messaggio: "non e' nel catalogo" } });
  let errori = 0;
  ctx.toastErrore = () => { errori += 1; };

  await ctx.removeService("systemd", "cron.service");
  assert.equal(s.services.systemd.length, 2, "non e' stato tolto niente davvero");
  assert.equal(errori, 1);
  assert.equal(toasts.length, 0, "e nessuna conferma di successo");
});
