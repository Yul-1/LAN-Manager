/* ===================================================================
   resources.test.mjs — Monitoraggio · Risorse

   Tre difetti:
     1. REGRESSIONE introdotta lavorando su Stats: `PERIODI` e' condiviso
        fra le due pagine, e la scheda "live" aggiunta li' compariva anche
        qui — dove pero' `resPeriod === "1h"` significava "serie viva".
        Cliccando "live" la pagina chiedeva al backend un periodo che non
        esiste (400) e i grafici restavano vuoti;
     2. lo storico per host si scaricava una volta sola: su 24 ore o 7
        giorni i grafici restavano fermi all'istante in cui si era aperta
        la pagina;
     3. un host che falliva il rinfresco azzerava la cache di TUTTI, e il
        suo grafico si svuotava invece di mostrare l'ultimo dato buono;
     4. la nota di testa restava "Prima raccolta in corso…" per sempre a
        chi entrava nella pagina prima della prima lettura: il refresh
        parziale riscriveva solo l'elenco degli host (visto sulla UI vera
        servita da homeserver, 0.1.45).
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, PERIODI, resHist,"
  + "\n  get periodo() { return resPeriod; }, set periodo(v) { resPeriod = v; } };";

/* Contesto 2D finto: i grafici disegnano davvero, e senza questo `_prep`
   fallisce su `ctx.scale`. Nessuna asserzione guarda i pixel. */
function contesto2D() {
  const grad = { addColorStop() {} };
  return new Proxy({}, {
    get: (_t, k) => (k === "createLinearGradient" ? () => grad : () => {}),
    set: () => true,
  });
}

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, appendChild(f) { return f; },
    append() {}, remove() {}, focus() {}, closest: () => null,
    clientWidth: 600, clientHeight: 200, width: 0, height: 0,
    getContext: () => contesto2D(),
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto(esito = { ok: true }) {
  const chiamate = [];
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url) => {
      chiamate.push(url);
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify({ points: [{ t: 1, cpu: 5 }] }) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, chiamate, dammi };
}

const risorse = (over = {}) => Object.assign({
  enabled: true, reachable: 2, total: 2, interval: 60, ts: 1000,
  hosts: [
    { host: "192.0.2.10", name: "uno", reachable: true, series: [{ t: 1, cpu: 5 }, { t: 60001, cpu: 6 }] },
    { host: "192.0.2.11", name: "due", reachable: true, series: [{ t: 1, cpu: 9 }] },
  ],
}, over);


// ── 1. La regressione delle schede ─────────────────────────────────

test("le due pagine usano le stesse schede, e 'live' significa la stessa cosa", () => {
  const { ctx } = contesto();
  // Lo spread riporta l'array fuori dal contesto vm: deepEqual confronta anche
  // il prototipo, e due Array di realm diversi non sono mai uguali.
  const chiavi = [...ctx.__interni.PERIODI.map(([v]) => v)];
  assert.deepEqual(chiavi, ["live", "1h", "24h", "7d"]);
  assert.equal(ctx.__interni.periodo, "live", "Risorse parte dalla serie viva, come Stats");
});

test("su 'live' non si chiede allo storico un periodo che non esiste", async () => {
  // Il backend ammette solo 1h/24h/7d: `period=live` rispondeva 400 e i
  // grafici restavano vuoti senza che nulla lo spiegasse.
  const { ctx, chiamate } = contesto();
  ctx.__interni.periodo = "live";
  await ctx.loadResourcesHistory(risorse());
  assert.equal(chiamate.length, 0);
});

test("su 'live' i grafici leggono la serie che arriva nello snapshot", () => {
  const { ctx } = contesto();
  const r = risorse();
  ctx.__interni.periodo = "live";
  assert.equal(ctx.resourceSeries(r.hosts[0]).length, 2);
  ctx.__interni.periodo = "24h";
  assert.equal(ctx.resourceSeries(r.hosts[0]).length, 0,
    "senza storico in cache non si ripiega sulla serie viva spacciandola per 24 ore");
});


