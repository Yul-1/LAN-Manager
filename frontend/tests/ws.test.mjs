/* ===================================================================
   ws.test.mjs — connessione live e onesta' degli indicatori

   Prima la riconnessione era a intervallo fisso di 2s senza tetto: col
   backend spento erano trenta tentativi al minuto per ore. E l'orario
   accanto all'indicatore era quello del browser al momento dell'ultimo
   messaggio: restava fermo su un'ora plausibile e faceva sembrare fresco
   un dato di mezz'ora prima.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, WS_BACKOFF };";

/* Contesto 2D finto: l'arrivo di uno snapshot ridisegna la pagina attiva, che
   sulla dashboard disegna i grafici. Con un getContext nullo il test
   fallirebbe dentro il canvas invece che sull'oggetto in esame. */
function ctx2d() {
  const noop = () => {};
  // createLinearGradient deve tornare un oggetto vero: il grafico ci chiama
  // addColorStop sopra per l'area sotto la linea.
  const grad = { addColorStop: noop };
  return new Proxy({}, {
    get: (_t, k) => {
      if (k === "canvas") return { width: 600, height: 200 };
      if (k === "createLinearGradient") return () => grad;
      return noop;
    },
    set: () => true,
  });
}

function elemento() {
  const figli = [];
  return {
    className: "", innerHTML: "", textContent: "", style: {}, dataset: {},
    width: 600, height: 200, children: figli,
    setAttribute() {}, appendChild(f) { figli.push(f); return f; }, remove() {},
    querySelector: () => elemento(), querySelectorAll: () => [],
    addEventListener() {}, focus() {},
    getContext: () => ctx2d(),
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
}

/* WebSocket finto: registra le istanze e lascia pilotare open/close/message.
   Lo stub vuoto usato altrove renderebbe cieco proprio il test. */
function contesto() {
  const sockets = [];
  const ritardi = [];
  const nodi = { "#conn": elemento(), "#updated": elemento() };
  function FintoWS() {
    this.readyState = 0;
    this.onopen = this.onclose = this.onmessage = this.onerror = null;
    this.close = () => { this.readyState = 3; if (this.onclose) this.onclose({ reason: "" }); };
    this.send = () => {};
    sockets.push(this);
  }
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1 },
    document: {
      body: elemento(),
      createElement: () => elemento(),
      querySelector: (sel) => nodi[sel] || elemento(),
      querySelectorAll: () => [],
      addEventListener() {},
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, text: async () => "{}", headers: { get: () => null } }),
    AbortController,
    // I ritardi sono l'oggetto in esame: si registrano invece di attendere.
    // Solo quelli della riconnessione: la diagnosi della caduta chiama l'API,
    // e il suo budget di tempo e' un altro setTimeout che qui non c'entra.
    setTimeout: (fn, ms) => { if (fn === ctx.connectWS) ritardi.push(ms); return 0; },
    clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object,
    WebSocket: FintoWS,
  });
  runInContext(SORGENTE, ctx);
  return { ctx, sockets, ritardi, nodi };
}


test("la riconnessione rallenta invece di martellare ogni due secondi", () => {
  const { ctx, sockets, ritardi } = contesto();
  ctx.connectWS();
  // Sei cadute di fila: i ritardi devono salire e poi fermarsi al tetto.
  for (let i = 0; i < 7; i++) sockets[sockets.length - 1].onclose({ reason: "" });
  const attesi = ctx.__interni.WS_BACKOFF;
  assert.equal(ritardi.length, 7);
  ritardi.forEach((ms, i) => {
    const base = attesi[Math.min(i, attesi.length - 1)];
    // Jitter +/-25%: si verifica l'intervallo, non il valore esatto.
    assert.ok(ms >= base * 0.7 && ms <= base * 1.35,
      `tentativo ${i}: ${ms}ms fuori dall'intervallo atteso attorno a ${base}ms`);
  });
});

