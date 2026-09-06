/* ===================================================================
   app.test.mjs — SPA (frontend/app.js)

   app.js e' uno script classico che si limita a registrare `boot` su
   DOMContentLoaded: caricandolo in un contesto vm con un DOM finto si
   ottengono le sue funzioni senza far partire nulla.

   Copre una regressione vera: la pagina Servizi si ridisegna dopo
   aver caricato gli stati storici, e `renderIfLive` la ridisegna daccapo
   perche' `PAGES.services` non ha un refresh parziale. Se il ridisegno non
   fosse condizionato al fatto di aver caricato dati **nuovi**, i due si
   richiamerebbero all'infinito e la pagina si pianterebbe.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

/* `state` e `PAGES` sono `const`: in un contesto vm non diventano proprieta'
   dell'oggetto globale (solo `var` e le function declaration lo fanno). Un
   epilogo li espone senza toccare il sorgente vero. */
const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, PAGES };";

/* Elemento minimo: tiene il conto di quante volte gli si riscrive dentro.

   Oltre `TETTO` riscritture solleva invece di lasciar correre: un ciclo di
   ridisegni non ha fine, e un test che si pianta bloccherebbe anche il giro di
   verifica invece di segnalare il difetto. */
const TETTO = 50;

function elemento() {
  const el = {
    className: "", textContent: "", value: "", checked: false, dataset: {},
    style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    scritture: 0,
    set innerHTML(v) {
      this._html = v;
      this.scritture += 1;
      if (this.scritture > TETTO)
        throw new Error(`ridisegnata piu' di ${TETTO} volte: ciclo di ridisegni`);
    },
    get innerHTML() { return this._html || ""; },
    querySelector: () => elemento(),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, focus() {}, remove() {},
    appendChild(f) { return f; },
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  return el;
}

function contesto(risposta) {
  const chiamate = [];
  const document = {
    querySelector: () => elemento(),
    querySelectorAll: () => [],
    createElement: () => elemento(),
    addEventListener() {},
    body: elemento(),
  };
  return {
    ctx: createContext({
      window: { addEventListener() {}, devicePixelRatio: 1 },
      document, console,
      location: { hash: "", reload() {} },
      // api() legge il corpo come testo e guarda gli header: il finto deve
      // esporre le stesse cose della risposta vera, o ogni chiamata fallisce.
      fetch: async (url) => {
        chiamate.push(url);
        return {
          ok: true, status: 200,
          text: async () => JSON.stringify(risposta),
          headers: { get: () => null },
        };
      },
      AbortController,
      setTimeout, clearTimeout,
      // No-op: un intervallo vero (startMonitorEta, il conto alla rovescia
      // della pagina Monitoraggio) terrebbe vivo il processo di test per
      // sempre, e qui non e' l'oggetto in esame.
      setInterval: () => 0, clearInterval: () => {},
      Date, Math, JSON, WebSocket: function () {},
    }),
    chiamate,
  };
}

const STATI = {
  devices: [{ key: "192.0.2.5", online: false, since: Date.now() - 3 * 86400e3 }],
  services: [{ kind: "systemd", host: "192.0.2.10", name: "ssh.service",
               ok: false, since: Date.now() - 7200e3 }],
};

/* Lascia girare le promise in coda: il rimbalzo, se c'e', si manifesta qui. */
const drena = () => new Promise((r) => setTimeout(r, 30));


test("loadStatiStorici dice se ha caricato dati nuovi", async () => {
  const { ctx } = contesto(STATI);
  runInContext(SORGENTE, ctx);
  assert.equal(await ctx.loadStatiStorici(), true, "la prima chiamata carica");
  assert.equal(await ctx.loadStatiStorici(), false,
    "la seconda e' dentro la finestra di throttling e non deve dire di aver caricato");
});


test("la pagina Servizi non entra in un ciclo di ridisegni", async () => {
  const { ctx, chiamate } = contesto(STATI);
  runInContext(SORGENTE, ctx);
  ctx.__interni.state.route = "services";
  const view = elemento();
  ctx.document.querySelector = (sel) => (sel === "#view" ? view : elemento());

  ctx.pageServices(view, { services: { systemd: [], healthchecks: [], summary: {} } });
  await drena();

  // Un ridisegno iniziale piu' al massimo uno dopo l'arrivo degli stati.
  assert.ok(view.scritture <= 2,
    `la pagina si e' ridisegnata ${view.scritture} volte: il rimbalzo non si ferma`);
  assert.equal(chiamate.filter((u) => u.includes("/api/history/states")).length, 1,
    "gli stati storici vanno chiesti una volta sola");
});


test("daQuando tace quando lo storico non concorda con lo stato attuale", () => {
  const { ctx } = contesto(STATI);
  runInContext(SORGENTE, ctx);
  const spento = { online: false, since: Date.now() - 86400e3 };
  assert.equal(ctx.daQuando(spento, false).length > 0, true);
  // Device gia' riacceso ma storico indietro di un ciclo: meglio niente che
  // "offline da un giorno" su una macchina accesa.
  assert.equal(ctx.daQuando(spento, true), "");
  assert.equal(ctx.daQuando(undefined, true), "");
});
