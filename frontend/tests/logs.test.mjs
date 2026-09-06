/* ===================================================================
   logs.test.mjs — Pagina Logs

   Il difetto grosso l'ha mostrato l'istanza vera: le ultime 300 righe
   del log del router erano 240 righe di `dropbear` e 60 di `crond`,
   cioe' le connessioni SSH che LANMng apre per leggere il router. Il
   monitoraggio si vede nel log che sta leggendo, e ci si annega dentro.

   Da li' tre difetti:
     1. il filtro cercava DENTRO le ultime N righe (il `tail` stava
        prima del `grep`): cercare "wireguard" tornava vuoto anche con
        il log pieno di quelle righe poco piu' indietro — misurato in
        produzione, 0 risultati su un log che ne ha;
     2. non c'era modo di nascondere il rumore, ne' la pagina diceva
        che quel rumore c'era;
     3. "Nessun log." era la stessa frase per "il log e' vuoto" e per
        "nessuna riga soddisfa i filtri", e nulla diceva quando era
        stata letta la pagina (che non si aggiorna da sola).
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES, logStato };"
  + "\n;globalThis.h = h;";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false, scrollTop: 0, scrollHeight: 0,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    children: [],
    setAttribute() {}, addEventListener() {}, appendChild(f) { return f; },
    append() {}, remove() {}, focus() {}, click() {}, closest: () => null,
    getContext: () => null, insertAdjacentHTML() {},
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

/* Backend finto che si comporta come quello vero: filtra su TUTTO il log e
   poi taglia, cosi' il test vede la differenza fra le due implementazioni. */
const SORGENTI_FINTE = [
  { id: "router", label: "Router (syslog)", sensitive: false, persisted: false },
  { id: "backend", label: "Backend LANMng", sensitive: true, persisted: true },
  { id: "audit", label: "Registro di audit", sensitive: true, persisted: true },
  { id: "journal:10.0.0.7", label: "journalctl \u00b7 10.0.0.7", sensitive: true },
];

function backendLog(tutte, sorgenti = SORGENTI_FINTE) {
  return (url) => {
    if (url.startsWith("/api/logs/sources")) return { sources: sorgenti };
    const q = new URL("http://x" + url).searchParams;
    const lines = Number(q.get("lines") || 500);
    const filtro = (q.get("filter") || "").toLowerCase();
    const escludi = (q.get("exclude") || "").toLowerCase();
    const livello = q.get("level") || "";
    let righe = tutte;
    if (filtro) righe = righe.filter(l => l.raw.toLowerCase().includes(filtro));
    for (const e of escludi.split(",").map(x => x.trim()).filter(Boolean))
      righe = righe.filter(l => !l.raw.toLowerCase().includes(e));
    righe = righe.slice(-lines);
    const livelli = { error: 0, warn: 0, info: 0, debug: 0 };
    righe.forEach(l => { livelli[l.level] = (livelli[l.level] || 0) + 1; });
    if (livello) righe = righe.filter(l => l.level === livello);
    const conteggio = {};
    righe.forEach(l => { if (l.src) conteggio[l.src] = (conteggio[l.src] || 0) + 1; });
    return { lines: righe, count: righe.length, levels: livelli,
             source: q.get("source") || "router",
             sources: Object.entries(conteggio).map(([src, count]) => ({ src, count }))
                            .sort((a, b) => b.count - a.count) };
  };
}

function contesto(rispondi, opzioni = {}) {
  const chiamate = [];
  const nodi = new Map();
  const creati = [];
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const creaElemento = () => { const e = elemento(); creati.push(e); return e; };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: creaElemento, createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    URL, URLSearchParams,
    fetch: opzioni.fetch || (async (url) => {
      chiamate.push(url);
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(rispondi(url)) };
    }),
    Blob: function (parti) { this.parti = parti; },
    URL: Object.assign(URL, { createObjectURL: () => "blob:finto", revokeObjectURL() {} }),
    confirm: opzioni.confirm || (() => false),
    TextDecoder,
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    // Timer veri: `api()` ritenta le GET con un'attesa, e con un setTimeout
    // finto il secondo tentativo non arriverebbe mai.
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate, creati, nodi };
}