test("il backoff riparte solo quando arrivano dati veri, non all'apertura", () => {
  const { ctx, sockets, ritardi } = contesto();
  ctx.connectWS();
  const ws = sockets[0];
  ws.onclose({ reason: "" });
  ws.onclose({ reason: "" });
  const primaDeiDati = ritardi.length;
  // Una connessione che si apre e muore subito non deve riazzerare il conto:
  // altrimenti il backoff resta a 1s per sempre.
  ws.onopen();
  ws.onclose({ reason: "" });
  assert.ok(ritardi[primaDeiDati] > ritardi[0],
    "aprire e basta non prova che la connessione regga");

  ws.onmessage({ data: JSON.stringify({ type: "snapshot", data: {} }) });
  ws.onclose({ reason: "" });
  const base = ctx.__interni.WS_BACKOFF[0];
  assert.ok(ritardi[ritardi.length - 1] <= base * 1.35,
    "dopo dati veri il conto riparte da capo");
});

test("una chiusura per sessione scaduta chiede il login invece di ritentare", () => {
  const { ctx, sockets, ritardi } = contesto();
  let chieste = 0;
  ctx.chiediLogin = () => { chieste += 1; return Promise.resolve(false); };
  ctx.connectWS();
  sockets[0].onclose({ reason: "sessione" });
  assert.equal(chieste, 1);
  assert.equal(ritardi.length, 0, "ritentare con la sessione scaduta non riapre mai lo stream");
});

test("una chiusura per origine non consentita smette di ritentare", () => {
  const { ctx, sockets, ritardi } = contesto();
  ctx.connectWS();
  sockets[0].onclose({ reason: "origine" });
  assert.equal(ritardi.length, 0, "e' una configurazione sbagliata: insistere non serve");
});

test("etaDato misura da quanto il dato e' fermo, non che ora era", () => {
  const { ctx } = contesto();
  const st = ctx.__interni.state;
  assert.equal(ctx.etaDato().testo, "—", "senza dati non si inventa un orario");

  st.datoRicevutoA = Date.now() - 5000;
  assert.equal(ctx.etaDato().fermo, false);
  assert.match(ctx.etaDato().testo, /^agg\. \d+s fa$/);

  // Oltre tre giri di raccolta il dato e' vecchio e va detto: prima l'orario
  // restava quello dell'ultimo messaggio e sembrava fresco.
  st.snap = { collector: { fast_interval: 10 } };
  st.datoRicevutoA = Date.now() - 90000;
  const eta = ctx.etaDato();
  assert.equal(eta.fermo, true);
  assert.match(eta.testo, /fermo da 1m/);
});

test("l'indicatore distingue live, riconnessione e offline", () => {
  const { ctx, sockets } = contesto();
  ctx.connectWS();
  const ws = sockets[0];
  ws.readyState = 1;
  ws.onmessage({ data: JSON.stringify({ type: "snapshot", data: {} }) });
  assert.equal(ctx.statoConnessione().testo, "live");

  ws.readyState = 3;
  ws.onclose({ reason: "" });
  assert.match(ctx.statoConnessione().testo, /riconnessione fra \d+s/);
});


test("l'avviso di sorgente si aggiorna anche sulle pagine a refresh parziale", () => {
  // Dispositivi e Risorse non rifanno l'HTML completo ad ogni update: senza un
  // aggiornamento apposito l'avviso resterebbe quello di quando si e' entrati,
  // cioe' proprio il caso da segnalare non si vedrebbe mai.
  const { ctx } = contesto();
  const note = [];
  ctx.document.querySelectorAll = (sel) => (sel === ".src-nota" ? note : []);

  const nota = elemento();
  note.push(nota);
  ctx.__interni.state.snap = {
    sources: { devices: { ok: false, error: "router irraggiungibile", since: 1, ts: 2 } },
  };
  ctx.aggiornaNoteSorgente(["devices"]);
  assert.match(nota.innerHTML, /router irraggiungibile/);

  ctx.__interni.state.snap.sources.devices = { ok: true, ts: 3 };
  ctx.aggiornaNoteSorgente(["devices"]);
  assert.equal(nota.innerHTML, "", "tornata su, l'avviso deve sparire da solo");
});