// ── 2. Storico congelato ───────────────────────────────────────────

test("lo storico degli host si riscarica dopo un minuto", async () => {
  const { ctx, chiamate } = contesto();
  ctx.__interni.state.snap = { resources: risorse() };
  ctx.__interni.periodo = "24h";
  await ctx.loadResourcesHistory(risorse());
  assert.equal(chiamate.length, 2, "una richiesta per host");
  await ctx.loadResourcesHistory(risorse());
  assert.equal(chiamate.length, 2, "subito dopo vale la cache");
  ctx.__interni.resHist.chiesto = Date.now() - 61000;
  await ctx.loadResourcesHistory(risorse());
  assert.equal(chiamate.length, 4, "dopo un minuto si rinfresca");
});


// ── 3. Un host rotto non deve svuotare gli altri ───────────────────

test("un host che fallisce il rinfresco non cancella i punti gia' presi", async () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = { resources: risorse() };
  ctx.__interni.periodo = "24h";
  await ctx.loadResourcesHistory(risorse());
  assert.equal(Object.keys(ctx.__interni.resHist.per_host).length, 2);

  // Il secondo host smette di rispondere.
  ctx.api = async (url) => url.includes("192.0.2.11")
    ? { ok: false, error: { kind: "rete", messaggio: "host giu'" } }
    : { ok: true, data: { points: [{ t: 2, cpu: 7 }] } };
  ctx.__interni.resHist.chiesto = Date.now() - 61000;
  await ctx.loadResourcesHistory(risorse());
  assert.equal(ctx.__interni.resHist.per_host["192.0.2.11"].length, 1,
    "i suoi punti restano: svuotare il grafico e' peggio che mostrare l'ultimo dato buono");
  assert.equal(ctx.__interni.resHist.per_host["192.0.2.10"][0].t, 2, "l'altro si aggiorna");
  assert.ok(ctx.__interni.resHist.stato.includes("host giu'"), "e l'errore si dichiara");
});


// ── 4. La nota di testa deve seguire i dati ────────────────────────

test("la nota di testa smette di dire 'prima raccolta' quando i dati arrivano", () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  // Si entra nella pagina prima che la raccolta abbia prodotto qualcosa.
  ctx.__interni.state.snap = { resources: { enabled: true, hosts: [] } };
  ctx.pageResources(view, ctx.__interni.state.snap);
  assert.ok(dammi("#res-testa").innerHTML === "" || view.innerHTML.includes("Prima raccolta"),
    "al primo render la nota dichiara che la raccolta non e' ancora arrivata");

  const pieno = { resources: risorse() };
  ctx.__interni.state.snap = pieno;
  ctx.refreshResources(view, pieno);
  const testa = dammi("#res-testa").innerHTML;
  assert.ok(!testa.includes("Prima raccolta"),
    "col refresh parziale la nota restava li' mentre sotto c'erano le schede piene");
  assert.ok(testa.includes("host su"), "e dice quanti host rispondono");
});

test("la nota di testa segnala la sorgente caduta anche senza un ts nuovo", () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  ctx.__interni.state.snap = { resources: risorse() };
  ctx.pageResources(view, ctx.__interni.state.snap);
  const rotto = { resources: risorse({ warning: "SSH non raggiungibile" }) };
  ctx.refreshResources(view, rotto);
  assert.ok(dammi("#res-testa").innerHTML.includes("SSH non raggiungibile"));
});


// ── Host che non parlano Linux ─────────────────────────────────────

function hostWindows(extra = {}) {
  return Object.assign({
    host: "192.0.2.12", name: "pc-windows", os: "windows", reachable: true,
    uptime_human: "4g 18h", load: [], series: [],
    cpu: { percent: 12.4, cores: 8, per_core: [12, 13], model: "Core i7", window_seconds: 16 },
    memory: { total: 16 * 1024 ** 3, used: 12 * 1024 ** 3, used_pct: 73.6 },
    swap: { total: 0, used: 0, used_pct: null },
    disks: [], disk_io: [], temperatures: [], processes: [],
  }, extra);
}