/* Un log come quello vero: rumore SSH del monitoraggio, piu' qualche riga
   utile piu' indietro nel tempo. */
function logVero() {
  const utili = [
    { ts: "Thu Sep  3 19:02:11 2026", level: "warn", src: "dnsmasq", msg: "no address range available",
      raw: "Thu Sep  3 19:02:11 2026 daemon.warn dnsmasq[1234]: no address range available" },
    { ts: "Thu Sep  3 19:05:40 2026", level: "info", src: "wireguard", msg: "peer handshake ok",
      raw: "Thu Sep  3 19:05:40 2026 daemon.info wireguard: peer handshake ok" },
  ];
  const rumore = Array.from({ length: 300 }, (_, i) => ({
    ts: "Thu Sep  3 20:14:27 2026", level: "info", src: "dropbear",
    msg: `Child connection from 198.51.100.10:${39000 + i}`,
    raw: `Thu Sep  3 20:14:27 2026 authpriv.info dropbear[${i}]: Child connection from 198.51.100.10:${39000 + i}`,
  }));
  return [...utili, ...rumore];
}


// ── 1. Il filtro deve cercare in tutto il log ──────────────────────

test("cercare una parola la trova anche se sta oltre le ultime righe", async () => {
  const { ctx, dammi } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  dammi("#lfilter").value = "wireguard";
  await ctx.caricaLogs();
  assert.ok(dammi("#lbox").innerHTML.includes("peer handshake ok"),
    "col taglio prima del filtro questa riga era irraggiungibile");
});

test("il filtro viaggia come parametro, non come taglio locale", async () => {
  const { ctx, dammi, chiamate } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  dammi("#lfilter").value = "dnsmasq";
  dammi("#lexclude").value = "dropbear";
  dammi("#llines").value = "2000";
  await ctx.caricaLogs();
  const url = chiamate[chiamate.length - 1];
  assert.ok(url.includes("filter=dnsmasq") && url.includes("exclude=dropbear")
            && url.includes("lines=2000"), url);
});


// ── 2. Il rumore del monitoraggio ──────────────────────────────────

test("la pagina dice chi sta riempiendo il log e offre di nasconderlo", async () => {
  const { ctx, dammi } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  const nota = dammi("#lnota").innerHTML;
  assert.ok(nota.includes("dropbear"), "il rumore va dichiarato, non lasciato scoprire");
  assert.ok(/300 righe su 3\d\d/.test(nota) || nota.includes("300"), nota);
  assert.ok(nota.includes("Nascondi dropbear"));
});

test("il pulsante nasconde davvero, e la nota sparisce", async () => {
  const { ctx, dammi } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  await dammi("#lg-nascondi").onclick();
  assert.equal(dammi("#lexclude").value, "dropbear");
  assert.ok(!dammi("#lbox").innerHTML.includes("Child connection"), "il rumore e' sparito");
  assert.ok(dammi("#lbox").innerHTML.includes("no address range"), "e sotto c'era il log vero");
  assert.equal(dammi("#lnota").innerHTML, "", "nascosto il rumore, la nota non serve piu'");
});

test("senza una sorgente dominante non si propone niente", async () => {
  const misto = [
    { ts: "x", level: "info", src: "dnsmasq", msg: "a", raw: "a dnsmasq" },
    { ts: "x", level: "info", src: "kernel", msg: "b", raw: "b kernel" },
  ];
  const { ctx, dammi } = contesto(backendLog(misto));
  await ctx.pageLogs(elemento());
  assert.equal(dammi("#lnota").innerHTML, "");
});


// ── 3. Vuoti distinti e "di quando e' questa pagina" ───────────────

test("il vuoto distingue 'log vuoto' da 'nessuna riga con questi filtri'", async () => {
  const { ctx, dammi } = contesto(backendLog([]));
  await ctx.pageLogs(elemento());
  // Il messaggio nomina la sorgente: con quattro registri fra cui scegliere,
  // "il log e' vuoto" non direbbe quale.
  assert.match(dammi("#lbox").innerHTML, /nessuna riga/);

  dammi("#lfilter").value = "zzz-non-esiste";
  await ctx.caricaLogs();
  assert.match(dammi("#lbox").innerHTML, /Nessuna riga con questi filtri/);
});

