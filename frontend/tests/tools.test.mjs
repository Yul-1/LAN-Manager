/* ===================================================================
   tools.test.mjs — Pagina Tools di rete

   Quattro strumenti aggiunti su richiesta del proprietario: whois,
   misura di velocita', HTTP (quello che si farebbe con curl) e ARP
   ping. La pagina costruisce il form dal catalogo che il backend
   dichiara, quindi i casi da tenere fermi sono suoi:
     - un tool senza bersaglio (la misura di velocita') non deve
       mostrare il campo, ne' mandare l'ultimo host digitato per un
       altro strumento;
     - le opzioni di tipo testo (l'interfaccia dell'ARP ping) non
       esistevano: il form sapeva fare solo numeri, scelte e spunte;
     - il segnaposto del bersaglio diceva "host, IP o subnet" anche
       dove ci va un URL.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, toolsState };";

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

/* Il catalogo vero, come lo manda routers/tools.py. */
const CATALOGO = [
  { id: "ping", label: "Ping", help: "raggiungibilita' e latenza", max_seconds: 35,
    options: { count: { type: "int", default: 4, min: 1, max: 10 } } },
  { id: "arping", label: "ARP ping", help: "vivo sulla LAN anche se ignora il ping",
    options: { count: { type: "int", default: 3, min: 1, max: 10 },
               iface: { type: "text", default: "", label: "interfaccia (vuoto = automatica)" } } },
  { id: "whois", label: "Whois", help: "di chi e' un dominio o un IP pubblico", options: {} },
  { id: "http", label: "HTTP (curl)", help: "stato, redirect e intestazioni di un URL",
    options: { method: { type: "choice", default: "GET", values: ["GET", "HEAD"] },
               follow: { type: "bool", default: true, label: "segui i redirect" } } },
  { id: "speedtest", label: "Velocita' della linea", help: "consuma traffico (5 MB di serie)",
    no_target: true, max_seconds: 60,
    options: { mb: { type: "int", default: 5, min: 1, max: 50, label: "MB" } } },
];

function contesto(risposta = { tool: "x", target: "y", command: "c", output: "o",
                               exit_code: 0, duration_ms: 12 }) {
  const chiamate = [];
  const nodi = new Map();
  const opzioni = [];
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi,
                // Le opzioni del form si raccolgono con querySelectorAll(".tl-opt"):
                // il test le registra a mano, come farebbe il DOM vero.
                querySelectorAll: (sel) => sel === ".tl-opt" ? opzioni : [],
                addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url, init) => {
      chiamate.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      // La pagina passa dal gate di sessione e rilegge il catalogo: qui
      // rispondono come il backend, altrimenti si finisce nella schermata di
      // login invece che negli strumenti.
      const corpo = url.includes("/api/auth/status") ? { session: true, password_set: true }
        : url.includes("/api/tools/run") ? risposta
        : url.includes("/api/tools/") ? { tools: CATALOGO, ids: CATALOGO.map(t => t.id) }
        : {};
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(corpo) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  ctx.__interni.toolsState.catalog = CATALOGO;
  return { ctx, dammi, chiamate, opzioni };
}

function rendi(ctx, id) {
  const view = elemento();
  ctx.__interni.toolsState.tool = id;
  ctx.__interni.toolsState.options = {};
  ctx.renderTools(view);
  return view.innerHTML;
}


// ── Il form si adatta allo strumento ───────────────────────────────

test("la misura di velocita' non chiede un bersaglio", () => {
  const { ctx } = contesto();
  const html = rendi(ctx, "speedtest");
  assert.ok(!html.includes('id="tl-target"'),
    "un campo bersaglio per la misura di velocita' e' una domanda senza risposta giusta");
  assert.ok(html.includes("MB"), "ma l'opzione dei MB c'e'");
});

