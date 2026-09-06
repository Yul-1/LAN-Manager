/* ===================================================================
   temi.test.mjs — I sei temi

   Il difetto che questi test difendono non e' il CSS: e' che **canvas e
   SVG non leggono le variabili CSS**. Griglia, etichette, riquadro di
   lettura dei grafici e nodi della mappa avevano i colori del tema dark
   scritti a mano dentro app.js: su un tema chiaro sarebbero rimasti
   grafici scuri dentro una pagina bianca, e nessun CSS avrebbe potuto
   rimediare.

   Quindi si guarda una cosa sola, da tre lati:
     1. cosa e' stato scritto su `fillStyle`/`strokeStyle` durante un
        disegno (il finto contesto 2D registra invece di dipingere);
     2. cosa e' finito negli attributi dei nodi SVG della mappa;
     3. che ogni tema dichiari DAVVERO tutti i token che il JavaScript va
        a cercare — un tema lasciato a meta' si vede solo aprendolo, e
        non e' detto che qualcuno lo apra.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
const INDEX = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const AVVIO = readFileSync(new URL("../tema.js", import.meta.url), "utf8");
const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, map, TEMI, TOKEN_TEMA, PALETTE_DARK };";

/* Un tema finto: ogni token ha un colore riconoscibile a occhio nudo nelle
   asserzioni, cosi' un valore rimasto scritto a mano si distingue subito da
   uno letto davvero dal tema. */
const FINTO = {
  "--bg": "#010101", "--bg-2": "#020202", "--panel": "#030303",
  "--panel-2": "#040404", "--border-2": "#050505",
  "--text": "#111111", "--muted": "#222222", "--faint": "#333333",
  "--teal": "#aa1111", "--green": "#aa2222", "--orange": "#aa3333",
  "--red": "#aa4444", "--purple": "#aa5555", "--blue": "#aa6666",
  "--grid": "#bb1111", "--off": "#bb2222", "--tip": "#bb3333ee",
};

/* Contesto 2D che registra TUTTI i colori usati, non solo l'ultimo: qui la
   domanda e' "quali colori sono stati usati nel disegno", e con un solo
   valore l'ultimo cancellerebbe i precedenti. */
function contesto2D(reg) {
  return {
    scale() {}, clearRect() {}, beginPath() {}, closePath() {}, moveTo() {},
    lineTo() {}, stroke() {}, fill() {}, rect() {}, roundRect() {}, fillRect() {},
    save() {}, restore() {}, arc() {}, fillText() {},
    measureText: (t) => ({ width: String(t).length * 6 }),
    createLinearGradient: () => ({ addColorStop(_p, c) { reg.gradienti.push(c); } }),
    set fillStyle(v) { if (typeof v === "string") reg.fill.push(v); },
    get fillStyle() { return ""; },
    set strokeStyle(v) { reg.stroke.push(v); },
    get strokeStyle() { return ""; },
    lineWidth: 1, font: "", textAlign: "", textBaseline: "",
  };
}

function nodo(extra = {}) {
  const attr = {};
  const el = {
    attr, className: "", innerHTML: "", textContent: "", dataset: {}, style: {},
    figli: [], listener: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute(k, v) { attr[k] = String(v); },
    getAttribute(k) { return k in attr ? attr[k] : null; },
    removeAttribute(k) { delete attr[k]; },
    appendChild(f) { el.figli.push(f); return f; },
    append(...f) { el.figli.push(...f); },
    insertBefore(f) { el.figli.push(f); return f; },
    remove() {}, focus() {},
    querySelector: () => nodo(), querySelectorAll: () => [],
    addEventListener(t, fn) { (el.listener[t] = el.listener[t] || []).push(fn); },
    removeEventListener() {},
    getBoundingClientRect: () => ({ width: 600, height: 520, top: 0, left: 0 }),
  };
  return Object.assign(el, extra);
}