test("la pagina dice quante righe sono e a che ora le ha lette", async () => {
  const { ctx, dammi } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  const stato = dammi("#lstato").textContent;
  assert.match(stato, /\d+ righe · letto alle \d\d:\d\d:\d\d/,
    "la pagina non si aggiorna da sola: senza l'ora si guardano righe di mezz'ora fa credendole di adesso");
});

test("se il router non risponde lo dice, con il riprova", async () => {
  const { ctx, dammi } = contesto(() => { throw new Error("giu'"); });
  const view = elemento();
  ctx.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await ctx.pageLogs(view);
  assert.ok(dammi("#lbox").innerHTML.includes("Riprova"), dammi("#lbox").innerHTML);
});


test("i rumori sono spesso due: il secondo si aggiunge al primo", async () => {
  // Sul router vero il buffer e' fatto di connessioni SSH del monitoraggio E
  // del cron che controlla il modem: nascondendone uno solo restavano 130
  // righe di cron e nessun evento vero.
  const doppio = [
    { ts: "x", level: "info", src: "dnsmasq", msg: "reply", raw: "dnsmasq reply from 1.1.1.1" },
    ...Array.from({ length: 60 }, () => ({ ts: "x", level: "info", src: "dropbear",
      msg: "Child connection", raw: "dropbear Child connection" })),
    ...Array.from({ length: 40 }, () => ({ ts: "x", level: "info", src: "crond",
      msg: "USER root pid 1 cmd modem.sh", raw: "crond USER root pid 1 cmd modem.sh" })),
  ];
  const { ctx, dammi } = contesto(backendLog(doppio));
  await ctx.pageLogs(elemento());
  await dammi("#lg-nascondi").onclick();          // via dropbear
  assert.equal(dammi("#lexclude").value, "dropbear");
  await dammi("#lg-nascondi").onclick();          // ora domina crond
  assert.equal(dammi("#lexclude").value, "dropbear, crond");
  assert.ok(dammi("#lbox").innerHTML.includes("reply"), "e resta il log vero");
});


/* ===================================================================
   Piu' sorgenti, conteggi, evidenziazione, segui, esporta
   =================================================================== */

// ── 4. Il selettore delle sorgenti viene dall'API ──────────────────

test("le sorgenti si chiedono al backend, non stanno scritte nella pagina", async () => {
  // Quali registri esistono dipende dalla configurazione (host SSH, audit
  // acceso o spento): un elenco fisso nel frontend sarebbe un nome di macchina
  // hardcoded, che e' proprio quello che il progetto vieta.
  const { ctx, dammi, chiamate } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  assert.ok(chiamate.some(u => u.startsWith("/api/logs/sources")),
    "la pagina non ha nemmeno chiesto l'elenco");
  const html = dammi("#lsource").innerHTML;
  assert.ok(html.includes("journal:10.0.0.7"), html);
  assert.ok(html.includes("Registro di audit"), html);
});

test("se l'elenco non arriva resta almeno il syslog", async () => {
  const { ctx, dammi } = contesto((url) =>
    url.startsWith("/api/logs/sources") ? {} : backendLog([])(url));
  await ctx.pageLogs(elemento());
  assert.ok(dammi("#lsource").innerHTML.includes("router"),
    "una pagina senza selettore sarebbe peggio di una con una voce sola");
});

test("la sorgente scelta viaggia come parametro", async () => {
  const { ctx, dammi, chiamate } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  dammi("#lsource").value = "audit";
  await dammi("#lsource").onchange();
  assert.ok(chiamate[chiamate.length - 1].includes("source=audit"),
    chiamate[chiamate.length - 1]);
});

test("l'intervallo temporale viaggia come parametro", async () => {
  const { ctx, dammi, chiamate } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  dammi("#lperiod").value = "1h";
  await ctx.caricaLogs();
  assert.ok(chiamate[chiamate.length - 1].includes("period=1h"));
});


