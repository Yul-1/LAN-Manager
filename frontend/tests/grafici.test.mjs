/* ===================================================================
   grafici.test.mjs — Zoom, spostamento e lettura puntuale dei grafici

   Chiesto dal proprietario il 2026-09-04. Prima un grafico era una
   figura muta: si vedeva che c'era un picco, non quanto valesse ne'
   quando fosse successo, e la finestra era sempre e solo tutta la
   serie.

   Le due cose che questi test difendono, perche' sono le due che si
   possono rompere senza accorgersene:

     1. la finestra dello zoom si tiene in TIMESTAMP, non in indici.
        La serie cresce ad ogni ciclo di raccolta: con gli indici
        l'intervallo che si sta guardando scivolerebbe indietro da
        solo mentre lo si guarda;
     2. il pallino sta su un punto VERO della serie. Un valore
        interpolato sotto il cursore sarebbe peggio che nessun valore:
        si leggerebbe come un dato misurato che non e' mai esistito.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { ZOOM_MIN_PUNTI };";

/* Contesto 2D che registra invece di disegnare: i test guardano cosa e'
   stato scritto e dove, non dei pixel. */
function contesto2D(reg) {
  return {
    scale() {}, clearRect() {}, beginPath() {}, closePath() {},
    moveTo() {}, lineTo() {}, stroke() {}, fill() {}, rect() {}, roundRect() {},
    fillRect() {}, save() {}, restore() {},
    arc(x, y, r) { reg.pallini.push({ x, y, r }); },
    fillText(t, x, y) { reg.testi.push({ t: String(t), x, y }); },
    measureText: (t) => ({ width: String(t).length * 6 }),
    createLinearGradient: () => ({ addColorStop() {} }),
    set fillStyle(v) { reg.fill = v; }, get fillStyle() { return reg.fill; },
    set strokeStyle(v) { reg.stroke = v; }, get strokeStyle() { return reg.stroke; },
    lineWidth: 1, font: "", textAlign: "", textBaseline: "",
  };
}

/* Un nodo qualunque, quel tanto che basta a `montaComandiGrafico`. */
function nodoFinto() {
  const classi = new Set();
  const figli = [];
  const nodo = {
    className: "", innerHTML: "", dataset: {}, figli, classi,
    classList: {
      add: (c) => classi.add(c), remove: (c) => classi.delete(c),
      contains: (c) => classi.has(c),
      toggle: (c, on) => (on ? classi.add(c) : classi.delete(c)),
    },
    listener: {},
    addEventListener(t, fn) { (nodo.listener[t] = nodo.listener[t] || []).push(fn); },
    appendChild(f) { figli.push(f); if (f) f.parentNode = nodo; return f; },
    insertBefore(f) { figli.push(f); if (f) f.parentNode = nodo; return f; },
    emit(t, ev = {}) { (nodo.listener[t] || []).forEach(fn => fn(ev)); },
  };
  return nodo;
}

function canvasFinto(larghezza = 600, altezza = 180, chiave = "") {
  const reg = { testi: [], pallini: [] };
  const el = {
    reg, listener: {}, dataset: chiave ? { chartKey: chiave } : {},
    style: {}, width: 0, height: 0,
    clientWidth: larghezza, clientHeight: altezza,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    getContext: () => contesto2D(reg),
    getBoundingClientRect: () => ({ width: larghezza, height: altezza, top: 0, left: 0 }),
    addEventListener(nome, fn) { (el.listener[nome] = el.listener[nome] || []).push(fn); },
    removeEventListener() {},
    // Scatena un evento come farebbe il browser.
    emit(nome, ev = {}) {
      (el.listener[nome] || []).forEach(fn => fn({
        preventDefault() {}, cancelable: true, ...ev,
      }));
    },
  };
  el.parentNode = nodoFinto();
  el.parentNode.figli.push(el);
  return el;
}

