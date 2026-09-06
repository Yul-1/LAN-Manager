/* ===================================================================
   stats.test.mjs — la pagina Stats & Traffico

   Tre difetti trovati nel giro su questa pagina:
     1. la scheda "1 ora" mostrava la finestra viva in memoria, che e'
        di 120 punti all'intervallo rapido: una VENTINA DI MINUTI. La
        pagina diceva il falso;
     2. lo storico veniva scaricato una volta sola: scegliendo 24 ore il
        grafico restava fermo all'istante in cui si era aperta la pagina,
        per sempre, mentre la barra in alto diceva "agg. 3s fa";
     3. la pagina non avvisava mai quando il router non rispondeva, e la
        tabella delle interfacce mostrava numeri vecchi come nuovi.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES, PERIODI, statsHist,"
  + "\n  get periodo() { return statsPeriod; }, set periodo(v) { statsPeriod = v; } };";

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
    className: "", textContent: "", innerHTML: "", value: "", title: "",
    hidden: false, style: {}, dataset: {},
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) {
      if (!memo.has(sel)) memo.set(sel, elemento());
      return memo.get(sel);
    },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, removeEventListener() {},
    appendChild(f) { return f; }, append() {}, remove() {}, focus() {},
    closest: () => null, clientWidth: 600, clientHeight: 240, width: 0, height: 0,
    getContext: () => contesto2D(),
    getBoundingClientRect: () => ({ width: 600, height: 240, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto(risposte = {}) {
  const chiamate = [];
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
    fetch: async (url) => {
      chiamate.push(url);
      const corpo = risposte.corpo || { period: "24h", points: [] };
      return { ok: true, status: 200, text: async () => JSON.stringify(corpo),
               headers: { get: () => "application/json" } };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, chiamate, dammi };
}

/* Serie di N punti a passo di `passo` secondi. */
function serie(n, passo = 10, da = 1_700_000_000_000) {
  return Array.from({ length: n }, (_, i) => ({
    t: da + i * passo * 1000,
    rx_bps: 1_000_000 + i * 1000,
    tx_bps: 200_000,
    latency: 20,
  }));
}


// ── 1. "1 ora" che era una ventina di minuti ───────────────────────

test("la finestra viva non si chiama piu' un'ora", () => {
  const { ctx } = contesto();
  const etichette = Object.fromEntries(ctx.__interni.PERIODI);
  assert.equal(etichette.live, "live", "la serie in memoria e' quella che e': live");
  assert.ok("1h" in etichette, "l'ora vera resta disponibile, dallo storico");
  assert.equal(ctx.__interni.periodo, "live", "e si parte da li'");
});

test("l'arco coperto si dichiara ed e' calcolato dai dati", () => {
  const { ctx } = contesto();
  // 120 punti a 10s = 1190s = ~20 minuti, non un'ora.
  assert.equal(ctx.arcoSerie(serie(120, 10)), "20 minuti");
  assert.equal(ctx.arcoSerie(serie(361, 10)), "60 minuti");
  assert.equal(ctx.arcoSerie(serie(600, 30)), "5 ore");
  assert.equal(ctx.arcoSerie(serie(2, 10)), "meno di un minuto");
  assert.equal(ctx.arcoSerie([]), "", "senza dati non si annuncia un arco");
});

test("live legge la serie in memoria, i periodi lunghi lo storico", () => {
  const { ctx } = contesto();
  const snap = { traffic_series: serie(5) };
  ctx.__interni.periodo = "live";
  assert.equal(ctx.statsSeries(snap).length, 5);
  ctx.__interni.periodo = "24h";
  assert.equal(ctx.statsSeries(snap).length, 0,
    "senza storico in cache non si ripiega sul live spacciandolo per 24 ore");
});


// ── 2. Storico congelato al primo caricamento ──────────────────────

test("lo storico si riscarica dopo un minuto", async () => {
  const { ctx, chiamate } = contesto();
  ctx.__interni.state.snap = { traffic_series: [] };
  ctx.__interni.periodo = "24h";

  await ctx.loadStatsHistory();
  assert.equal(chiamate.length, 1);

  // Subito dopo: la cache vale, niente seconda richiesta ad ogni update.
  await ctx.loadStatsHistory();
  assert.equal(chiamate.length, 1, "un update live non deve riscaricare");

  // Passato il minuto: il grafico non puo' restare fermo per sempre.
  ctx.__interni.statsHist.chiesto = Date.now() - 61000;
  await ctx.loadStatsHistory();
  assert.equal(chiamate.length, 2, "dopo un minuto lo storico si rinfresca");
});

test("un rinfresco fallito non svuota il grafico gia' disegnato", async () => {
  const { ctx } = contesto({ corpo: { period: "24h", points: serie(3) } });
  ctx.__interni.state.snap = { traffic_series: [] };
  ctx.__interni.periodo = "24h";
  await ctx.loadStatsHistory();
  assert.equal(ctx.__interni.statsHist.points.length, 3);

  // La richiesta successiva fallisce: si tiene quello che c'era.
  ctx.api = async () => ({ ok: false, error: { kind: "rete", messaggio: "backend giu'" } });
  ctx.__interni.statsHist.chiesto = Date.now() - 61000;
  await ctx.loadStatsHistory();
  assert.equal(ctx.__interni.statsHist.points.length, 3,
    "sparire sotto gli occhi e' peggio che mostrare l'ultimo dato buono");
});