function canvasFinto() {
  const reg = { fill: [], stroke: [], gradienti: [] };
  const el = nodo({
    reg, width: 0, height: 0, clientWidth: 600, clientHeight: 180,
    getContext: () => contesto2D(reg),
  });
  el.parentNode = nodo();
  return el;
}

/* `tema`: cosa dice getComputedStyle. `null` = non esiste proprio, cioe' il
   caso di chi non gira in un browser (i test) e deve ricadere sul dark. */
function contesto({ tema = FINTO, salvato = null, sistemaChiaro = false } = {}) {
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, nodo()); return nodi.get(sel); };
  const radice = nodo();
  const memoria = { dati: salvato, letture: 0, scritture: [] };

  const globali = {
    window: {
      addEventListener() {}, devicePixelRatio: 1,
      matchMedia: (q) => ({ matches: sistemaChiaro && /light/.test(q) }),
      localStorage: {
        getItem(k) { memoria.letture += 1; return k === "lanmng.tema" ? memoria.dati : null; },
        setItem(k, v) { memoria.scritture.push([k, v]); memoria.dati = v; },
      },
    },
    document: {
      documentElement: radice, body: nodo(),
      createElement: () => nodo(), createElementNS: () => nodo(),
      querySelector: dammi, querySelectorAll: () => [], addEventListener() {},
    },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, headers: { get: () => "application/json" },
                          text: async () => "{}" }),
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  };
  if (tema) {
    globali.getComputedStyle = () => ({ getPropertyValue: (k) => tema[k] || "" });
  }
  const ctx = createContext(globali);
  runInContext(SORGENTE, ctx);
  ctx.__interni.radice = radice;
  ctx.__interni.memoria = memoria;
  ctx.__interni.dammi = dammi;
  return ctx;
}

const SERIE = Array.from({ length: 20 }, (_, i) => ({
  t: Date.UTC(2026, 8, 4, 10, 0, 0) + i * 300e3,
  cpu: 10 + i, mem: 40 + i,
}));
const LINEE = [{ get: p => p.cpu, color: "teal", label: "CPU" },
               { get: p => p.mem, color: "purple", label: "RAM" }];


// ── 1. I grafici ───────────────────────────────────────────────────

test("la griglia e le etichette del grafico prendono i colori dal tema", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });

  assert.ok(cv.reg.stroke.includes(FINTO["--grid"]),
    `la griglia non usa --grid: ${cv.reg.stroke.join(" ")}`);
  assert.ok(cv.reg.fill.includes(FINTO["--faint"]),
    `le etichette degli assi non usano --faint: ${cv.reg.fill.join(" ")}`);
  assert.ok(cv.reg.fill.includes(FINTO["--muted"]),
    `l'unita' di misura non usa --muted: ${cv.reg.fill.join(" ")}`);
  // Il dark non deve piu' comparire: era la tinta scritta a mano nel codice.
  assert.ok(!cv.reg.stroke.includes(ctx.__interni.PALETTE_DARK.grid),
    "la griglia disegna ancora il grigio del tema dark");
});

test("il colore di una linea e' un token del tema, risolto al disegno", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });

  assert.ok(cv.reg.stroke.includes(FINTO["--teal"]), "la linea CPU non e' del tema");
  assert.ok(cv.reg.stroke.includes(FINTO["--purple"]), "la linea RAM non e' del tema");
  // La sfumatura sotto la curva concatena l'alfa: il token deve risolversi in
  // un hex, altrimenti "rgb(...)40" non e' un colore e l'area sparisce.
  assert.ok(cv.reg.gradienti.includes(FINTO["--teal"] + "40"),
    `sfumatura non valida: ${cv.reg.gradienti.join(" ")}`);
  // In `_dati` restano i TOKEN: e' quello che permette a un ridisegno dopo il
  // cambio di tema di ripescare i colori nuovi invece dei vecchi.
  assert.equal(cv._dati.lines[0].color, "teal");
});

