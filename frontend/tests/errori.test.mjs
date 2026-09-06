/* ===================================================================
   errori.test.mjs — helper api(), notifiche, sessione

   In origine il frontend aveva 39 fetch() scritte a mano e 38
   catch che ingoiavano l'eccezione senza loggarla: un guasto di rete, un
   500 e un timeout producevano tutti la stessa stringa "Errore: rete".
   Qui si verifica che ciascun fallimento venga riconosciuto per quello che
   e', che la chiamata abbia davvero un tetto di tempo, e che una sessione
   scaduta non faccia perdere l'operazione in corso.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

// PERCORSO resta: l'ultimo test ispeziona il sorgente di app.js DA SOLO
// (fetch sparse, alert, messaggi generici), e li' la concatenazione con i
// cataloghi falserebbe i conteggi.
const PERCORSO = new URL("../app.js", import.meta.url);
const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES, toastVivi };";

/* Risposta finta: `api()` legge SEMPRE il corpo come testo, quindi il finto
   deve esporre text() e headers.get() come quello vero. */
function risposta(status, corpo, headers = {}) {
  const testo = typeof corpo === "string" ? corpo : JSON.stringify(corpo);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => testo,
    headers: { get: (k) => headers[k] ?? headers[String(k).toLowerCase()] ?? null },
  };
}

function elemento() {
  const figli = [];
  const el = {
    className: "", id: "", textContent: "", value: "", style: {}, dataset: {},
    innerHTML: "", attributi: {}, staccato: false,
    get children() { return figli; },
    setAttribute(k, v) { this.attributi[k] = v; },
    appendChild(f) { figli.push(f); f.parentNode = el; return f; },
    remove() { this.staccato = true; if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this); if (i >= 0) this.parentNode.children.splice(i, 1); } },
    querySelector: () => elemento(),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, focus() {},
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  return el;
}

/* `fetch` programmabile: si passa una funzione (url, init) -> risposta, o una
   coda di risposte consumate in ordine. Ogni chiamata viene registrata. */
function contesto(risponditore) {
  const chiamate = [];
  const body = elemento();
  const nodi = new Map();
  const document = {
    body,
    createElement: () => elemento(),
    // Cerca fra i nodi montati sul body: senza, toastBox() non ritroverebbe
    // il contenitore appena creato e ne farebbe uno nuovo ad ogni notifica.
    querySelector: (sel) => nodi.get(sel)
      || body.children.find((c) => "#" + c.id === sel) || null,
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const coda = Array.isArray(risponditore) ? risponditore.slice() : null;
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1 },
    document, console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url, init) => {
      chiamate.push({ url, init });
      const r = coda ? coda.shift() : await risponditore(url, init, chiamate.length);
      if (r instanceof Error) throw r;
      if (typeof r === "function") return r(init);
      return r;
    },
    AbortController, setTimeout, clearTimeout,
    // Un intervallo vero terrebbe vivo il processo di test per sempre.
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object,
    WebSocket: function () {},
  });
  runInContext(SORGENTE, ctx);
  return { ctx, chiamate, body, nodi };
}

/* Errore di rete come lo produce fetch quando il server non risponde. */
const RETE = () => { const e = new Error("failed to fetch"); e.name = "TypeError"; return e; };