test("live non interroga lo storico", async () => {
  const { ctx, chiamate } = contesto();
  ctx.__interni.periodo = "live";
  await ctx.loadStatsHistory();
  assert.equal(chiamate.length, 0);
});


// ── 3. Nessun avviso quando il router non risponde ─────────────────

test("la pagina dichiara quando la sorgente e' ferma", () => {
  const { ctx, dammi } = contesto();
  ctx.__interni.state.snap = {
    traffic_series: [], interfaces: [],
    sources: { interfaces: { ok: false, error: "router irraggiungibile",
                             since: Math.floor(Date.now() / 1000) - 300, ts: 1000 } },
  };
  ctx.pageStats(dammi("#view"), ctx.__interni.state.snap);
  assert.ok(ctx.badgeSorgente("interfaces").includes("fermo da"),
    "i numeri vecchi mostrati come nuovi sono una bugia");
});

test("senza interfacce la tabella lo dice invece di restare vuota", () => {
  const { ctx, dammi } = contesto();
  const snap = { traffic_series: [], interfaces: [], sources: {} };
  ctx.__interni.state.snap = snap;
  ctx.pageStats(dammi("#view"), snap);
  assert.ok(dammi("#view").innerHTML.includes("In attesa dati router"));
});


// ── Il riepilogo del periodo ───────────────────────────────────────

test("il riepilogo parla della serie che si sta guardando", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = { meta: { internet_probe: "192.0.2.53" } };
  const html = ctx.statsRiassunto([
    { t: 0, rx_bps: 1_000_000, tx_bps: 100_000, latency: 10 },
    { t: 10_000, rx_bps: 3_000_000, tx_bps: 200_000, latency: 30 },
  ]);
  assert.ok(html.includes("picco RX") && html.includes("3.0 Mbit/s"));
  assert.ok(html.includes("picco TX") && html.includes("200 kbit/s"));
  assert.ok(html.includes("20<span class=\"unit\"> ms</span>"), "media di 10 e 30");
  assert.ok(html.includes("verso 192.0.2.53"), "il bersaglio viene dalla config, non dal codice");
});

test("i buchi non entrano nelle medie ne' nei picchi", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = {};
  const html = ctx.statsRiassunto([
    { t: 0, rx_bps: 1_000_000, tx_bps: 1000, latency: 10 },
    { t: 10_000, rx_bps: null, tx_bps: null, latency: null },
    { t: 20_000, rx_bps: 3_000_000, tx_bps: 1000, latency: 30 },
  ]);
  assert.ok(html.includes("3.0 Mbit/s"), "il picco ignora il buco");
  assert.ok(html.includes("20<span class=\"unit\"> ms</span>"), "e la media pure");
});

test("senza dati il riepilogo non inventa zeri", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = {};
  const html = ctx.statsRiassunto([]);
  assert.equal((html.match(/>—</g) || []).length, 4, "quattro trattini, non quattro zeri");
});

test("il volume si integra sull'intervallo, saltando i buchi", () => {
  const { ctx } = contesto();
  // 1 Mbit/s per 10 secondi = 10 Mbit = 1.25 MB
  const uno = ctx.volumeStimato(
    [{ t: 0 }, { t: 10_000, rx_bps: 1_000_000 }], "rx_bps");
  assert.equal(Math.round(uno), 1_250_000);
  const conBuco = ctx.volumeStimato(
    [{ t: 0 }, { t: 10_000, rx_bps: null }, { t: 20_000, rx_bps: 1_000_000 }], "rx_bps");
  assert.equal(Math.round(conBuco), 1_250_000, "il tratto mancante non si conta");
});

test("la tabella delle interfacce si aggiorna quando arrivano i dati", () => {
  // Difetto colto guardando la pagina vera: `refreshStats` ridisegnava solo i
  // canvas, quindi la tabella restava quella del momento in cui si era entrati
  // — e arrivando sulla pagina prima del primo snapshot restava vuota per
  // sempre, dicendo "in attesa" con i dati gia' in mano.
  const { ctx, dammi } = contesto();
  const vuoto = { traffic_series: [], interfaces: [], sources: {} };
  ctx.__interni.state.snap = vuoto;
  ctx.pageStats(dammi("#view"), vuoto);
  assert.ok(dammi("#st-if").innerHTML.includes("In attesa"));

  const pieno = { traffic_series: [], sources: {}, interfaces: [
    { name: "wan", ifname: "eth0", up: true, ip4: ["203.0.113.7"], rx_mb: 10, tx_mb: 2 }] };
  ctx.__interni.state.snap = pieno;
  assert.equal(ctx.refreshStats(dammi("#view"), pieno), true);
  assert.ok(dammi("#st-if").innerHTML.includes("eth0"));
  assert.ok(!dammi("#st-if").innerHTML.includes("In attesa"));
});

test("la nota sui contatori condivisi compare solo quando serve", () => {
  const { ctx } = contesto();
  const sola = ctx.statsInterfacce({ interfaces: [{ name: "wan", ifname: "eth0", up: true }] });
  assert.ok(!sola.includes("stesso dispositivo"));
  const condivise = ctx.statsInterfacce({ interfaces: [
    { name: "lan", ifname: "br-lan", up: true, shared_with: ["guest"], counters_own: true },
    { name: "guest", ifname: "br-lan", up: true, shared_with: ["lan"], counters_own: false }] });
  assert.ok(condivise.includes("stesso dispositivo di"));
  assert.ok(condivise.includes("contati su"), "la rete che non ha i contatori suoi rimanda all'altra");
});
