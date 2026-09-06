/* ===================================================================
   host.test.mjs — Pagina "Host di LANMng"

   Guardata coi dati veri di homeserver: 13 interfacce, di cui **11
   virtuali** (i bridge e le veth dei container Docker, senza indirizzo
   e con nomi come `veth0e1ea91`). Le due interfacce vere si perdevano
   in mezzo.

   Quattro difetti:
     1. tutte le interfacce in tabella, virtuali comprese: l'elenco era
        fatto per l'85% di righe che non dicono niente;
     2. gli errori di rete (9 su enp1s0f0, accumulati su 30 GB) erano
        colorati di rosso come un guasto in corso;
     3. due rotte di default (eth metrica 100, wifi metrica 600) erano
        entrambe in grassetto, senza dire quale e' quella in uso;
     4. la pagina non diceva di quale macchina parlasse ne' di quando
        fosse il dato — e non si aggiorna da sola.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, hostState };";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false, checked: false,
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

function contesto(dati) {
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
               text: async () => JSON.stringify(dati) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate };
}

/* La rete vera di homeserver, ridotta all'essenziale. */
function retiVere() {
  const veth = (n) => ({ name: `veth${n}`, state: "up", virtual: true, wireless: false,
    addresses: [], subnets: [], mtu: 1500, speed_mbps: 10000, mac: `aa:bb:cc:00:00:0${n}`,
    stats: { rx_bytes: 1e7, tx_bytes: 1e7, rx_packets: 50000, tx_packets: 50000,
             rx_errors: 0, tx_errors: 0, rx_dropped: 0, tx_dropped: 0 } });
  return {
    source: "ip", hostname: "homeserver", ts: 1788464019,
    interfaces: [
      { name: "enp1s0f0", state: "up", virtual: false, wireless: false, mtu: 1500,
        speed_mbps: 100, mac: "aa:bb:cc:dd:ee:01",
        addresses: [{ family: "inet", ip: "198.51.100.10", prefix: 24 }], subnets: ["PC2"],
        stats: { rx_bytes: 30172767806, tx_bytes: 5e9, rx_packets: 40000000, tx_packets: 30000000,
                 rx_errors: 0, tx_errors: 9, rx_dropped: 0, tx_dropped: 0 } },
      { name: "wlp2s0", state: "up", virtual: false, wireless: true, mtu: 1500,
        speed_mbps: null, mac: "aa:bb:cc:dd:ee:02",
        addresses: [{ family: "inet", ip: "198.51.100.11", prefix: 24 }], subnets: ["PC2"],
        stats: { rx_bytes: 22250403048, tx_bytes: 4e9, rx_packets: 20000000, tx_packets: 10000000,
                 rx_errors: 0, tx_errors: 0, rx_dropped: 0, tx_dropped: 0 } },
      ...[1, 2, 3, 4, 5].map(veth),
      { name: "docker0", state: "down", virtual: true, wireless: false, mtu: 1500,
        speed_mbps: null, mac: "aa:bb:cc:dd:ee:03",
        addresses: [{ family: "inet", ip: "172.17.0.1", prefix: 16 }], subnets: [],
        stats: { rx_bytes: 12717639, tx_bytes: 0, rx_packets: 1000, tx_packets: 0,
                 rx_errors: 0, tx_errors: 0, rx_dropped: 0, tx_dropped: 0 } },
    ],
    routes: [
      { dest: "default", gateway: "198.51.100.1", dev: "enp1s0f0", metric: 100, default: true },
      { dest: "default", gateway: "198.51.100.1", dev: "wlp2s0", metric: 600, default: true },
      { dest: "198.51.100.0/24", gateway: "", dev: "enp1s0f0", metric: null, default: false },
    ],
  };
}

async function pagina(ctx, dati = retiVere()) {
  const view = elemento();
  ctx.__interni.state.snap = { alerts: [], alerts_summary: {} };
  await ctx.pageHost(view);
  return { view, html: view.innerHTML };
}