test("gli altri strumenti il bersaglio ce l'hanno, col segnaposto giusto", () => {
  const { ctx } = contesto();
  assert.match(rendi(ctx, "http"), /placeholder="https:\/\/esempio/);
  assert.match(rendi(ctx, "whois"), /placeholder="dominio o IP pubblico…"/);
  assert.match(rendi(ctx, "arping"), /placeholder="IP sulla stessa rete…"/);
  assert.match(rendi(ctx, "ping"), /placeholder="host, IP o subnet…"/);
});

test("un'opzione di testo diventa un campo di testo", () => {
  // Il form sapeva fare numeri, scelte e spunte: l'interfaccia dell'ARP ping
  // sarebbe finita in un campo numerico.
  const { ctx } = contesto();
  const html = rendi(ctx, "arping");
  assert.match(html, /class="tl-opt" data-k="iface" data-t="str"/);
  assert.ok(html.includes('type="text"'));
});


// ── Cosa viene mandato al backend ──────────────────────────────────

test("per un tool senza bersaglio non si manda l'host di un altro", async () => {
  const { ctx, chiamate } = contesto();
  ctx.__interni.toolsState.target = "192.0.2.1";      // digitato per il ping
  const view = elemento();
  ctx.__interni.toolsState.tool = "speedtest";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO.find(t => t.id === "speedtest"));
  const inviato = chiamate[chiamate.length - 1].body;
  assert.equal(inviato.tool, "speedtest");
  assert.equal(inviato.target, "", "un bersaglio inventato e' un parametro che nessuno ha chiesto");
});

test("le opzioni viaggiano col tipo giusto", async () => {
  const { ctx, chiamate, opzioni } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.tool = "http";
  ctx.__interni.toolsState.target = "https://esempio.it";
  ctx.renderTools(view);
  opzioni.push(elemento({ dataset: { k: "method", t: "str" }, value: "HEAD" }),
               elemento({ dataset: { k: "follow", t: "bool" }, checked: false }),
               elemento({ dataset: { k: "timeout", t: "int" }, value: "7" }));
  await ctx.runTool(view, CATALOGO.find(t => t.id === "http"));
  const inviato = chiamate[chiamate.length - 1].body;
  assert.deepEqual(inviato.options, { method: "HEAD", follow: false, timeout: 7 });
  assert.equal(inviato.target, "https://esempio.it");
});

test("l'esito mostra comando, output e durata", async () => {
  const { ctx, dammi } = contesto({ tool: "whois", target: "esempio.it",
    command: "whois -h whois.iana.org esempio.it", output: "domain: ESEMPIO.IT",
    exit_code: 0, duration_ms: 340 });
  const view = elemento();
  ctx.__interni.toolsState.tool = "whois";
  ctx.__interni.toolsState.target = "esempio.it";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO.find(t => t.id === "whois"));
  const fuori = ctx.__interni.toolsState.out;
  assert.ok(fuori.includes("whois -h whois.iana.org"), fuori);
  assert.ok(fuori.includes("domain: ESEMPIO.IT"));
  assert.ok(fuori.includes("340 ms"));
});


// ── Giro di controllo della pagina ─────────────────────────────────

test("il catalogo si rilegge entrando: gli strumenti nuovi compaiono", async () => {
  const { ctx, chiamate } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.catalog = [CATALOGO[0]];      // elenco vecchio in cache
  await ctx.pageTools(view);
  assert.ok(chiamate.some(c => c.url.includes("/api/tools/")),
    "con la cache eterna gli strumenti nuovi comparivano solo ricaricando la pagina");
});

test("un bersaglio mancante non spende una richiesta", async () => {
  // Il backend lo rifiuterebbe comunque, ma quel rifiuto consumava uno dei
  // dieci colpi al minuto: dieci clic a vuoto bloccavano gli strumenti.
  const { ctx, chiamate } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.tool = "ping";
  ctx.__interni.toolsState.target = "   ";
  ctx.renderTools(view);
  const prima = chiamate.length;
  await ctx.runTool(view, CATALOGO[0]);
  assert.equal(chiamate.length, prima, "nessuna richiesta");
  assert.match(ctx.__interni.toolsState.out, /ha bisogno di un bersaglio/);
});

test("il tool senza bersaglio parte anche col campo vuoto", async () => {
  const { ctx, chiamate } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.tool = "speedtest";
  ctx.__interni.toolsState.target = "";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO.find(t => t.id === "speedtest"));
  assert.ok(chiamate.some(c => c.url.includes("/api/tools/run")));
});

test("premere Invio mentre gira non fa partire una seconda esecuzione", async () => {
  const { ctx, chiamate } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.tool = "ping";
  ctx.__interni.toolsState.target = "192.0.2.1";
  ctx.renderTools(view);
  const uno = ctx.runTool(view, CATALOGO[0]);
  const due = ctx.runTool(view, CATALOGO[0]);      // Invio mentre la prima gira
  await Promise.all([uno, due]);
  assert.equal(chiamate.filter(c => c.url.includes("/api/tools/run")).length, 1);
});

