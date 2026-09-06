/* ===================================================================
   terminal-pagina.test.mjs — Pagina Terminale SSH

   Non l'emulatore (quello sta in terminal.test.mjs): la pagina che lo
   ospita, cioe' la parte che parla di stato e di errori.

   Il difetto principale: il rifiuto della connessione avviene durante
   l'handshake del WebSocket (origine non consentita, sessione assente:
   `routers/terminal.py` chiude prima di accettare), quindi il browser
   riceve un 1006 muto — niente codice, niente motivo. La pagina diceva
   soltanto "chiuso", e una sessione scaduta era indistinguibile da un
   terminale spento in configurazione o da un backend caduto.

   Piu' due difetti minori: una chiusura generica cancellava la
   spiegazione appena mostrata, e con `terminal.js` non caricato il
   pulsante Connetti finiva in un ReferenceError silenzioso.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, term };";

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

/* Emulatore finto: si guarda cosa la pagina ci scrive dentro. */
function emulatore() {
  const scritto = [];
  return {
    scritto, el: elemento(), cols: 100, rows: 30,
    write(t) { scritto.push(t); }, fit() { return { cols: 100, rows: 30 }; },
    focus() {}, dispose() {}, onData() {},
    testo() { return scritto.join(""); },
  };
}

function contesto({ statoAuth = { session: true, password_set: true }, authOk = true,
                    conTerminal = true } = {}) {
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const creati = [];
  const emu = emulatore();
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => ({ matches: false }) },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "lanmng:81", protocol: "http:", reload() {} },
    fetch: async () => (authOk
      ? { ok: true, status: 200, headers: { get: () => "application/json" },
          text: async () => JSON.stringify(statoAuth) }
      : Promise.reject(new TypeError("Failed to fetch"))),
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    ResizeObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function (url) {
      this.url = url; this.readyState = 0;
      this.close = () => {};
      creati.push(this);
    },
  });
  if (conTerminal) ctx.Terminal = function () { return emu; };
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, creati, emu };
}

function apri(ctx, dammi) {
  ctx.__interni.term.host = "198.51.100.10";
  dammi("#tm-box");                     // il contenitore esiste in pagina
  ctx.connectTerminal();
}


// ── Il motivo della chiusura ───────────────────────────────────────

test("una chiusura in handshake con la sessione scaduta lo dice", async () => {
  const { ctx, dammi, creati, emu } = contesto({ statoAuth: { session: false, password_set: true } });
  apri(ctx, dammi);
  await creati[0].onclose({ code: 1006 });      // rifiuto muto, come dal browser
  await new Promise(r => setTimeout(r, 10));
  assert.match(emu.testo(), /sessione e' scaduta/);
  assert.match(dammi("#tm-state").innerHTML, /sessione scaduta/);
});

test("con la sessione buona il motivo e' un altro, e si dice quale", async () => {
  const { ctx, dammi, creati, emu } = contesto({ statoAuth: { session: true, password_set: true } });
  apri(ctx, dammi);
  await creati[0].onclose({ code: 1006 });
  await new Promise(r => setTimeout(r, 10));
  assert.match(emu.testo(), /rifiutato la connessione/);
  assert.match(emu.testo(), /terminale sia attivo/);
  assert.match(dammi("#tm-state").innerHTML, /connessione rifiutata/);
});

test("se non risponde nemmeno il backend, non si accusa la sessione", async () => {
  const { ctx, dammi, creati, emu } = contesto({ authOk: false });
  apri(ctx, dammi);
  await creati[0].onclose({ code: 1006 });
  await new Promise(r => setTimeout(r, 30));
  assert.match(emu.testo(), /backend non risponde/);
});

test("una chiusura dopo una spiegazione non la cancella", async () => {
  const { ctx, dammi, creati, emu } = contesto();
  apri(ctx, dammi);
  const ws = creati[0];
  ws.onmessage({ data: JSON.stringify({ type: "closed", reason: "chiusa dopo 900s di inattivita'" }) });
  const primaDelClose = dammi("#tm-state").innerHTML;
  await ws.onclose({ code: 1000 });
  await new Promise(r => setTimeout(r, 10));
  assert.equal(dammi("#tm-state").innerHTML, primaDelClose,
    "il motivo vero e' quello arrivato dal server, non il 'chiuso' che segue");
  assert.match(emu.testo(), /inattivita'/);
});

test("dopo 'ready' una chiusura non chiede spiegazioni a nessuno", async () => {
  const { ctx, dammi, creati, emu } = contesto();
  apri(ctx, dammi);
  const ws = creati[0];
  ws.onmessage({ data: JSON.stringify({ type: "ready", host: "198.51.100.10", user: "user" }) });
  assert.match(dammi("#tm-state").innerHTML, /connesso a user@198.51.100.10/);
  await ws.onclose({ code: 1000 });
  await new Promise(r => setTimeout(r, 10));
  assert.ok(!emu.testo().includes("Connessione non aperta"),
    "la sessione era aperta davvero: parlare di rifiuto sarebbe falso");
});


// ── terminal.js non caricato ───────────────────────────────────────

test("senza il modulo del terminale si dice, invece di non fare nulla", () => {
  const { ctx, dammi } = contesto({ conTerminal: false });
  ctx.connectTerminal();
  assert.match(dammi("#tm-box").innerHTML, /terminal\.js/);
  assert.match(dammi("#tm-state").innerHTML, /non caricato/);
});
