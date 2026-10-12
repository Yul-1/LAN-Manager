/* ===================================================================
   snmp.test.mjs — SNMP nella UI (Fase 23)

   Quattro comportamenti, ciascuno con il suo modo di sbagliare:
     1. mappa: un device con un collegamento di topologia si aggancia allo
        switch, ma se lo switch non e' disegnato (nascosto, filtrato) deve
        restare sull'hub, non appeso a un nodo che non esiste;
     2. editor SNMP: gli host col profilo default si salvano come IP nudi,
        i campi v3 non finiscono su un profilo v2c, e il resto della sezione
        (community mascherata compresa) torna indietro intatto;
     3. modale servizi: il metodo snmp manda host, OID, condizione e valore;
     4. il grafico storico di un'interfaccia chiede i dati UNA volta per
        apertura, non a ogni update del WebSocket.
   =================================================================== */
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES, map, devFilter };";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false, checked: false, options: { length: 0 }, children: [],
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {}, addEventListener() {}, removeEventListener() {},
    appendChild(f) { el.children.push(f); return f; },
    append() {}, remove() {}, focus() {}, closest: () => null,
    getContext: () => new Proxy({}, { get: (_t, k) =>
      (k === "createLinearGradient" ? () => ({ addColorStop() {} })
        : k === "measureText" ? () => ({ width: 10 }) : () => {}), set: () => true }),
    clientWidth: 600, clientHeight: 200, width: 0, height: 0,
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
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => ({ matches: false }) },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    confirm: () => true,
    fetch: async (url, init) => {
      const metodo = (init && init.method) || "GET";
      chiamate.push({ url, metodo, body: init && init.body ? JSON.parse(init.body) : null });
      const chiave = Object.keys(risposte).find(k => url.includes(k));
      const corpo = chiave ? risposte[chiave] : {};
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(corpo) };
    },
    AbortController, URLSearchParams, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate };
}

const attendi = () => new Promise(r => setTimeout(r, 20));

/* Indirizzi documentativi, mai quelli veri. */
const dev = (key, o = {}) => Object.assign({ key, ips: [key], name: key, type: "desktop",
  subnet: "lan", online: true, mac: "" }, o);


// ── 1. Mappa ───────────────────────────────────────────────────────

test("con la topologia il device si aggancia allo switch, senza switch resta sull'hub", () => {
  const { ctx } = contesto();
  const { state, map } = ctx.__interni;
  map.W = 800; map.H = 520;
  state.snap = { topology: [{ device: "192.0.2.10", via: "192.0.2.2", port: "port3", source: "fdb" }] };

  ctx.syncMapNodes([dev("192.0.2.2", { type: "switch" }), dev("192.0.2.10")]);
  assert.equal(map.nodes.get("192.0.2.10").parent, "192.0.2.2");
  assert.equal(map.nodes.get("192.0.2.10").port, "port3");
  assert.ok(map.links.some(l => l.s.id === "192.0.2.2" && l.t.id === "192.0.2.10"));

  // Switch nascosto: non e' fra i nodi, il device torna all'hub della subnet.
  ctx.syncMapNodes([dev("192.0.2.10")]);
  assert.equal(map.nodes.get("192.0.2.10").parent, "hub:lan");
  assert.equal(map.nodes.get("192.0.2.10").port, undefined,
    "la porta di un collegamento che non si disegna non deve restare nel tooltip");
});


// ── 2. Editor SNMP ─────────────────────────────────────────────────

