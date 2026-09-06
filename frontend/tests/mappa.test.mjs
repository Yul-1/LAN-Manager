/* ===================================================================
   mappa.test.mjs — Mappa della topologia

   Un difetto solo, scelto dal proprietario dopo che il rifacimento
   della mappa era stato annullato (vedi CHANGELOG, "La mappa torna
   com'era"): **le etichette in basso venivano tagliate dal bordo**.

   Il nome sta 27px sotto il centro del nodo (31 per gli hub), ma il
   vincolo di posizione ne lasciava 24: misurato sull'istanza vera,
   "homeserver" usciva di 6px dal riquadro e si leggeva a meta'.

   Qui si prova solo questo. Come la mappa si muove — che continui a
   sistemarsi da sola, senza raffreddamenti — resta com'era.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, map, ARIA_ETICHETTA };";

function elemento(rect = { width: 1014, height: 520 }) {
  const attr = new Map();
  const ascoltatori = [];
  return {
    attr, ascoltatori,
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    setAttribute(k, v) { attr.set(k, String(v)); },
    getAttribute(k) { return attr.has(k) ? attr.get(k) : null; },
    appendChild(f) { return f; }, append() {},
    querySelector: () => elemento(), querySelectorAll: () => [],
    addEventListener(t, fn) { ascoltatori.push([t, fn]); },
    removeEventListener() {}, remove() {}, focus() {},
    closest: () => null, getContext: () => null,
    getBoundingClientRect: () => ({ ...rect, top: 0, left: 0 }),
  };
}

function contesto() {
  const nodi = new Map();
  const dammi = (sel) => {
    if (!nodi.has(sel)) nodi.set(sel, elemento());
    return nodi.get(sel);
  };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(),
                createElementNS: () => elemento(), querySelector: dammi,
                querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, headers: { get: () => "application/json" },
                          text: async () => "{}" }),
    AbortController,
    // La simulazione si richiama da sola con requestAnimationFrame: qui si
    // ferma il ciclo e si fanno i giri a mano, uno per volta.
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi };
}

const SUBNET = ["LAN", "PC2", "VM", "Guest", "ZTE", "WireGuard"];
const dispositivi = (n = 27) => Array.from({ length: n }, (_, i) => ({
  key: `10.0.${i}.2`, ips: [`10.0.${i}.2`], name: `apparato-${SUBNET[i % 6]}-${i}`,
  type: "server", subnet: SUBNET[i % 6], online: i % 3 === 0, hidden: false,
}));


test("nessun nome esce dal bordo basso del riquadro", () => {
  const { ctx } = contesto();
  ctx.startMap(dispositivi());
  const m = ctx.__interni.map;
  // Duecento giri: la disposizione non si ferma da sola (e' voluto), quindi si
  // guarda dopo che i nodi si sono sparpagliati.
  for (let i = 0; i < 200; i++) ctx.tickMap();
  const sporgenti = [...m.nodes.values()]
    .filter(n => !n.fixed && n.y + 31 > m.H);      // 31 = l'etichetta piu' bassa (hub)
  assert.deepEqual(sporgenti.map(n => n.id), [],
    "il nome sta sotto il nodo: senza aria il bordo lo taglia a meta'");
});

test("l'aria lasciata in basso basta per l'etichetta piu' bassa", () => {
  const { ctx } = contesto();
  assert.ok(ctx.__interni.ARIA_ETICHETTA >= 35,
    "gli hub scrivono il nome a 31px dal centro, piu' l'altezza dei caratteri");
});

test("in alto e ai lati il vincolo resta quello di prima", () => {
  // Il difetto era solo in basso: stringere anche gli altri bordi cambierebbe
  // la disposizione, che al proprietario andava bene com'era.
  const { ctx } = contesto();
  ctx.startMap(dispositivi());
  const m = ctx.__interni.map;
  for (let i = 0; i < 200; i++) ctx.tickMap();
  const nodi = [...m.nodes.values()].filter(n => !n.fixed);
  assert.ok(nodi.every(n => n.y >= 24 && n.x >= 24 && n.x <= m.W - 24));
  assert.ok(nodi.some(n => n.y < 24 + ctx.__interni.ARIA_ETICHETTA),
    "in alto i nodi possono ancora salire come prima");
});


/* ── Zoom e spostamento (2026-09-04) ───────────────────────────────
   Chiesto dal proprietario insieme allo zoom dei grafici. Si agisce sul
   `viewBox`, non sulle coordinate dei nodi: la simulazione continua a
   lavorare nello stesso mondo, quindi ingrandire non sposta niente e
   tornando alla vista intera si ritrova la mappa di prima.

   La trappola vera e' la conversione schermo -> mondo: se ignora il
   viewBox, un nodo trascinato mentre si e' zoomati salta altrove. */