// ── 5. Conteggi per livello ────────────────────────────────────────

test("gli errori si contano anche mentre si guardano gli info", async () => {
  // Contarli dopo il filtro darebbe sempre "0 errori" guardando gli info, che
  // e' il modo migliore per non accorgersi di un guasto.
  const misto = [
    { ts: "x", level: "error", src: "kernel", msg: "I/O error", raw: "kernel I/O error" },
    { ts: "x", level: "info", src: "dnsmasq", msg: "reply", raw: "dnsmasq reply" },
    { ts: "x", level: "info", src: "dnsmasq", msg: "reply due", raw: "dnsmasq reply due" },
  ];
  const { ctx, dammi } = contesto(backendLog(misto));
  await ctx.pageLogs(elemento());
  dammi("#llevel").value = "info";
  await ctx.caricaLogs();
  const chip = dammi("#llivelli").innerHTML;
  assert.ok(chip.includes("1 errore"), chip);
  assert.ok(!dammi("#lbox").innerHTML.includes("I/O error"),
    "l'errore e' contato ma non mostrato: si sta guardando il livello info");
});

test("il conteggio e' un pulsante che filtra", async () => {
  const misto = [
    { ts: "x", level: "error", src: "kernel", msg: "I/O error", raw: "kernel I/O error" },
    { ts: "x", level: "info", src: "dnsmasq", msg: "reply", raw: "dnsmasq reply" },
  ];
  // querySelectorAll deve restituire i pulsanti veri: il DOM finto di base
  // ritorna una lista vuota, quindi qui si arma il nodo dei contatori.
  const { ctx, dammi } = contesto(backendLog(misto));
  const bottoni = [];
  dammi("#llivelli").querySelectorAll = () => bottoni;
  await ctx.pageLogs(elemento());
  bottoni.push({ dataset: { livello: "error" }, set onclick(f) { this._f = f; },
                 get onclick() { return this._f; } });
  ctx.bindContatoriLivelli();
  await bottoni[0].onclick();
  assert.equal(dammi("#llevel").value, "error");
});


// ── 6. Evidenziazione: non deve aprire un'iniezione ────────────────

test("il termine cercato si evidenzia nella riga", async () => {
  const righe = [{ ts: "x", level: "info", src: "dnsmasq", msg: "no address range",
                   raw: "dnsmasq no address range" }];
  const { ctx, dammi } = contesto(backendLog(righe));
  await ctx.pageLogs(elemento());
  dammi("#lfilter").value = "address";
  await ctx.caricaLogs();
  assert.ok(dammi("#lbox").innerHTML.includes("<mark>address</mark>"),
    dammi("#lbox").innerHTML);
});

test("evidenziare non lascia passare HTML dal log", () => {
  // Una riga di log e' testo che arriva da fuori (un hostname, un user agent):
  // se l'evidenziazione si applicasse al grezzo, bastera' un <script> nel log
  // per eseguirlo nella dashboard.
  const { ctx } = contesto(backendLog([]));
  const uscita = ctx.evidenzia(ctx.h('<img src=x onerror=alert(1)> ciao'), "img");
  assert.ok(!uscita.includes("<img"), uscita);
  assert.ok(uscita.includes("<mark>img</mark>"), uscita);
});

test("una riga di log che contiene HTML non lo esegue in pagina", async () => {
  // Il caso vero: il messaggio arriva da fuori (un hostname, uno user agent,
  // il nome di un file). Se l'evidenziazione lavorasse sul testo grezzo,
  // basterebbe scriverlo nel log per farlo eseguire nella dashboard.
  const cattiva = [{ ts: "x", level: "info", src: "nginx",
                     msg: '<img src=x onerror=alert(1)> GET /img',
                     raw: 'nginx <img src=x onerror=alert(1)> GET /img' }];
  const { ctx, dammi } = contesto(backendLog(cattiva));
  await ctx.pageLogs(elemento());
  dammi("#lfilter").value = "img";
  await ctx.caricaLogs();
  const html = dammi("#lbox").innerHTML;
  assert.ok(!html.includes("<img"), html);
  // Il termine evidenziato spezza la stringa (`&lt;` + `<mark>img</mark>`),
  // quindi si cerca l'escape, non "&lt;img" tutto attaccato.
  assert.ok(html.includes("&lt;"), "il tag si deve vedere scritto, non eseguire");
  assert.ok(html.includes("<mark>img</mark>"), "e l'evidenziazione deve comunque funzionare");
});