test("cambiando tema il ridisegno usa i colori nuovi", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });

  // Il tema cambia: getComputedStyle da ora risponde un'altra cosa.
  const altro = { ...FINTO, "--grid": "#cc9999", "--teal": "#cc8888" };
  ctx.getComputedStyle = () => ({ getPropertyValue: (k) => altro[k] || "" });
  cv.reg.fill = []; cv.reg.stroke = [];
  ctx.ridisegnaGrafico(cv);

  assert.ok(cv.reg.stroke.includes("#cc9999"), "la griglia e' rimasta al tema di prima");
  assert.ok(cv.reg.stroke.includes("#cc8888"), "la linea e' rimasta al tema di prima");
});

test("il riquadro di lettura sotto il cursore e' del tema", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });
  cv._hover = 300;
  cv.reg.fill = []; cv.reg.stroke = [];
  ctx.ridisegnaGrafico(cv);

  assert.ok(cv.reg.fill.includes(FINTO["--tip"]), "il fondo del riquadro non e' del tema");
  assert.ok(cv.reg.fill.includes(FINTO["--text"]), "i valori letti non sono del tema");
  assert.ok(cv.reg.stroke.includes(FINTO["--border-2"]), "la riga verticale non e' del tema");
  assert.ok(cv.reg.stroke.includes(FINTO["--bg"]), "l'anello del pallino non e' del tema");
});


// ── 2. La mappa ────────────────────────────────────────────────────

/* La mappa e' SVG: i colori finiscono negli attributi, non su un contesto 2D. */
function disegnaMappa(ctx, { subnets = [] } = {}) {
  const svg = nodo();
  ctx.__interni.dammi("#lanmap").getBoundingClientRect = () => ({ width: 600, height: 520, top: 0, left: 0 });
  const finto = ctx.__interni.dammi("#lanmap");
  Object.assign(finto, { setAttribute: svg.setAttribute, appendChild: svg.appendChild,
                         figli: svg.figli, attr: svg.attr });
  ctx.__interni.state.snap = { meta: { subnets } };
  // createElementNS restituisce nodi nuovi che registrano i loro attributi.
  const creati = [];
  ctx.document.createElementNS = () => { const n = nodo(); creati.push(n); return n; };
  ctx.startMap([
    { key: "198.51.100.10", name: "homeserver", type: "server", online: true, ips: ["198.51.100.10"] },
    { key: "198.51.100.44", name: "spento", type: "desktop", online: false, ips: ["198.51.100.44"] },
  ]);
  return creati;
}

test("i nodi della mappa prendono i colori dal tema", () => {
  const ctx = contesto();
  const creati = disegnaMappa(ctx);
  const colori = creati.flatMap(n => Object.values(n.attr));

  assert.ok(colori.includes(FINTO["--teal"]), "il router non e' del tema");
  assert.ok(colori.includes(FINTO["--text"]), "i glifi dei nodi non sono del tema");
  assert.ok(colori.includes(FINTO["--panel"]), "il fondo dei nodi non e' del tema");
  assert.ok(colori.includes(FINTO["--off"]), "un nodo spento non usa --off");
  const dark = ctx.__interni.PALETTE_DARK;
  assert.ok(!colori.includes(dark.text) && !colori.includes(dark.teal),
    "la mappa disegna ancora i colori scritti a mano del tema dark");
});

test("i colori delle subnet restano quelli della config, non del tema", () => {
  // Sono dati del proprietario (config.yaml, subnets[].color): un tema non ha
  // titolo per riscriverli.
  const ctx = contesto();
  const creati = disegnaMappa(ctx, { subnets: [{ label: "casa", color: "#00ff00" }] });
  ctx.__interni.map.nodes.forEach(n => { if (!n.router) n.subnet = "casa"; });
  ctx.buildMapDom();
  const colori = [...ctx.__interni.map.nodes.values()]
    .flatMap(n => (n.el ? Object.values(n.el.attr) : []))
    .concat(creati.flatMap(n => Object.values(n.attr)));
  assert.ok(colori.includes("#00ff00"), "il colore della subnet e' stato sostituito dal tema");
});