test("la vista di partenza e' il mondo intero", () => {
  const { ctx, dammi } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  assert.deepEqual([m.vista.x, m.vista.y, m.vista.w, m.vista.h], [0, 0, m.W, m.H]);
  assert.equal(dammi("#lanmap").getAttribute("viewBox"), `0 0 ${m.W} ${m.H}`);
});

test("zoomando la finestra si stringe e il viewBox la segue", () => {
  const { ctx, dammi } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  ctx.zoomMappa(1 / 2, null);
  assert.ok(m.vista.w < m.W, "la finestra doveva stringersi");
  assert.equal(dammi("#lanmap").getAttribute("viewBox"),
    `${m.vista.x} ${m.vista.y} ${m.vista.w} ${m.vista.h}`);
});

test("il punto sotto il cursore resta fermo mentre si zooma", () => {
  // Con il centro fisso si perde di vista il nodo che si stava guardando.
  const { ctx } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  const perno = { x: 200, y: 150 };
  ctx.zoomMappa(1 / 2, perno);
  // Il perno deve trovarsi alla stessa frazione della finestra di prima
  // (200/W in orizzontale), cioe' restare sotto lo stesso pixel.
  const fx = (perno.x - m.vista.x) / m.vista.w;
  assert.ok(Math.abs(fx - 200 / m.W) < 0.02, `frazione ${fx}, attesa ${200 / m.W}`);
});

test("lo zoom ha un minimo e un massimo", () => {
  const { ctx } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  for (let i = 0; i < 40; i++) ctx.zoomMappa(1 / 1.4, null);
  assert.ok(m.W / m.vista.w <= 6.001, "sopra 6x si vede un nodo solo e ci si perde");
  for (let i = 0; i < 80; i++) ctx.zoomMappa(1.4, null);
  assert.ok(m.W / m.vista.w >= 0.299, "sotto 0,3x i nodi sono puntini senza nome");
});

test("la finestra non esce dal mondo", () => {
  const { ctx } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  ctx.zoomMappa(1 / 3, { x: m.W, y: m.H });     // zoom sull'angolo estremo
  assert.ok(m.vista.x >= 0 && m.vista.y >= 0);
  assert.ok(m.vista.x + m.vista.w <= m.W + 0.001);
  assert.ok(m.vista.y + m.vista.h <= m.H + 0.001);
});

test("il pulsante di reset riporta la vista intera", () => {
  const { ctx } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  ctx.zoomMappa(1 / 3, null);
  ctx.azzeraVistaMappa();
  assert.deepEqual([m.vista.x, m.vista.y, m.vista.w, m.vista.h], [0, 0, m.W, m.H]);
});

