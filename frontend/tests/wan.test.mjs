/* ===================================================================
   wan.test.mjs — Pagina WAN

   La pagina piu' povera dell'applicazione: una tabella con nome, IP,
   stato e due contatori. Difetti trovati guardando cosa il backend
   sapeva gia' e la pagina non diceva (dati veri di homeserver:
   uplink `tethering`/eth1 con IP 192.168.9.178, probe 8.8.8.8 a 41 ms):
     1. non diceva se internet funziona, pur avendo la misura nello
        snapshot (`traffic_series[].latency`) — la domanda per cui si
        apre una pagina WAN;
     2. non diceva la velocita' attuale, solo i totali dall'avvio;
     3. l'indirizzo dell'uplink e' privato (4G in CGNAT): mostrato
        nudo sembra l'indirizzo pubblico;
     4. se l'uplink non veniva riconosciuto diceva "Nessuna interfaccia
        WAN", la stessa frase del router che non risponde;
     5. i tre endpoint `/api/wan/*` (status, ping, firewall) non erano
        raggiungibili da nessuna pagina: ping e firewall non esistevano
        nella UI;
     6. nessun aggiornamento parziale.
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
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, appendChild(f) { return f; },
    append() {}, remove() {}, focus() {}, closest: () => null, getContext: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto(risposte = {}) {
  const chiamate = [];
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi,
                querySelectorAll: (sel) => sel === ".src-nota" ? [dammi(".src-nota")] : [],
                addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url) => {
      chiamate.push(url);
      const corpo = Object.entries(risposte).find(([k]) => url.includes(k));
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(corpo ? corpo[1] : {}) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate };
}

const ora = () => Date.now();

/* Lo stato vero di homeserver: uplink tethering (eth1) su un 4G in CGNAT. */
function snap(over = {}) {
  const t = ora();
  const s = {
    wan_interface: "tethering",
    meta: { internet_probe: "8.8.8.8", router_name: "gateway",
            wan_candidates: ["wan", "wan6", "usb0", "wwan", "tethering"], subnets: [] },
    sources: { interfaces: { ok: true, ts: Math.floor(t / 1000) - 8 } },
    interfaces: [
      { name: "lan", ifname: "br-lan", up: true, ip4: ["192.0.2.1"], rx_mb: 22899.7, tx_mb: 57922.1 },
      { name: "tethering", ifname: "eth1", up: true, ip4: ["192.168.9.178"], rx_mb: 43.0, tx_mb: 1050.6 },
      { name: "wan", ifname: "", up: false, ip4: [], rx_mb: 0, tx_mb: 0 },
    ],
    traffic_series: [
      { t: t - 20000, rx_bps: 900000, tx_bps: 120000, latency: 39.8 },
      { t: t - 10000, rx_bps: 1200000, tx_bps: 250000, latency: 41.4 },
    ],
    alerts: [], devices: [], services: {}, docker: {}, system: {}, resources: {}, wireguard: {},
  };
  return Object.assign(s, over);
}

function pagina(ctx, s) {
  const view = elemento();
  ctx.__interni.state.snap = s;
  ctx.pageWan(view, s);
  return view.innerHTML;
}


// ── 1 e 2. Le domande per cui si apre la pagina ────────────────────

test("la pagina dice se internet funziona, e con che latenza", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap());
  assert.ok(html.includes("internet"), "manca il riquadro internet");
  assert.ok(/41<span class="unit">ms/.test(html), "la misura c'era nello snapshot e non si vedeva");
  assert.ok(html.includes("8.8.8.8"), "e si dice verso chi e' misurata (dal meta, non scritta qui)");
});

test("quando il probe non risponde lo dichiara, e dice da quando", () => {
  const { ctx } = contesto();
  const s = snap();
  s.traffic_series[1].latency = null;         // ultima misura fallita
  const html = pagina(ctx, s);
  assert.ok(html.includes("non risponde"));
  assert.ok(/ultima risposta \d+/.test(html), "l'ultima risposta buona resta un'informazione");
});

test("mostra la velocita' di adesso, non solo i totali dall'avvio", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap());
  assert.ok(html.includes("1.2 Mbit/s"), "velocita' in ingresso");
  assert.ok(html.includes("totali"), "e i contatori restano, dichiarati come totali");
});


// ── 3. L'indirizzo privato ─────────────────────────────────────────