test("cercare un carattere speciale trova comunque qualcosa", () => {
  // Nel testo escapato "&" e' diventato "&amp;": confrontare il termine grezzo
  // non lo troverebbe mai.
  const { ctx } = contesto(backendLog([]));
  assert.ok(ctx.evidenzia(ctx.h("a & b"), "&").includes("<mark>&amp;</mark>"));
});

test("senza termine la riga resta intatta", () => {
  const { ctx } = contesto(backendLog([]));
  assert.equal(ctx.evidenzia("testo semplice", ""), "testo semplice");
});


// ── 7. Segui in tempo reale (NDJSON) ───────────────────────────────

function flussoNdjson(righe, { attendi } = {}) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true, status: 200,
    headers: { get: () => "application/x-ndjson" },
    body: {
      getReader: () => ({
        async read() {
          if (i >= righe.length) {
            if (attendi) await new Promise(r => setTimeout(r, 50));
            return { done: true, value: undefined };
          }
          return { done: false, value: enc.encode(JSON.stringify(righe[i++]) + "\n") };
        },
      }),
    },
  };
}

test("il segui accoda le righe senza rifare la pagina", async () => {
  const base = [{ ts: "x", level: "info", src: "a", msg: "prima", raw: "a prima" }];
  let quanteChiamate = 0;
  const { ctx, dammi } = contesto(backendLog(base), {
    fetch: async (url) => {
      quanteChiamate++;
      if (url.startsWith("/api/logs/stream"))
        return flussoNdjson([
          { type: "start", source: "backend", interval: 5 },
          { type: "line", line: { ts: "y", level: "error", src: "b", msg: "arrivata dopo",
                                  raw: "b arrivata dopo" } },
          { type: "end" },
        ]);
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(backendLog(base)(url)) };
    },
  });
  await ctx.pageLogs(elemento());
  const box = dammi("#lbox");
  // Nel DOM finto querySelector inventa sempre un nodo: qui deve dire la
  // verita', altrimenti la pagina crede di avere davanti il "caricamento…" e
  // lo cancella, cioe' proprio la cosa che il test vuole misurare.
  box.querySelector = () => null;
  let riscritture = 0;
  Object.defineProperty(box, "innerHTML", {
    get() { return this._h || ""; },
    set(v) { riscritture++; this._h = v; },
  });
  box.innerHTML = "<div class=\"l\">prima</div>";
  riscritture = 0;
  box.insertAdjacentHTML = (dove, html) => { box._h += html; };
  await ctx.startLogTail();
  assert.ok(box.innerHTML.includes("arrivata dopo"), box.innerHTML);
  assert.ok(box.innerHTML.includes("prima"),
    "un render intero avrebbe azzerato quello che c'era e lo scorrimento");
  assert.equal(riscritture, 0, "la riga si accoda, non si ridisegna la pagina");
});

test("fermare il segui chiude davvero il flusso", async () => {
  let abortata = false;
  const { ctx, dammi } = contesto(backendLog([]), {
    fetch: async (url, init) => {
      if (url.startsWith("/api/logs/stream")) {
        if (init && init.signal) init.signal.addEventListener("abort", () => { abortata = true; });
        return flussoNdjson([{ type: "start" }], { attendi: true });
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(backendLog([])(url)) };
    },
  });
  await ctx.pageLogs(elemento());
  const giro = ctx.startLogTail();
  ctx.stopLogTail();
  await giro;
  assert.ok(abortata, "senza abort il flusso resta aperto e il router continua a essere interrogato");
  assert.match(dammi("#lfollow").innerHTML, /Segui/, "il pulsante torna a proporre di seguire");
});