test("un 500 con corpo HTML non passa per un guasto di rete", async () => {
  const { ctx } = contesto(async () => risposta(500, "<html><body>Internal Server Error</body></html>"));
  const r = await ctx.api("/api/devices/");
  assert.equal(r.ok, false);
  assert.equal(r.error.kind, "server");
  assert.match(r.error.messaggio, /backend/i);
  assert.doesNotMatch(r.error.messaggio, /\[object/);
});

test("la rete caduta e' distinguibile dal 500", async () => {
  const { ctx } = contesto(async () => RETE());
  const r = await ctx.api("/api/devices/");
  assert.equal(r.error.kind, "rete");
  assert.equal(r.status, 0);
});

test("una chiamata che non risponde viene interrotta dal budget di tempo", async () => {
  // Il fetch finto rispetta il segnale come quello vero: senza AbortController
  // questa promessa non si risolverebbe mai e il test andrebbe in timeout.
  const { ctx } = contesto((url, init) => new Promise((_, rej) => {
    init.signal.addEventListener("abort", () => {
      const e = new Error("aborted"); e.name = "AbortError"; rej(e);
    });
  }));
  const r = await ctx.api("/api/devices/", { timeout: 30, retry: false });
  assert.equal(r.error.kind, "timeout");
});

test("il 422 di FastAPI diventa una frase e non [object Object]", async () => {
  const { ctx } = contesto(async () => risposta(422, {
    detail: [{ loc: ["body", "port"], msg: "input should be a valid integer" }],
  }));
  const r = await ctx.api("/api/services/", { method: "POST", body: { port: "x" } });
  assert.equal(r.error.kind, "invalido");
  assert.match(r.error.detail, /port: input should be/);
  assert.doesNotMatch(r.error.messaggio, /\[object/);
});

test("il 429 riporta quanto attendere", async () => {
  const { ctx } = contesto(async () => risposta(429, { detail: "troppe esecuzioni" }, { "Retry-After": "12" }));
  const r = await ctx.api("/api/tools/run", { method: "POST", body: {} });
  assert.equal(r.error.kind, "limite");
  assert.equal(r.error.retryAfter, 12);
  assert.match(r.error.messaggio, /12s/);
});

test("si ritenta una GET caduta, mai una mutazione", async () => {
  const a = contesto(async () => RETE());
  await a.ctx.api("/api/devices/");
  assert.equal(a.chiamate.length, 2, "una GET caduta sulla rete si ritenta una volta");

  const b = contesto(async () => RETE());
  await b.ctx.api("/api/devices/x", { method: "DELETE" });
  assert.equal(b.chiamate.length, 1, "una mutazione potrebbe essere gia' passata: non si ripete");
});

test("un timeout non si ritenta: l'attesa c'e' gia' stata", async () => {
  const { ctx, chiamate } = contesto((url, init) => new Promise((_, rej) => {
    init.signal.addEventListener("abort", () => {
      const e = new Error("aborted"); e.name = "AbortError"; rej(e);
    });
  }));
  await ctx.api("/api/devices/", { timeout: 30 });
  assert.equal(chiamate.length, 1);
});

test("un 200 con corpo non-JSON non passa per successo", async () => {
  const { ctx } = contesto(async () => risposta(200, "<html>pagina del proxy</html>"));
  const r = await ctx.api("/api/devices/");
  assert.equal(r.ok, false);
  assert.equal(r.error.kind, "risposta");
});

test("il 503 di require_session non viene scambiato per il proxy giu'", async () => {
  const { ctx } = contesto(async () => risposta(503, { detail: "serve una password admin" }));
  const r = await ctx.api("/api/tools/run", { method: "POST", body: {} });
  assert.equal(r.error.kind, "servizio");
  assert.match(r.error.messaggio, /password admin/);
});

test("statoGate distingue l'API giu' dalla password mai impostata", () => {
  const { ctx } = contesto(async () => risposta(200, {}));
  assert.equal(ctx.statoGate({ ok: false, error: { kind: "rete" } }), "offline",
    "con l'API giu' non si puo' accusare il proprietario di non aver messo la password");
  assert.equal(ctx.statoGate({ ok: true, data: { session: true } }), "ok");
  assert.equal(ctx.statoGate({ ok: true, data: { password_set: true } }), "login");
  assert.equal(ctx.statoGate({ ok: true, data: { password_set: false } }), "senza-password");
});

test("due chiamate in 401 aprono un solo pannello e riprendono entrambe", async () => {
  let pannelli = 0;
  const { ctx } = contesto(async (url) => {
    if (url.startsWith("/api/auth/status")) return risposta(200, { session: false, password_set: true });
    if (url.startsWith("/api/auth/login")) return risposta(200, { ok: true });
    return ctx.__rientrato ? risposta(200, { fatto: true }) : risposta(401, { detail: "non autenticato" });
  });
  // Il pannello vero richiede un DOM completo: qui si sostituisce con una
  // finta che conta le aperture, che e' l'oggetto in esame.
  ctx.pannelloLogin = async () => { pannelli += 1; ctx.__rientrato = true; return true; };

  const [a, b] = await Promise.all([ctx.api("/api/devices/"), ctx.api("/api/services/")]);
  assert.equal(pannelli, 1, "cinque chiamate in 401 non devono aprire cinque login");
  assert.equal(a.ok, true, "la chiamata interrotta viene ripresa e restituita al chiamante");
  assert.equal(b.ok, true);
});

test("un 401 che si ripete dopo il login non riapre il pannello", async () => {
  let pannelli = 0;
  const { ctx } = contesto(async () => risposta(401, { detail: "non autenticato" }));
  ctx.pannelloLogin = async () => { pannelli += 1; return true; };
  const r = await ctx.api("/api/devices/");
  assert.equal(pannelli, 1, "una sola ripresa: altrimenti si cicla all'infinito");
  assert.equal(r.ok, false);
  assert.equal(r.error.kind, "sessione");
});

test("le chiamate di login non passano dall'interceptor", async () => {
  let pannelli = 0;
  const { ctx } = contesto(async () => risposta(401, { detail: "credenziali non valide" }));
  ctx.pannelloLogin = async () => { pannelli += 1; return true; };
  const r = await ctx.api("/api/auth/login", { method: "POST", body: {}, auth: false });
  assert.equal(pannelli, 0, "un login sbagliato aprirebbe un login dentro il login");
  assert.equal(r.error.kind, "sessione");
});

test("i toast si deduplicano per chiave e non superano il tetto", async () => {
  const { ctx, body } = contesto(async () => risposta(200, {}));
  const box = () => body.children[0];
  ctx.toast("uno", { chiave: "k", durata: 0 });
  ctx.toast("due", { chiave: "k", durata: 0 });
  assert.equal(box().children.length, 1, "stessa chiave: si aggiorna il toast, non se ne accumulano");
  for (let i = 0; i < 6; i++) ctx.toast("n" + i, { durata: 0 });
  assert.ok(box().children.length <= 4, "una pila infinita coprirebbe la pagina");
});
test("app.js non contiene piu' fetch sparse, alert di sistema o messaggi generici", () => {
  const testo = readFileSync(PERCORSO, "utf8");
  // Poche fetch, e si sa quali: `apiUnaVolta` (tutte le chiamate normali) piu'
  // le due che leggono un flusso mentre esce e quindi non possono passare da
  // apiUnaVolta, che il corpo lo legge tutto insieme — `eseguiInDiretta` per
  // l'output dei tool e `startLogTail` per il "segui" dei log. Il test
  // controlla la funzione che le contiene, non il numero: una fetch in piu'
  // scritta a mano da qualche altra parte resta un errore.
  const consentite = ["apiUnaVolta", "eseguiInDiretta", "startLogTail"];
  const contenitore = (pos) => {
    const prima = testo.slice(0, pos).match(/function\s+([A-Za-z0-9_]+)\s*\(/g) || [];
    const ultima = prima[prima.length - 1] || "";
    return (ultima.match(/function\s+([A-Za-z0-9_]+)/) || [])[1] || "?";
  };
  const dove = [...testo.matchAll(/\bfetch\(/g)].map(m => contenitore(m.index));
  assert.deepEqual(dove, consentite,
    `fetch trovate in: ${dove.join(", ")} — devono stare solo in ${consentite.join(" e ")}`);
  assert.equal((testo.match(/\balert\(/g) || []).length, 0,
    "gli alert di sistema bloccano la pagina e non si copiano");
  assert.equal((testo.match(/"Errore: "/g) || []).length, 0,
    "il messaggio generico nasconde la differenza fra rete, 500 e timeout");
});