test("un host Windows si riconosce dalla scheda", () => {
  const { ctx } = contesto();
  const html = ctx.hostCard(hostWindows(), 0);
  assert.ok(html.includes(">Windows<"), "la targhetta dice quale sistema e'");
  assert.ok(html.includes("192.0.2.12"), "l'IP resta al suo posto");
});

test("un host Linux non porta targhette inutili", () => {
  // Su una LAN di macchine Linux una targhetta su ognuna sarebbe solo rumore.
  const { ctx } = contesto();
  assert.ok(!ctx.hostCard(hostWindows({ os: "linux" }), 0).includes(">Windows<"));
});

test("senza load average la scheda scrive un trattino, non zero", () => {
  /* Windows non ha un carico medio: mostrare 0.00 direbbe "macchina a riposo",
     che e' il contrario di "non lo so". */
  const { ctx } = contesto();
  const html = ctx.hostCard(hostWindows(), 0);
  assert.ok(!html.includes("0.00"), html.slice(0, 400));
});

test("anche un host Windows irraggiungibile si riconosce", () => {
  // Il motivo del guasto e il sistema servono insieme: senza il secondo non si
  // capisce perche' quell'host abbia una scheda diversa dalle altre.
  const { ctx } = contesto();
  const html = ctx.hostCard(hostWindows({ reachable: false, error: "porta 22 filtrata" }), 0);
  assert.ok(html.includes(">Windows<"));
  assert.ok(html.includes("porta 22 filtrata"));
});


// ── Gli host accesi vanno in cima ──────────────────────────────────

function elencoRisorse() {
  // Ordine di configurazione: uno spento in mezzo a due accesi, e uno saltato
  // perche' il ping l'ha visto giu' (che e' comunque "non online").
  return {
    ts: 1, enabled: true, total: 4, reachable: 2, interval: 60,
    hosts: [
      { host: "192.0.2.10", name: "nas-spento", reachable: false, series: [],
        error: "porta 22 filtrata" },
      hostWindows({ host: "192.0.2.12", name: "pc-windows" }),
      { host: "192.0.2.13", name: "portatile", reachable: false, skipped: true,
        series: [], error: "host non raggiungibile (nessuna risposta al ping)" },
      hostWindows({ host: "192.0.2.30", name: "server", os: "linux",
                    load: [0.4, 0.5, 0.6] }),
    ],
  };
}

test("gli host accesi stanno in cima all'elenco", () => {
  const { ctx } = contesto();
  assert.deepEqual(ctx.hostsOrdinati(elencoRisorse()).map(x => x.name),
    ["pc-windows", "server", "nas-spento", "portatile"]);
});

test("a parita' di stato l'ordine della configurazione non cambia", () => {
  /* Sort stabile: senza, l'elenco si rimescolerebbe ad ogni ciclo e leggere
     una scheda mentre la pagina si aggiorna diventerebbe una caccia. */
  const { ctx } = contesto();
  const r = elencoRisorse();
  assert.deepEqual(ctx.hostsOrdinati(r).filter(x => !x.reachable).map(x => x.name),
    ["nas-spento", "portatile"]);
  assert.deepEqual(ctx.hostsOrdinati(r).filter(x => x.reachable).map(x => x.name),
    ["pc-windows", "server"]);
});

test("l'elenco dello snapshot non viene riordinato sotto i piedi a nessuno", () => {
  // Lo snapshot e' condiviso con alert e storico: l'ordinamento e' una scelta
  // di questa pagina, non un effetto sui dati.
  const { ctx } = contesto();
  const r = elencoRisorse();
  ctx.hostsOrdinati(r);
  assert.deepEqual(r.hosts.map(x => x.name),
    ["nas-spento", "pc-windows", "portatile", "server"]);
});

test("le schede e i grafici usano lo stesso ordine", () => {
  /* I canvas si agganciano per indice: ordinare solo le schede avrebbe messo
     il grafico di un host sotto la scheda di un altro. */
  const { ctx } = contesto();
  const r = elencoRisorse();
  const html = ctx.resourceCards(r);
  const nomiInPagina = [...html.matchAll(/<h3>([^<]+)/g)].map(m => m[1]);
  assert.deepEqual(nomiInPagina, ctx.hostsOrdinati(r).map(x => x.name));
});

