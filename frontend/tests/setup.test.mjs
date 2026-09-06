/* ===================================================================
   setup.test.mjs — Primo avvio guidato

   La proprieta' che conta: senza configurazione la pagina NON deve
   inventare una rete. Se le interfacce dell'host non si leggono, il
   campo resta vuoto e lo dice, invece di proporre una subnet a caso e
   mandare lo scanner a bussare da qualcun altro.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state };";

function elemento() {
  const memo = new Map();
  const figli = [];
  const el = {
    className: "", textContent: "", value: "", checked: false, title: "",
    hidden: false, style: {}, dataset: {}, innerHTML: "", figli,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    appendChild(f) { figli.push(f); return f; },
    append() {}, remove() {}, focus() {},
    closest: () => null, scrollIntoView() {}, getContext: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 520, top: 0, left: 0 }),
  };
  return el;
}

/* `risposte` mappa URL -> corpo JSON. Cosi' ogni test decide cosa
   risponde il backend senza toccare l'app. */
function contesto(risposte) {
  const chiamate = [];
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const body = elemento();
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: {
      body, createElement: () => elemento(), createElementNS: () => elemento(),
      querySelector: dammi, querySelectorAll: () => [], addEventListener() {},
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url, init) => {
      chiamate.push({ url, init });
      const corpo = risposte[url];
      if (corpo === undefined) {
        return { ok: false, status: 500, text: async () => "{}",
                 headers: { get: () => null } };
      }
      return { ok: true, status: 200, text: async () => JSON.stringify(corpo),
               headers: { get: () => "application/json" } };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: (f) => { if (typeof f === "function") f(); return 0; }, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean, RegExp,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, body, chiamate, dammi };
}

const PROPOSTE = {
  "/api/setup/suggest": {
    subnets: [{ cidr: "192.0.2.0/24", label: "LAN", color: "#5b9cf0", scan: true }],
  },
};

test("senza configurazione si apre il primo avvio, non il login", async () => {
  const { ctx, body, chiamate } = contesto({
    "/api/setup/status": { setup_required: true }, ...PROPOSTE,
  });
  await ctx.boot();
  assert.ok(body.innerHTML.includes("Primo avvio"), "doveva aprirsi il wizard");
  assert.ok(!chiamate.some(c => c.url === "/api/auth/status"),
    "il login non va chiesto prima di sapere quale rete guardare");
});

test("a configurazione fatta il primo avvio non compare", async () => {
  const { ctx, body } = contesto({
    "/api/setup/status": { setup_required: false },
    "/api/auth/status": { auth_required: true, authenticated: false, password_set: true },
  });
  await ctx.boot();
  assert.ok(!body.innerHTML.includes("Primo avvio: quali reti"),
    "il wizard non deve poter riapparire a configurazione fatta");
  assert.ok(body.innerHTML.includes("Accedi per continuare"));
});

test("le reti proposte arrivano dalle interfacce, non dal codice", async () => {
  const { ctx, dammi } = contesto({
    "/api/setup/status": { setup_required: true }, ...PROPOSTE,
  });
  await ctx.boot();
  const righe = dammi("#sw-subnets").figli;
  assert.equal(righe.length, 1);
  assert.ok(righe[0].innerHTML.includes('value="192.0.2.0/24"'));
});

test("se le interfacce non si leggono non viene proposta nessuna rete", async () => {
  // E' il caso che conta: proporre una subnet inventata manderebbe il ping
  // sweep e nmap su una rete che non e' quella di chi installa.
  const { ctx, body, dammi } = contesto({
    "/api/setup/status": { setup_required: true },
    "/api/setup/suggest": { subnets: [] },
  });
  await ctx.boot();
  assert.ok(body.innerHTML.includes("Non e' stato possibile leggere le interfacce"),
    "va detto perche' il campo e' vuoto");
  const righe = dammi("#sw-subnets").figli;
  assert.equal(righe.length, 1, "una riga vuota da compilare");
  assert.ok(!/value="\d+\.\d+\.\d+\.\d+/.test(righe[0].innerHTML),
    "nessun indirizzo precompilato");
});

test("il campo router resta facoltativo", async () => {
  const { ctx, body } = contesto({
    "/api/setup/status": { setup_required: true }, ...PROPOSTE,
  });
  await ctx.boot();
  assert.ok(body.innerHTML.includes("Router (facoltativo)"));
  assert.ok(body.innerHTML.includes("Si puo' aggiungere dopo"),
    "chi non ha un OpenWrt non deve credere di essere bloccato");
});

test("un solo indirizzo scritto a mano, e dichiarato", () => {
  // Stessa regola dei placeholder dei dispositivi: gli esempi vengono dalla
  // configurazione. Qui pero' la configurazione non esiste ancora per
  // definizione, quindi serve un esempio di FORMATO. E' ammesso a patto che sia
  // uno solo e centralizzato: sparso in giro tornerebbe a essere un
  // suggerimento su una rete che non e' quella di chi installa.
  const inizio = SORGENTE.indexOf("const SETUP_CIDR_ESEMPIO");
  const fine = SORGENTE.indexOf("async function boot()");
  const corpo = SORGENTE.slice(inizio, fine);
  const trovati = corpo.split("\n").filter(r =>
    /\b(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+\b/.test(r));
  assert.equal(trovati.length, 1, "l'esempio deve stare in un posto solo");
  assert.match(trovati[0], /^const SETUP_CIDR_ESEMPIO/);
});

test("in inglese il primo avvio e' davvero in inglese", async () => {
  // E' il caso di chi installa l'app: la lingua di default e' l'inglese, e la
  // prima schermata che vede e' questa. Se restasse in italiano, il bilingue
  // sarebbe una promessa non mantenuta proprio dove conta.
  const { ctx, body } = contesto({
    "/api/setup/status": { setup_required: true }, ...PROPOSTE,
  });
  ctx.I18N.setLang("en");
  await ctx.boot();
  const html = body.innerHTML;
  assert.ok(html.includes("which networks"), "il titolo e' rimasto in italiano");
  assert.ok(html.includes("Router (optional)"));
  assert.ok(html.includes("Save and continue"));
  assert.ok(!html.includes("Primo avvio"), "resta del testo italiano in pagina");
  assert.ok(!html.includes("facoltativo"));
});

test("in inglese anche la schermata di accesso e' in inglese", async () => {
  const { ctx, body } = contesto({
    "/api/setup/status": { setup_required: false },
    "/api/auth/status": { auth_required: true, authenticated: false, password_set: false },
  });
  ctx.I18N.setLang("en");
  await ctx.boot();
  const html = body.innerHTML;
  assert.ok(html.includes("First run: set the admin password"));
  assert.ok(html.includes("Set and sign in"));
  assert.ok(!html.includes("Primo accesso"));
});