test("l'editor SNMP salva host e profili nella forma giusta e non perde il resto", async () => {
  const sezione = { enabled: false, community: "********", timeout: 3,
    profiles: [{ name: "sw", version: "3", user: "lanmng", level: "authPriv",
                 auth_protocol: "SHA", priv_protocol: "AES" }],
    hosts: ["192.0.2.2", { ip: "192.0.2.3", profile: "sw" }] };
  const { ctx, dammi, chiamate } = contesto({
    "/api/config/section/discovery_snmp": { value: sezione },
    "/api/config/section/": { value: [] },
    "/api/config/secrets": { secrets: [] }, "/api/config/backups": { backups: [] },
    "/api/alerts/rules": { rules: [] },
    "/api/config/": { yaml: "x: 1\n", path: "/app/config/config.yaml" } });
  ctx.__interni.state.snap = { alerts: [], alerts_summary: {} };
  ctx.__interni.state.route = "settings";
  await ctx.pageSettings(elemento());
  await attendi();

  const box = dammi("#ed-snmp");
  box.querySelector("#snmp-en").checked = true;
  box.querySelector("#snmp-auto").checked = true;
  box.querySelector("#snmp-int").value = "45";
  box.querySelector("#snmp-excl").value = "192.0.2.9, 192.0.2.8";
  box.querySelector("#snmp-addp").onclick();          // profilo nuovo...
  box.querySelector("#snmp-save").onclick();
  await attendi();

  const put = chiamate.find(c => c.metodo === "PUT" && c.url.includes("discovery_snmp"));
  assert.ok(put, "il salvataggio deve chiamare la sezione discovery_snmp");
  const v = put.body.value;
  assert.equal(v.enabled, true);
  assert.equal(v.traffic_interval, 45);
  assert.deepEqual(v.exclude, ["192.0.2.9", "192.0.2.8"]);
  assert.deepEqual(v.hosts, ["192.0.2.2", { ip: "192.0.2.3", profile: "sw" }]);
  assert.equal(v.profiles.length, 1, "...senza nome non si salva");
  assert.equal(v.community, "********", "la maschera torna indietro: il backend ripristina il segreto");
  assert.equal(v.timeout, 3, "i campi senza controllo nella UI non si perdono");
});


// ── 3. Servizio snmp ───────────────────────────────────────────────

test("il metodo snmp della modale manda host, OID, condizione e valore", async () => {
  const { ctx, chiamate } = contesto({ "/api/services/config": { status: "added" } });
  const campi = new Map();
  const campo = (sel) => { if (!campi.has(sel)) campi.set(sel, elemento()); return campi.get(sel); };
  const ov = elemento({ querySelector: campo });
  ctx.document.createElement = () => ov;
  ctx.__interni.state.snap = {};
  ctx.openServiceModal();
  campo("#sv-method").value = "snmp";
  campo("#sv-name").value = "stampante";
  campo("#sv-host").value = "192.0.2.50";
  campo("#sv-oid").value = "1.3.6.1.2.1.25.3.5.1.1.1";
  campo("#sv-op").value = "eq";
  campo("#sv-value").value = "3";
  campo("#sv-dash").checked = true;
  await campo("#sv-save").onclick();
  const post = chiamate.find(c => c.metodo === "POST" && c.url.includes("/api/services/config"));
  assert.deepEqual(post.body, { kind: "http", type: "snmp", dashboard: true, name: "stampante",
    host: "192.0.2.50", oid: "1.3.6.1.2.1.25.3.5.1.1.1", op: "eq", value: "3" });
});


// ── 4. Grafico per interfaccia ─────────────────────────────────────

test("il grafico di un'interfaccia chiede lo storico una volta, non a ogni update", async () => {
  const { ctx, chiamate } = contesto({
    "/api/history/interface": { points: [{ t: 1, rx_bps: 800, tx_bps: null }] } });
  const { state, devFilter } = ctx.__interni;
  const snap = {
    devices: [dev("192.0.2.2", { snmp: { name: "switch" } })],
    snmp: { enabled: true, hosts: [{ ip: "192.0.2.2", reachable: true, profile: "default",
      system: { name: "switch" }, polled_at: 1,
      interfaces: [{ index: 1, name: "port1", oper: "up", rx_bps: null, tx_bps: 800 }] }] },
  };
  state.snap = snap;
  devFilter.detailKey = "192.0.2.2";
  await ctx.openIfaceChart("192.0.2.2", "port1");
  const storico = () => chiamate.filter(c => c.url.includes("/api/history/interface"));
  assert.equal(storico().length, 1);
  assert.match(storico()[0].url, /host=192\.0\.2\.2/);
  assert.match(storico()[0].url, /ifname=port1/);

  // Dieci update dello snapshot con la scheda aperta: nessuna richiesta in piu'.
  for (let i = 0; i < 10; i++) { state.snap = { ...snap, _ts: i }; ctx.renderDeviceList(); }
  assert.equal(storico().length, 1);
});