// ── Rifiuto muto dell'handshake ────────────────────────────────────
//  Il server chiude PRIMA di accettare (origine o sessione, vedi main.py):
//  il browser riceve un 1006 senza codice ne' testo, quindi i rami che
//  leggono `ev.reason` non scattano mai. La dashboard restava a
//  "riconnessione fra Ns" per ore, coi dati fermi e nessuno che lo dicesse.

function contestoDiagnosi(statoAuth) {
  const chiamate = [];
  const base = contesto();
  base.ctx.fetch = async (url) => {
    chiamate.push(url);
    return { ok: true, status: 200, headers: { get: () => "application/json" },
             text: async () => JSON.stringify(statoAuth) };
  };
  return { ...base, chiamate };
}

test("una caduta senza motivo fa chiedere all'API cosa sta succedendo", async () => {
  const { ctx, sockets, chiamate } = contestoDiagnosi({ session: true, password_set: true });
  ctx.connectWS();
  sockets[0].onclose({ reason: "" });
  sockets[sockets.length - 1].onclose({ reason: "" });
  await new Promise(r => setTimeout(r, 10));
  assert.ok(chiamate.some(u => u.includes("/api/auth/status")),
    "senza chiedere, un rifiuto in handshake resta indistinguibile da un backend spento");
});

test("con la sessione scaduta si chiede la password invece di ritentare all'infinito", async () => {
  const { ctx, sockets, chiamate } = contestoDiagnosi({ session: false, password_set: true });
  let pannelli = 0;
  const avvisi = [];
  ctx.toast = (testo, opt) => avvisi.push({ testo, opt });
  ctx.chiediLogin = async () => { pannelli += 1; return false; };
  ctx.connectWS();
  sockets[0].onclose({ reason: "" });
  sockets[sockets.length - 1].onclose({ reason: "" });
  await new Promise(r => setTimeout(r, 10));
  assert.equal(pannelli, 1);
  // Rinunciando al login lo stream resta fermo: dirlo, con il modo di rientrare.
  assert.equal(avvisi[0].opt.chiave, "ws-sessione");
});

test("rientrando, lo stream riparte da solo", async () => {
  const { ctx, sockets } = contestoDiagnosi({ session: false, password_set: true });
  ctx.chiediLogin = async () => true;
  ctx.connectWS();
  const prima = sockets.length;
  sockets[0].onclose({ reason: "" });
  sockets[sockets.length - 1].onclose({ reason: "" });
  await new Promise(r => setTimeout(r, 10));
  assert.ok(sockets.length > prima, "dopo il login si riapre la connessione");
});

test("con sessione valida e API viva, il sospetto e' il percorso /ws", async () => {
  const { ctx, sockets } = contestoDiagnosi({ session: true, password_set: true });
  const avvisi = [];
  ctx.toast = (testo, opt) => avvisi.push({ testo, opt });
  ctx.connectWS();
  sockets[0].onclose({ reason: "" });
  sockets[sockets.length - 1].onclose({ reason: "" });
  await new Promise(r => setTimeout(r, 10));
  assert.equal(avvisi.length, 1);
  assert.match(avvisi[0].testo, /proxy inoltri \/ws/);
  assert.equal(avvisi[0].opt.chiave, "ws-percorso");
  assert.equal(typeof avvisi[0].opt.azione.onclick, "function", "e si puo' riprovare");
});

test("si chiede una volta sola, non ad ogni tentativo", async () => {
  const { ctx, sockets, chiamate } = contestoDiagnosi({ session: true, password_set: true });
  ctx.connectWS();
  for (let i = 0; i < 5; i++) sockets[sockets.length - 1].onclose({ reason: "" });
  await new Promise(r => setTimeout(r, 10));
  assert.equal(chiamate.filter(u => u.includes("/api/auth/status")).length, 1,
    "cinque cadute non devono diventare cinque interrogazioni");
});