test("mentre gira si vede il tempo che scorre e quanto puo' durare", async () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  ctx.__interni.toolsState.tool = "ping";
  ctx.__interni.toolsState.target = "192.0.2.1";
  ctx.renderTools(view);
  const p = ctx.runTool(view, CATALOGO[0]);
  assert.match(dammi("#tl-crono").textContent, /^\d+s · al massimo 35s$/,
    "davanti a un nmap da tre minuti, 'in corso…' senza orizzonte sembra un blocco");
  await p;
  assert.equal(ctx.__interni.toolsState.running, false);
});

test("l'esito e' una parola, non un numero da interpretare", async () => {
  const spento = { tool: "arping", target: "192.0.2.9", command: "arping 192.0.2.9",
                   output: "Received 0 response(s)", exit_code: 1, duration_ms: 2009 };
  const { ctx } = contesto(spento);
  const view = elemento();
  ctx.__interni.toolsState.tool = "arping";
  ctx.__interni.toolsState.target = "192.0.2.9";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO.find(t => t.id === "arping"));
  assert.match(ctx.__interni.toolsState.out, /nessun risultato \(uscita 1\)/);
  assert.ok(!ctx.__interni.toolsState.out.includes("uscita 1 ·"),
    "'uscita 1' da solo sembrava un errore del programma invece di un IP spento");
});

test("un tool interrotto dal tempo massimo lo dichiara", async () => {
  const { ctx } = contesto({ tool: "nmap", target: "192.0.2.0/24", command: "nmap …",
                             output: "…", exit_code: null, timed_out: true, duration_ms: 180000 });
  const view = elemento();
  ctx.__interni.toolsState.tool = "ping";
  ctx.__interni.toolsState.target = "192.0.2.0/24";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO[0]);
  assert.match(ctx.__interni.toolsState.out, /interrotto: superato il tempo massimo/);
});


/* ── L'output esce mentre il comando gira ──────────────────────────
   Prima un traceroute lungo o un nmap da tre minuti mostravano una
   schermata vuota fino all'ultimo secondo: l'output arrivava tutto
   insieme alla fine. Ora i tool che il catalogo marca `stream: true`
   passano da /api/tools/stream, che manda una riga JSON per evento.

   Qui il flusso e' finto (un ReadableStream costruito a mano) perche'
   cio' che si verifica e' la pagina: che scriva man mano senza rifare
   il render, che il risultato finale sia lo stesso della rotta
   one-shot, e che un tool non-streaming resti dov'era. */

const PING_STREAM = { ...CATALOGO[0], stream: true };

/* Corpo NDJSON consegnato a pezzi, come farebbe il backend. */
function flusso(pezzi) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    getReader: () => ({
      read: async () => i < pezzi.length
        ? { done: false, value: enc.encode(pezzi[i++]) }
        : { done: true, value: undefined },
    }),
  };
}

function contestoStream(pezzi, stato = 200) {
  const chiamate = [];
  const nodi = new Map();
  const scritture = [];
  const dammi = (sel) => {
    if (!nodi.has(sel)) {
      // #tl-testo registra ogni testo scritto: e' la prova che la pagina
      // aggiorna man mano invece di aspettare la fine.
      const el = elemento();
      if (sel === "#tl-testo") {
        Object.defineProperty(el, "textContent", {
          get: () => scritture[scritture.length - 1] || "",
          set: (v) => { scritture.push(v); },
        });
      }
      nodi.set(sel, el);
    }
    return nodi.get(sel);
  };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url, init) => {
      chiamate.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      if (url.includes("/api/tools/stream")) {
        return stato === 200
          ? { ok: true, status: 200, body: flusso(pezzi),
              headers: { get: () => null }, text: async () => pezzi.join("") }
          : { ok: false, status: stato, body: null, headers: { get: () => null },
              text: async () => JSON.stringify({ detail: "bersaglio non valido" }) };
      }
      const corpo = url.includes("/api/auth/status") ? { session: true, password_set: true }
        : url.includes("/api/tools/run") ? { tool: "ping", target: "192.0.2.1",
            command: "ping (one-shot)", output: "tutto insieme", exit_code: 0, duration_ms: 9 }
        : { tools: CATALOGO, ids: CATALOGO.map(t => t.id) };
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(corpo) };
    },
    AbortController, TextDecoder, TextEncoder,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  ctx.__interni.toolsState.catalog = [PING_STREAM, ...CATALOGO.slice(1)];
  return { ctx, chiamate, scritture, dammi };
}

async function eseguiStream(ctx, pezzi) {
  const view = elemento();
  ctx.__interni.toolsState.tool = "ping";
  ctx.__interni.toolsState.target = "192.0.2.1";
  ctx.renderTools(view);
  await ctx.runTool(view, PING_STREAM);
  return view;
}