function contesto() {
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: {}, createElement: () => nodoFinto(), querySelector: () => null,
                querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, headers: { get: () => "application/json" },
                          text: async () => "{}" }),
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return ctx;
}

/* Una serie come quelle vere: un punto ogni 5 minuti, con un buco in mezzo
   (un dato mancante non e' uno zero e non deve diventarlo nemmeno qui). */
const T0 = Date.UTC(2026, 8, 4, 10, 0, 0);
const PASSO = 5 * 60e3;
function serie(n = 40, da = 0) {
  return Array.from({ length: n }, (_, i) => ({
    t: T0 + (da + i) * PASSO,
    cpu: i === 7 ? null : 10 + (i % 20) * 3,
    mem: 40 + (i % 10),
  }));
}
const LINEE = [
  { get: p => p.cpu, color: "#4dd6e0", label: "CPU" },
  { get: p => p.mem, color: "#9d8bf0", label: "RAM" },
];
const OPZ = { left: "pct", leftTop: 100 };

function disegna(ctx, cv, s = serie()) {
  cv.reg.testi = []; cv.reg.pallini = [];
  ctx.drawLineChart(cv, s, LINEE, OPZ);
  return cv.reg;
}
const testoDi = (reg) => reg.testi.map(x => x.t).join(" | ");


// ── 1. Il punto letto: valore esatto e orario ──────────────────────

test("senza il mouse sopra non si disegna nessun pallino", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  const reg = disegna(ctx, cv);
  assert.equal(reg.pallini.length, 0);
});

test("col mouse sopra compaiono un pallino per linea, l'orario e i valori", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  cv._hover = 300;                      // meta' del canvas
  const reg = disegna(ctx, cv);

  assert.equal(reg.pallini.length, 2, "una CPU e una RAM");
  const testo = testoDi(reg);
  assert.match(testo, /\d{2}\/\d{2} \d{2}:\d{2}:\d{2}/, `manca l'orario: ${testo}`);
  assert.match(testo, /CPU\s+\d+\.\d%/, `manca il valore della CPU: ${testo}`);
  assert.match(testo, /RAM\s+\d+\.\d%/, `manca il valore della RAM: ${testo}`);
});

test("l'orario mostrato e' quello di un punto vero della serie", () => {
  // Un valore interpolato sotto il cursore si leggerebbe come una misura che
  // non e' mai stata presa.
  const ctx = contesto();
  const cv = canvasFinto();
  const s = serie();
  disegna(ctx, cv, s);
  cv._hover = 237;                      // di proposito non su una tacca tonda
  const reg = disegna(ctx, cv, s);

  const orari = new Set(s.map(p => ctx.orarioCompleto(p.t)));
  const mostrato = reg.testi.map(x => x.t).find(t => /\d{2}\/\d{2} \d{2}:\d{2}:\d{2}/.test(t));
  assert.ok(orari.has(mostrato), `${mostrato} non e' l'orario di nessun punto`);
});

test("un buco nei dati si dichiara, non diventa uno zero", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  const s = serie();
  disegna(ctx, cv, s);
  // L'ottavo punto ha cpu null: ci si mette sopra esattamente.
  const i = 7;
  cv._hover = 46 + (600 - 46 - 10) * i / (s.length - 1);
  const reg = disegna(ctx, cv, s);

  assert.match(testoDi(reg), /CPU\s+—/, "il valore mancante va scritto come mancante");
  assert.equal(reg.pallini.length, 1, "sul buco non si disegna un pallino a zero");
});

test("il valore porta l'unita' scelta sul valore, non sul fondo scala", () => {
  const ctx = contesto();
  assert.equal(ctx.fmtValoreAsse("rate", 12_400_000), "12.4 Mbit/s");
  assert.equal(ctx.fmtValoreAsse("ms", 3.27), "3.3 ms");
  assert.equal(ctx.fmtValoreAsse("temp", 47.4), "47 °C");
  assert.equal(ctx.fmtValoreAsse("pct", 3.14), "3.1%");
  assert.equal(ctx.fmtValoreAsse("pct", null), "—");
});