// ── 3. Fuori dal browser si ricade sul dark ────────────────────────

test("senza getComputedStyle si disegna col tema dark, non senza colori", () => {
  const ctx = contesto({ tema: null });
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });
  const dark = ctx.__interni.PALETTE_DARK;
  assert.ok(cv.reg.stroke.includes(dark.grid), "griglia senza colore");
  assert.ok(cv.reg.stroke.includes(dark.teal), "linea senza colore");
});

test("un token vuoto non diventa una stringa vuota su fillStyle", () => {
  // Un tema incompleto deve degradare sul dark: `fillStyle = ""` viene
  // ignorato dal canvas e il testo resta del colore di prima, cioe' a caso.
  const ctx = contesto({ tema: { "--teal": "#123456" } });
  const cv = canvasFinto();
  ctx.drawLineChart(cv, SERIE, LINEE, { left: "pct", leftTop: 100 });
  assert.ok(!cv.reg.fill.includes(""), "un colore vuoto e' arrivato al canvas");
  assert.ok(!cv.reg.stroke.includes(""), "un colore vuoto e' arrivato al canvas");
});


// ── 4. La scelta e la sua memoria ──────────────────────────────────

test("scegliere un tema lo applica e lo ricorda in questo browser", () => {
  const ctx = contesto();
  ctx.applicaTema("neon");
  assert.equal(ctx.__interni.radice.getAttribute("data-tema"), "neon");
  assert.deepEqual(ctx.__interni.memoria.scritture.at(-1), ["lanmng.tema", "neon"]);
  assert.equal(ctx.temaAttivo(), "neon");
});

test("un tema inesistente vale come dark, non lascia la pagina scolorita", () => {
  const ctx = contesto();
  assert.equal(ctx.applicaTema("fucsia"), "dark");
  assert.equal(ctx.__interni.radice.getAttribute("data-tema"), "dark");
  assert.equal(ctx.temaAttivo(), "dark");
});

test("se il browser non lascia salvare, il tema si applica lo stesso", () => {
  const ctx = contesto();
  ctx.window.localStorage.setItem = () => { throw new Error("storage disabilitato"); };
  ctx.applicaTema("ambra");
  assert.equal(ctx.__interni.radice.getAttribute("data-tema"), "ambra");
});

test("il selettore in Impostazioni offre tutti i temi e segna quello in uso", () => {
  const ctx = contesto();
  const box = ctx.__interni.dammi("#ed-temi");
  const scelte = [];
  box.querySelectorAll = () => scelte;
  ctx.applicaTema("chiaro");
  ctx.editorTemi();
  const html = box.innerHTML;
  ctx.__interni.TEMI.forEach(t =>
    assert.ok(html.includes(`data-tema="${t.id}"`), `manca il tema ${t.id}`));
  assert.ok(/data-tema="chiaro"[^>]*class|class="tema-scelta attivo" data-tema="chiaro"/.test(html)
    || html.includes('class="tema-scelta attivo" data-tema="chiaro"'),
    "il tema in uso non e' segnalato");
});


// ── 5. Ogni tema e' completo ───────────────────────────────────────

/* I token che il JavaScript va a cercare: se un tema ne dimentica uno, quel
   pezzo di grafico resta del colore del dark dentro una pagina di un altro
   tema, e lo si scopre solo guardandolo. */
const TOKEN_COLORE = ["--bg", "--bg-2", "--panel", "--panel-2", "--border", "--border-2",
  "--text", "--muted", "--faint", "--teal", "--green", "--orange", "--red",
  "--purple", "--blue", "--code-bg", "--grid", "--off"];
const TOKEN_ALFA = ["--velo", "--tip"];