test("un tool che scrive man mano passa dalla rotta in diretta", async () => {
  const { ctx, chiamate } = contestoStream([
    JSON.stringify({ type: "start", tool: "ping", target: "192.0.2.1", command: "ping -c 4 192.0.2.1" }) + "\n",
    JSON.stringify({ type: "end", exit_code: 0, timed_out: false, truncated: false, duration_ms: 30 }) + "\n",
  ]);
  await eseguiStream(ctx);
  assert.ok(chiamate.some(c => c.url.includes("/api/tools/stream")));
  assert.ok(!chiamate.some(c => c.url.includes("/api/tools/run")),
    "con lo streaming disponibile non si chiama anche la rotta one-shot");
});

test("le righe si vedono arrivare, non tutte alla fine", async () => {
  const { ctx, scritture } = contestoStream([
    JSON.stringify({ type: "start", command: "ping -c 3 192.0.2.1" }) + "\n",
    JSON.stringify({ type: "out", text: "prima riga\n" }) + "\n",
    JSON.stringify({ type: "out", text: "seconda riga\n" }) + "\n",
    JSON.stringify({ type: "end", exit_code: 0, duration_ms: 2100 }) + "\n",
  ]);
  await eseguiStream(ctx);
  // Una scrittura per evento: e' esattamente cio' che prima non succedeva.
  assert.ok(scritture.length >= 3, `scritture: ${scritture.length}`);
  assert.ok(scritture[1].includes("prima riga") && !scritture[1].includes("seconda riga"),
    "la prima riga si e' vista prima che arrivasse la seconda");
  assert.ok(scritture[scritture.length - 1].includes("seconda riga"));
  assert.match(ctx.__interni.toolsState.out, /prima riga\nseconda riga/);
  assert.match(ctx.__interni.toolsState.out, /riuscito · 2100 ms/);
});

test("un evento spezzato fra due pezzi non si perde", async () => {
  // I chunk della rete non rispettano i confini delle righe.
  const riga = JSON.stringify({ type: "out", text: "intera\n" }) + "\n";
  const { ctx } = contestoStream([
    JSON.stringify({ type: "start", command: "ping x" }) + "\n" + riga.slice(0, 12),
    riga.slice(12) + JSON.stringify({ type: "end", exit_code: 0, duration_ms: 5 }) + "\n",
  ]);
  await eseguiStream(ctx);
  assert.match(ctx.__interni.toolsState.out, /intera/);
  assert.match(ctx.__interni.toolsState.out, /riuscito/);
});

test("un flusso che finisce senza 'end' non si spaccia per riuscito", async () => {
  const { ctx } = contestoStream([
    JSON.stringify({ type: "start", command: "nmap x" }) + "\n",
    JSON.stringify({ type: "out", text: "meta' output\n" }) + "\n",
  ]);
  await eseguiStream(ctx);
  assert.match(ctx.__interni.toolsState.out, /meta' output/, "cio' che e' arrivato resta");
  assert.match(ctx.__interni.toolsState.out, /interrotto prima della fine/);
});

test("un input rifiutato dalla rotta in diretta si spiega come sempre", async () => {
  const { ctx, chiamate } = contestoStream([], 400);
  await eseguiStream(ctx);
  assert.match(ctx.__interni.toolsState.out, /bersaglio non valido/);
  assert.ok(!chiamate.some(c => c.url.includes("/api/tools/run")),
    "un 400 non si ritenta: il bersaglio sarebbe lo stesso");
});

test("la sessione scaduta durante lo streaming ripassa da api()", async () => {
  // api() sa rifare il login inline e riprendere la richiesta; leggere un
  // flusso no. Si perde lo scorrere dell'output, non il risultato.
  const { ctx, chiamate } = contestoStream([], 401);
  await eseguiStream(ctx);
  assert.ok(chiamate.some(c => c.url.includes("/api/tools/run")));
  assert.match(ctx.__interni.toolsState.out, /tutto insieme/);
});

test("un tool senza output progressivo resta sulla rotta one-shot", async () => {
  const { ctx, chiamate } = contestoStream([]);
  const view = elemento();
  ctx.__interni.toolsState.tool = "whois";
  ctx.__interni.toolsState.target = "esempio.it";
  ctx.renderTools(view);
  await ctx.runTool(view, CATALOGO.find(t => t.id === "whois"));
  assert.ok(!chiamate.some(c => c.url.includes("/api/tools/stream")),
    "un risultato parziale del whois non esiste: prometterlo sarebbe una bugia");
  assert.ok(chiamate.some(c => c.url.includes("/api/tools/run")));
});