test("un uplink con indirizzo privato non viene spacciato per pubblico", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap());
  assert.ok(html.includes("indirizzo privato"),
    "192.168.9.178 e' dietro un altro router: mostrarlo nudo inganna");
});

test("con un indirizzo pubblico la nota non compare", () => {
  const { ctx } = contesto();
  const s = snap();
  s.interfaces[1].ip4 = ["203.0.113.7"];
  assert.ok(!pagina(ctx, s).includes("indirizzo privato"));
});


// ── 4. Vuoti distinti ──────────────────────────────────────────────

test("uplink non riconosciuto e router muto non sono la stessa frase", () => {
  const { ctx } = contesto();

  const senzaUplink = snap({ wan_interface: null });
  const a = pagina(ctx, senzaUplink);
  assert.ok(a.includes("non e' stato riconosciuto"), "il router risponde: il problema e' un altro");
  assert.ok(a.includes("tethering"), "e si elencano i nomi cercati, presi dal meta");
  assert.ok(a.includes("192.0.2.1"), "piu' le interfacce su con un indirizzo");

  const mutoAncora = snap({ wan_interface: null, interfaces: [], sources: {} });
  assert.match(pagina(ctx, mutoAncora), /In attesa del primo giro/);

  const muto = snap({ wan_interface: null, interfaces: [],
                      sources: { interfaces: { ok: false, error: "ssh: timeout" } } });
  assert.match(pagina(ctx, muto), /non risponde/);
});


// ── 5. I controlli esistono e fanno quello che dicono ──────────────

test("il ping parte dal router e racconta l'esito", async () => {
  const { ctx, dammi, chiamate } = contesto({
    "/api/wan/ping": { host: "8.8.8.8", online: true, latency_ms: 41.37 } });
  pagina(ctx, snap());
  await dammi("#wan-ping").onclick();
  assert.ok(chiamate.some(u => u.includes("/api/wan/ping")), "l'endpoint c'era e non lo usava nessuno");
  assert.ok(dammi("#wan-ping-esito").textContent.includes("41 ms"));
});

test("un host che non risponde non diventa una latenza di zero", async () => {
  const { ctx, dammi } = contesto({
    "/api/wan/ping": { host: "198.51.100.7", online: false, latency_ms: null } });
  pagina(ctx, snap());
  dammi("#wan-ping-host").value = "198.51.100.7";
  await dammi("#wan-ping").onclick();
  const testo = dammi("#wan-ping-esito").textContent;
  assert.ok(testo.includes("non risponde") && !testo.includes("0 ms"), testo);
});

test("le regole del firewall si caricano su richiesta", async () => {
  const { ctx, dammi, chiamate } = contesto({
    "/api/wan/firewall": { rules: "table inet fw4 {\n  chain input {\n  }\n}" } });
  pagina(ctx, snap());
  assert.equal(chiamate.filter(u => u.includes("firewall")).length, 0,
    "aprire la pagina non deve costare una connessione SSH al router");
  await dammi("#wan-fw").onclick();
  assert.ok(dammi("#wan-fw-box").textContent.includes("table inet fw4"));
  assert.equal(dammi("#wan-fw-box").hidden, false);
});


// ── 6. Aggiornamento parziale ──────────────────────────────────────

test("la pagina si aggiorna per parti: il campo del ping non si azzera", () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  ctx.__interni.state.snap = snap();
  ctx.pageWan(view, ctx.__interni.state.snap);
  assert.equal(typeof ctx.__interni.PAGES.wan.refresh, "function");
  dammi("#wan-ping-host").value = "1.1.1.1";
  const nuovo = snap();
  ctx.__interni.state.snap = nuovo;
  assert.equal(ctx.refreshWan(view, nuovo), true);
  assert.equal(dammi("#wan-ping-host").value, "1.1.1.1",
    "un render pieno ogni dieci secondi cancellerebbe quello che si sta scrivendo");
});

test("l'avviso di sorgente segue i dati anche qui", () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  ctx.__interni.state.snap = snap();
  ctx.pageWan(view, ctx.__interni.state.snap);
  const rotto = snap({ sources: { interfaces: { ok: false, error: "ssh: connessione rifiutata" } } });
  ctx.__interni.state.snap = rotto;
  ctx.refreshWan(view, rotto);
  assert.ok(dammi(".src-nota").innerHTML.includes("connessione rifiutata"));
});