function blocchiTema() {
  const blocchi = {};
  // L'attributo dev'essere subito seguito dalla graffa: `[data-tema="neon"] .card`
  // e' una regola in piu' per quel tema, non la sua tavolozza.
  const re = /(?:^|\n)(:root, )?\[data-tema="([a-z]+)"\]\s*\{([^}]*)\}/g;
  let m;
  while ((m = re.exec(CSS))) blocchi[m[2]] = m[3];
  return blocchi;
}

test("tutti e sei i temi dichiarano tutti i token, in hex", () => {
  const blocchi = blocchiTema();
  const ctx = contesto();
  const attesi = ctx.__interni.TEMI.map(t => t.id);
  assert.deepEqual(Object.keys(blocchi).sort(), [...attesi].sort(),
    "i temi di app.js e i blocchi di styles.css non coincidono");

  for (const [tema, corpo] of Object.entries(blocchi)) {
    for (const tok of TOKEN_COLORE) {
      const m = new RegExp(`\\${tok}:\\s*(\\S+);`).exec(corpo);
      assert.ok(m, `il tema ${tema} non dichiara ${tok}`);
      assert.match(m[1], /^#[0-9a-f]{6}$/,
        `${tema} ${tok} = ${m[1]}: serve un hex a 6 cifre (il JS ci concatena l'alfa)`);
    }
    for (const tok of TOKEN_ALFA) {
      const m = new RegExp(`\\${tok}:\\s*(\\S+);`).exec(corpo);
      assert.ok(m, `il tema ${tema} non dichiara ${tok}`);
      assert.match(m[1], /^#[0-9a-f]{8}$/, `${tema} ${tok} deve essere semitrasparente`);
    }
    assert.match(corpo, /--font-base:\s*[\d.]+px;/, `il tema ${tema} non dichiara --font-base`);
  }
});

test("i token cercati dal JavaScript esistono tutti nel CSS", () => {
  const ctx = contesto();
  const dichiarati = new Set([...TOKEN_COLORE, ...TOKEN_ALFA]);
  Object.values(ctx.__interni.TOKEN_TEMA).forEach(tok =>
    assert.ok(dichiarati.has(tok), `${tok} e' letto da app.js ma nessun tema lo dichiara`));
});


// ── 6. La prima apertura ───────────────────────────────────────────

/* tema.js sceglie il tema PRIMA che il CSS dipinga: sono quattro righe, ma
   sono le quattro righe che decidono cosa si vede al primo caricamento. Si
   esegue davvero, non si legge. */
function avvio(ctx) {
  runInContext(AVVIO, ctx);
  return ctx.__interni.radice.getAttribute("data-tema");
}

test("tema.js e' caricato da index.html, e prima del foglio di stile", () => {
  // Inline non si puo': la CSP del vhost (default-src 'self') rifiuta gli
  // script nella pagina senza dire niente, e il tema salvato non tornerebbe
  // piu' su. Dopo il CSS nemmeno: si vedrebbe il lampo del tema sbagliato.
  assert.ok(!/<script>[^<]/.test(INDEX), "c'e' uno script inline: la CSP lo blocca");
  const iTema = INDEX.indexOf("tema.js?v=");
  const iCss = INDEX.indexOf("styles.css?v=");
  assert.ok(iTema > 0, "index.html non carica tema.js");
  assert.ok(iTema < iCss, "tema.js va caricato prima del foglio di stile");
});

test("senza una scelta salvata si segue il tema del sistema", () => {
  assert.equal(avvio(contesto({ salvato: null, sistemaChiaro: true })), "chiaro");
  assert.equal(avvio(contesto({ salvato: null, sistemaChiaro: false })), "dark");
});

test("con una scelta salvata vince quella, non il sistema", () => {
  assert.equal(avvio(contesto({ salvato: "ambra", sistemaChiaro: true })), "ambra");
});

test("se il localStorage e' inaccessibile la pagina parte lo stesso", () => {
  const ctx = contesto();
  ctx.window.localStorage.getItem = () => { throw new Error("storage bloccato"); };
  assert.equal(avvio(ctx), "dark");
});