test("zoomati, il punto del mondo tiene conto della finestra", () => {
  // E' il difetto che si introduce senza pensarci: la conversione
  // schermo -> mondo passava da map.W/H e ignorava il viewBox, quindi un
  // nodo trascinato mentre si era zoomati saltava da un'altra parte.
  const { ctx } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  const ev = { clientX: 507, clientY: 260 };           // meta' esatta del riquadro
  const intero = ctx.puntoMappa(ev);
  assert.ok(Math.abs(intero.x - m.W / 2) < 1, "alla vista intera e' il centro del mondo");

  ctx.zoomMappa(1 / 2, { x: 100, y: 100 });
  const zoomato = ctx.puntoMappa(ev);
  const atteso = m.vista.x + m.vista.w / 2;
  assert.ok(Math.abs(zoomato.x - atteso) < 1,
    `${zoomato.x} invece di ${atteso}: la conversione ignora il viewBox`);
});

test("la simulazione lavora sempre nel mondo intero, non nella finestra", () => {
  // Se lo zoom entrasse nella simulazione, ingrandire ammasserebbe i nodi e
  // tornando indietro la mappa sarebbe un'altra.
  const { ctx } = contesto();
  ctx.startMap(dispositivi(12));
  const m = ctx.__interni.map;
  for (let i = 0; i < 60; i++) ctx.tickMap();
  const prima = [...m.nodes.values()].map(n => [n.x, n.y]);
  ctx.zoomMappa(1 / 3, null);
  const dopo = [...m.nodes.values()].map(n => [n.x, n.y]);
  assert.deepEqual(dopo, prima, "lo zoom ha spostato i nodi");
});

test("cambiando larghezza della finestra la vista intera si rifa'", () => {
  // Il mondo e' largo quanto il riquadro: senza questo resta il viewBox della
  // misura precedente e la mappa si vede tagliata.
  const { ctx, dammi } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  const primaW = m.W;

  dammi("#lanmap").getBoundingClientRect = () => ({ width: 640, height: 520, top: 0, left: 0 });
  ctx.startMap(dispositivi(6));
  assert.notEqual(m.W, primaW, "il mondo doveva seguire il riquadro");
  assert.equal(dammi("#lanmap").getAttribute("viewBox"), `0 0 ${m.W} ${m.H}`);
});

test("cambiando larghezza una vista zoomata resta dentro i confini nuovi", () => {
  const { ctx, dammi } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  ctx.zoomMappa(1 / 4, { x: m.W, y: m.H });      // zoomata sull'angolo destro
  assert.ok(m.vista.x > 0, "la finestra doveva essere spostata a destra");

  // Il mondo si stringe, ma la finestra resta piu' piccola di lui: deve
  // rientrare, altrimenti si guarda il vuoto oltre il bordo destro.
  dammi("#lanmap").getBoundingClientRect = () => ({ width: 600, height: 520, top: 0, left: 0 });
  ctx.startMap(dispositivi(6));
  assert.ok(m.vista.w < m.W, "presupposto del test: finestra piu' stretta del mondo");
  assert.ok(m.vista.x + m.vista.w <= m.W + 0.001,
    `la finestra sporge di ${m.vista.x + m.vista.w - m.W}px dal mondo`);
});

test("i pulsanti dello zoom bastano da soli, senza tastiera", () => {
  // La rotellina secca non zooma (bloccherebbe lo scorrimento della pagina):
  // chi non vuole usare Ctrl deve avere una strada, ed e' questa.
  const { ctx, dammi } = contesto();
  ctx.startMap(dispositivi(6));
  const m = ctx.__interni.map;
  const bottoni = dammi("#map-zoom");
  const click = (bottoni.ascoltatori || []).find(([t]) => t === "click");
  assert.ok(click, "i pulsanti non ascoltano nessun clic");
  click[1]({ target: { closest: () => ({ dataset: { zoom: "in" } }) } });
  assert.ok(m.vista.w < m.W, "il pulsante + non ha ingrandito");
  click[1]({ target: { closest: () => ({ dataset: { zoom: "reset" } }) } });
  assert.equal(m.vista.w, m.W, "il pulsante di reset non ha riportato la vista intera");
});