test("senza host la pagina lo dice invece di mostrare una griglia vuota", () => {
  const { ctx } = contesto();
  assert.ok(ctx.resourceCards({ hosts: [] }).includes("Nessun host da interrogare"));
});


/* ── Lo zoom dei grafici e la riscrittura delle schede (2026-09-04) ─
   `refreshResources` riscrive `#res-hosts` ad ogni cambio di `resources.ts`,
   quindi il canvas viene distrutto e ricreato: circa ogni 60 secondi, e subito
   dopo ogni "aggiorna ora". Con lo stato dello zoom appeso al nodo, zoomare qui
   durava al massimo un intervallo di raccolta e poi si azzerava da solo, senza
   che niente lo spiegasse. Ora la finestra sta in un registro indicizzato per
   host, e il nodo nuovo la ritrova. */

test("ogni grafico host porta la chiave del suo host, non la sua posizione", () => {
  // Le schede si riordinano (gli accesi in cima): con l'indice come chiave lo
  // zoom finirebbe sul grafico di un altro.
  const { ctx } = contesto();
  const r = elencoRisorse();
  const html = ctx.resourceCards(r);
  const chiavi = [...html.matchAll(/data-chart-key="([^"]+)"/g)].map(m => m[1]);
  const attese = ctx.hostsOrdinati(r).map(x => `res:${x.host}`);

  // Non tutti gli host hanno un grafico: quelli che non hanno mai risposto
  // mostrano il motivo al posto della scheda piena. Le chiavi che ci sono
  // devono pero' essere host veri, tutte diverse, e nell'ordine delle schede.
  assert.ok(chiavi.length, "nessuna chiave nei grafici");
  assert.deepEqual(chiavi, attese.filter(k => chiavi.includes(k)));
  assert.equal(new Set(chiavi).size, chiavi.length, "due schede con la stessa chiave");
  chiavi.forEach(k => assert.match(k, /^res:\d+\.\d+\.\d+\.\d+$/,
    `${k} non e' un host: con l'indice lo zoom finirebbe sul grafico di un altro`));
});

test("lo zoom di un host resta dopo che le schede sono state riscritte", () => {
  const { ctx } = contesto();
  const serie = Array.from({ length: 40 }, (_, i) => ({ t: 1e12 + i * 60e3, cpu: 10 + i, mem: 40 }));
  const linee = [{ get: p => p.cpu, color: "#4dd6e0", label: "CPU" }];
  const chiave = "res:" + elencoRisorse().hosts[0].host;

  // Il canvas della prima raccolta, zoomato.
  const primo = canvasConChiave(ctx, chiave);
  ctx.drawLineChart(primo, serie, linee, { left: "pct", leftTop: 100 });
  ctx.zoomGrafico(primo, 0.5, 0.4);
  const atteso = { t0: primo._vista.t0, t1: primo._vista.t1 };

  // Arriva un ts nuovo: la scheda si riscrive, il nodo e' un altro.
  const secondo = canvasConChiave(ctx, chiave);
  ctx.drawLineChart(secondo, serie, linee, { left: "pct", leftTop: 100 });

  assert.ok(secondo._vista, "il grafico ricreato ha perso lo zoom");
  assert.equal(secondo._vista.t0, atteso.t0);
  assert.equal(secondo._vista.t1, atteso.t1);
});

/* Un canvas con la chiave dell'host, come quello che genera `resourceCards`. */
function canvasConChiave(ctx, chiave) {
  const cv = elemento();
  cv.dataset = { chartKey: chiave };
  cv.clientWidth = 600; cv.clientHeight = 180;
  cv.getBoundingClientRect = () => ({ width: 600, height: 180, top: 0, left: 0 });
  cv.listener = {};
  cv.addEventListener = () => {};
  return cv;
}