// ── 1. Le virtuali non devono seppellire le vere ───────────────────

test("di serie si vedono solo le interfacce vere, e si dice quante sono nascoste", async () => {
  const { ctx } = contesto(retiVere());
  const { html } = await pagina(ctx);
  assert.ok(html.includes("enp1s0f0") && html.includes("wlp2s0"));
  assert.ok(!html.includes("veth1"), "le veth dei container non dicono niente");
  assert.ok(!html.includes("docker0"));
  assert.match(html, /6 interfacce virtuali/);
});

test("la spunta le fa comparire, senza ricaricare nulla", async () => {
  const { ctx, dammi, chiamate } = contesto(retiVere());
  const { view } = await pagina(ctx);
  const prima = chiamate.length;
  dammi("#hs-virt").onchange({ target: { checked: true } });
  assert.ok(view.innerHTML.includes("veth1"));
  assert.equal(chiamate.length, prima, "sono dati che abbiamo gia'");
});


// ── 2. Errori: contatori storici, non allarmi ──────────────────────

test("nove errori su settanta milioni di pacchetti non sono rossi", async () => {
  const { ctx } = contesto(retiVere());
  const { html } = await pagina(ctx);
  const riga = html.split("<tr").find(r => r.includes("enp1s0f0"));
  assert.ok(riga.includes("0 / 9"), "il numero si vede comunque");
  assert.ok(!/class="mono err"/.test(riga), "ma non e' un guasto in corso");
  assert.match(riga, /errori su [\d.]+ pacchetti/, "e il contesto sta nel titolo");
});

test("un tasso di errori vero resta rosso", async () => {
  const dati = retiVere();
  Object.assign(dati.interfaces[0].stats,
    { rx_errors: 5000, tx_errors: 5000, rx_packets: 100000, tx_packets: 100000 });
  const { ctx } = contesto(dati);
  const { html } = await pagina(ctx);
  const riga = html.split("<tr").find(r => r.includes("enp1s0f0"));
  assert.ok(/class="mono err"/.test(riga), riga);
});


// ── 3. Quale default vince ─────────────────────────────────────────

test("fra due rotte di default si dice quale e' in uso", async () => {
  const { ctx } = contesto(retiVere());
  const { html } = await pagina(ctx);
  const righe = html.split("<tr").filter(r => r.includes("default"));
  const eth = righe.find(r => r.includes("enp1s0f0"));
  const wifi = righe.find(r => r.includes("wlp2s0"));
  assert.ok(eth.includes("in uso"), "metrica 100: e' quella che si usa");
  assert.ok(wifi.includes("di riserva"), "metrica 600: entra solo se cade l'altra");
});


// ── 4. Di chi e', e di quando ──────────────────────────────────────

test("la pagina dice di quale macchina parla e con cosa ha letto", async () => {
  const { ctx } = contesto(retiVere());
  const { html } = await pagina(ctx);
  assert.ok(html.includes("homeserver"), "non e' il router: dirlo evita di confondere le pagine");
  assert.ok(html.includes("iproute2"));
  assert.match(html, /alle \d\d:\d\d:\d\d/, "la pagina non si aggiorna da sola");
});

test("il pulsante rilegge, e mentre legge non se ne accettano altri", async () => {
  const { ctx, dammi, chiamate } = contesto(retiVere());
  const { view } = await pagina(ctx);
  const prima = chiamate.length;
  const uno = ctx.caricaHost(view);
  const due = ctx.caricaHost(view);          // secondo clic mentre la prima gira
  await Promise.all([uno, due]);
  assert.equal(chiamate.length, prima + 1, "una lettura sola");
});

test("se la lettura fallisce lo dice, col riprova", async () => {
  const { ctx, dammi } = contesto(retiVere());
  const view = elemento();
  ctx.__interni.state.snap = { alerts: [], alerts_summary: {} };
  ctx.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await ctx.pageHost(view);
  assert.ok(view.innerHTML.includes("Riprova"), view.innerHTML);
});