// ── 2. Zoom ────────────────────────────────────────────────────────

test("la rotellina restringe la finestra e lo dichiara in pagina", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  assert.ok(!cv._vista);

  cv.emit("wheel", { deltaY: -100, clientX: 300, clientY: 90, ctrlKey: true });
  assert.ok(cv._vista, "la rotellina non ha zoomato");
  const reg = cv.reg;
  assert.match(testoDi(reg), /zoom · \d+\/40 punti · trascina per spostarti/,
    "una finestra piu' stretta senza spiegazione si legge come un dato mancante");
});

test("lo zoom non scende sotto tre punti", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  for (let i = 0; i < 40; i++) ctx.zoomGrafico(cv, 0.5, 0.75);
  const dentro = serie().filter(p => p.t >= cv._vista.t0 && p.t <= cv._vista.t1);
  assert.ok(dentro.length >= ctx.__interni.ZOOM_MIN_PUNTI,
    `finestra da ${dentro.length} punti: sotto i tre non e' piu' un grafico`);
});

test("allargando oltre i dati si torna alla vista intera", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  ctx.zoomGrafico(cv, 0.5, 0.5);
  assert.ok(cv._vista);
  for (let i = 0; i < 10; i++) ctx.zoomGrafico(cv, 0.5, 1 / 0.75);
  assert.equal(cv._vista, null, "senza questo resta una finestra 'zoomata' larga quanto tutto");
});

test("il doppio clic riporta tutti i punti e toglie la nota", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  ctx.zoomGrafico(cv, 0.5, 0.4);
  cv.reg.testi = [];
  cv.emit("dblclick");
  assert.equal(cv._vista, null);
  assert.doesNotMatch(testoDi(cv.reg), /zoom ·/);
});

test("la finestra e' un intervallo di tempo: i punti nuovi non la spostano", () => {
  // E' la ragione per cui lo stato NON e' un intervallo di indici. Con gli
  // indici, ogni punto nuovo in coda farebbe scorrere indietro da sola la
  // finestra che si sta guardando.
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv, serie(40));
  ctx.zoomGrafico(cv, 0.5, 0.4);
  const prima = { ...cv._vista };

  // Arrivano dieci punti nuovi, come farebbe un ciclo di raccolta.
  disegna(ctx, cv, serie(50));
  // Campo per campo: l'oggetto nasce dentro il contesto vm e ha un prototipo
  // diverso da quelli di qui, quindi un confronto profondo fallirebbe anche a
  // valori identici.
  assert.equal(cv._vista.t0, prima.t0, "la finestra si e' mossa da sola");
  assert.equal(cv._vista.t1, prima.t1, "la finestra si e' mossa da sola");
});

test("spostare la finestra non ne cambia l'ampiezza e non esce dai dati", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  const s = serie();
  disegna(ctx, cv, s);
  ctx.zoomGrafico(cv, 0.5, 0.4);
  const arco = cv._vista.t1 - cv._vista.t0;

  ctx.spostaGrafico(cv, -0.5);
  assert.equal(cv._vista.t1 - cv._vista.t0, arco, "l'ampiezza deve restare quella");

  for (let i = 0; i < 20; i++) ctx.spostaGrafico(cv, -1);
  assert.equal(cv._vista.t0, s[0].t, "spostandosi all'indietro ci si ferma al primo dato");
  for (let i = 0; i < 40; i++) ctx.spostaGrafico(cv, 1);
  assert.equal(cv._vista.t1, s[s.length - 1].t, "e in avanti all'ultimo");
});

test("senza zoom il trascinamento non sposta niente", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  cv.emit("mousedown", { clientX: 100 });
  assert.equal(cv._trascina, undefined, "alla vista intera non c'e' dove spostarsi");
});