test("uscire dalla pagina ferma il segui", async () => {
  const { ctx, dammi } = contesto(backendLog([]), {
    fetch: async (url, init) => {
      if (url.startsWith("/api/logs/stream")) return flussoNdjson([{ type: "start" }],
                                                                 { attendi: true });
      return { ok: true, status: 200, headers: { get: () => "application/json" },
               text: async () => JSON.stringify(backendLog([])(url)) };
    },
  });
  await ctx.pageLogs(elemento());
  const giro = ctx.startLogTail();
  // La dashboard vera disegna su canvas, che il DOM finto non ha: qui interessa
  // solo che uscire dalla pagina stacchi il flusso.
  ctx.__interni.PAGES.dashboard.render = () => {};
  ctx.go("dashboard");
  await giro;
  assert.match(dammi("#lfollow").innerHTML, /Segui/,
    "un flusso lasciato aperto tiene occupata una connessione per una pagina che nessuno guarda");
});


// ── 8. Esportazione ────────────────────────────────────────────────

test("si esporta quello che si sta guardando, in testo", async () => {
  const righe = [
    { ts: "x", level: "warn", src: "dnsmasq", msg: "no address range",
      raw: "x daemon.warn dnsmasq: no address range" },
  ];
  const { ctx, dammi, creati } = contesto(backendLog(righe), { confirm: () => false });
  await ctx.pageLogs(elemento());
  ctx.esportaLogs();
  const a = creati[creati.length - 1];
  assert.ok(a.download.endsWith(".txt"), a.download);
  assert.ok(a.download.includes("router"), "il nome del file dice da quale registro viene");
});

test("in JSON si esportano i campi, non solo il testo", async () => {
  const righe = [{ ts: "x", level: "error", src: "kernel", msg: "I/O error",
                   raw: "kernel I/O error" }];
  const { ctx, creati } = contesto(backendLog(righe), { confirm: () => true });
  await ctx.pageLogs(elemento());
  ctx.esportaLogs();
  const a = creati[creati.length - 1];
  assert.ok(a.download.endsWith(".json"), a.download);
});

test("con il log vuoto non si scarica un file vuoto", async () => {
  const { ctx, creati } = contesto(backendLog([]));
  await ctx.pageLogs(elemento());
  ctx.esportaLogs();
  // Non si contano gli elementi creati: il toast che avvisa ne crea uno. Si
  // guarda se e' stato preparato un download, che e' la cosa da non fare.
  assert.ok(!creati.some(e => e.download),
    "meglio avvisare che consegnare un file di zero righe");
});


// ── 10. Le regole di scarto si dichiarano ──────────────────────────
//
// `logs.exclude` toglie righe prima che arrivino in pagina. Se lo facesse in
// silenzio, cercare una riga che "dovrebbe esserci" diventerebbe una caccia
// senza indizi: la pagina dice quante regole stanno agendo e dove si cambiano.

test("con regole di scarto attive la pagina lo dichiara", async () => {
  const risposta = (url) => Object.assign(backendLog(logVero())(url), { filtri: 2 });
  const { ctx, dammi } = contesto(risposta);
  await ctx.pageLogs(elemento());
  await ctx.caricaLogs();
  const nota = dammi("#lnota").innerHTML;
  assert.ok(/2 regole di scarto attive/.test(nota), nota);
  assert.ok(nota.includes("logs.exclude"), "va detto dove si cambiano");
});

test("senza regole di scarto non compare nessuna nota in piu'", async () => {
  const { ctx, dammi } = contesto(backendLog(logVero()));
  await ctx.pageLogs(elemento());
  await ctx.caricaLogs();
  assert.ok(!dammi("#lnota").innerHTML.includes("regole di scarto"));
});

test("una regola sola si dice al singolare", async () => {
  const risposta = (url) => Object.assign(backendLog(logVero())(url), { filtri: 1 });
  const { ctx, dammi } = contesto(risposta);
  await ctx.pageLogs(elemento());
  await ctx.caricaLogs();
  assert.ok(dammi("#lnota").innerHTML.includes("1 regola di scarto attiva"));
});