test("i listener si agganciano una volta sola per canvas", () => {
  // `drawLineChart` viene richiamata ad ogni ciclo di raccolta: riagganciare
  // ogni volta accumulerebbe un listener per giro finche' la pagina resta
  // aperta (e' il difetto gia' corretto sulla mappa).
  const ctx = contesto();
  const cv = canvasFinto();
  for (let i = 0; i < 12; i++) disegna(ctx, cv);
  assert.equal(cv.listener.wheel.length, 1);
  assert.equal(cv.listener.mousemove.length, 1);
});

test("una serie troppo corta per essere zoomata resta intera", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv, serie(2));
  ctx.zoomGrafico(cv, 0.5, 0.5);
  const reg = disegna(ctx, cv, serie(2));
  assert.doesNotMatch(testoDi(reg), /zoom ·/);
});

test("il perno della rotellina si misura sull'area disegnata, non sul canvas", () => {
  // Gli assi occupano 46px a sinistra e 10 a destra: prendendo la frazione
  // sull'intera larghezza il perno scivola, e si ingrandisce accanto a quello
  // che si stava guardando.
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  assert.equal(ctx.frazioneDisegnata(cv, { x: 46, larghezza: 600 }), 0);
  assert.equal(ctx.frazioneDisegnata(cv, { x: 590, larghezza: 600 }), 1);
  assert.ok(Math.abs(ctx.frazioneDisegnata(cv, { x: 318, larghezza: 600 }) - 0.5) < 0.01);
  // Fuori dall'area non si va oltre gli estremi.
  assert.equal(ctx.frazioneDisegnata(cv, { x: 0, larghezza: 600 }), 0);
});

test("col secondo asse l'area disegnata e' piu' stretta", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  ctx.drawLineChart(cv, serie(), [
    { get: p => p.cpu, color: "#4dd6e0", label: "CPU" },
    { get: p => p.mem, color: "#e6a94d", axis: "right", label: "temp" },
  ], { left: "pct", leftTop: 100, right: "temp", rightTop: 100 });
  assert.equal(ctx.frazioneDisegnata(cv, { x: 554, larghezza: 600 }), 1,
    "con l'asse destro il disegno finisce 46px prima del bordo");
});

test("la rotellina senza Ctrl lascia scorrere la pagina", () => {
  // Nella pagina Risorse i grafici sono sei, impilati: prendendosi la rotellina
  // secca, scorrere la pagina diventerebbe impossibile appena il cursore passa
  // sopra un grafico.
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  let fermato = false;
  cv.emit("wheel", { deltaY: -100, clientX: 300, preventDefault() { fermato = true; } });
  assert.ok(!cv._vista, "ha zoomato senza che glielo si chiedesse");
  assert.equal(fermato, false, "e ha pure bloccato lo scorrimento");
});


// ── 3. Lo zoom non deve morire con il nodo ─────────────────────────
//
// Nella pagina Risorse le schede si riscrivono ad ogni raccolta
// (`refreshResources`) e il canvas viene ricreato: con lo stato appeso al nodo
// lo zoom durava al massimo un intervallo di raccolta, ~60 secondi, e si
// azzerava da solo senza che niente lo spiegasse.

test("lo zoom sopravvive alla ricreazione del canvas, a parita' di chiave", () => {
  const ctx = contesto();
  const primo = canvasFinto(600, 180, "res:192.0.2.30");
  disegna(ctx, primo);
  ctx.zoomGrafico(primo, 0.5, 0.4);
  const atteso = { t0: primo._vista.t0, t1: primo._vista.t1 };

  // La scheda viene riscritta: nodo nuovo, stessa chiave.
  const secondo = canvasFinto(600, 180, "res:192.0.2.30");
  const reg = disegna(ctx, secondo);
  assert.ok(secondo._vista, "il grafico ricreato ha perso lo zoom");
  assert.equal(secondo._vista.t0, atteso.t0);
  assert.equal(secondo._vista.t1, atteso.t1);
  assert.match(testoDi(reg), /zoom ·/, "e non lo dichiara nemmeno");
});

test("due grafici si zoomano indipendentemente", () => {
  const ctx = contesto();
  const a = canvasFinto(600, 180, "res:192.0.2.30");
  const b = canvasFinto(600, 180, "res:192.0.2.31");
  disegna(ctx, a); disegna(ctx, b);
  ctx.zoomGrafico(a, 0.5, 0.4);
  disegna(ctx, b);
  assert.ok(a._vista, "il primo doveva restare zoomato");
  assert.ok(!b._vista, "il secondo si e' zoomato da solo");
});

test("la chiave e' l'host, non la posizione della scheda", () => {
  // Le schede si riordinano (gli accesi vanno in cima, 0.1.76): con l'indice
  // come chiave lo zoom finirebbe sul grafico di un altro host.
  const ctx = contesto();
  const trenta = canvasFinto(600, 180, "res:192.0.2.30");
  disegna(ctx, trenta);
  ctx.zoomGrafico(trenta, 0.5, 0.4);

  // Ora .30 e' in seconda posizione e .31 in prima: nodi nuovi, chiavi loro.
  const trentuno = canvasFinto(600, 180, "res:192.0.2.31");
  const trentaDopo = canvasFinto(600, 180, "res:192.0.2.30");
  disegna(ctx, trentuno); disegna(ctx, trentaDopo);
  assert.ok(!trentuno._vista, "lo zoom e' finito sull'host sbagliato");
  assert.ok(trentaDopo._vista, "l'host zoomato ha perso lo zoom cambiando posto");
});

test("un canvas senza chiave tiene comunque lo stato sul nodo", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  ctx.zoomGrafico(cv, 0.5, 0.4);
  assert.ok(cv._vista, "senza chiave lo zoom deve funzionare lo stesso");
});


// ── 4. I pulsanti sul grafico ──────────────────────────────────────

const comandiDi = (cv) => cv._comandi;
const premi = (cv, quale) => comandiDi(cv).emit("click", {
  target: { closest: () => ({ dataset: { zoom: quale } }) },
});

test("il grafico monta i suoi tre pulsanti", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  const box = comandiDi(cv);
  assert.ok(box, "nessun comando montato");
  assert.match(box.innerHTML, /data-zoom="in"/);
  assert.match(box.innerHTML, /data-zoom="out"/);
  assert.match(box.innerHTML, /data-zoom="reset"/);
});

test("il piu' zooma, il meno allarga, il reset azzera", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);

  premi(cv, "in"); premi(cv, "in");
  assert.ok(cv._vista, "il + non ha zoomato");
  const stretto = cv._vista.t1 - cv._vista.t0;

  premi(cv, "out");
  assert.ok(cv._vista, "il − ha azzerato invece di allargare");
  assert.ok(cv._vista.t1 - cv._vista.t0 > stretto, "il − non ha allargato");

  premi(cv, "reset");
  assert.ok(!cv._vista, "il reset non ha azzerato");
});

test("i comandi si accendono solo quando c'e' qualcosa da azzerare", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  assert.equal(comandiDi(cv).classi.has("attivo"), false);
  premi(cv, "in");
  assert.equal(comandiDi(cv).classi.has("attivo"), true);
  premi(cv, "reset");
  assert.equal(comandiDi(cv).classi.has("attivo"), false);
});

test("i comandi si montano una volta sola, non uno per ciclo di raccolta", () => {
  const ctx = contesto();
  const cv = canvasFinto();
  for (let i = 0; i < 12; i++) disegna(ctx, cv);
  const box = comandiDi(cv);
  assert.equal(box.parentNode.figli.filter(f => f === box).length, 1);
});

test("allargando fino a coprire tutto si torna alla vista intera", () => {
  // Non resta una finestra "zoomata" larga quanto tutta la serie: sarebbe uno
  // stato che dice di essere zoomato senza esserlo.
  const ctx = contesto();
  const cv = canvasFinto();
  disegna(ctx, cv);
  premi(cv, "in");
  premi(cv, "out");
  assert.ok(!cv._vista);
});
