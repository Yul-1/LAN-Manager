/* ===================================================================
   dashboard.test.mjs — la pagina Dashboard

   Il proprietario l'ha giudicata "un po' inutile": la scritta ONLINE al
   posto di un pallino, il traffico illeggibile, il router che non diceva
   di quando fossero i suoi numeri, e servizi in evidenza che non si
   potevano ne' capire ne' cliccare.

   Sotto c'era un difetto tecnico mai notato: `kpi()` passava da sempre
   "green"/"orange"/"red", ma in styles.css non esisteva alcun selettore
   che li raccogliesse. Il test 2 e' li' apposta perche' non torni.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES };";
const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");

/* Contesto 2D che registra i colori di linea: serve a provare che una serie
   non viene piu' disegnata, cosa che dall'HTML non si vedrebbe. */
function contesto2D(colori) {
  // createLinearGradient deve tornare un oggetto vero: l'area sotto la linea
  // ci chiama addColorStop sopra.
  const grad = { addColorStop(_p, c) { colori.push("grad:" + c); } };
  return new Proxy({}, {
    get: (_t, k) => {
      if (k === "measureText") return () => ({ width: 8 });
      if (k === "createLinearGradient") return () => grad;
      return () => {};
    },
    set: (_t, k, v) => { if (k === "strokeStyle") colori.push(v); return true; },
  });
}

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", value: "", checked: false, title: "",
    hidden: false, style: {}, dataset: {}, scritture: 0,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    colori: [],
    set innerHTML(v) { this._html = v; this.scritture += 1; },
    get innerHTML() { return this._html || ""; },
    // Memoizzato: il test deve ritrovare lo stesso nodo che il codice ha scritto.
    querySelector(sel) {
      if (!memo.has(sel)) memo.set(sel, elemento());
      return memo.get(sel);
    },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, removeEventListener() {},
    appendChild(f) { return f; }, remove() {}, focus() {}, closest: () => null,
    clientWidth: 600, clientHeight: 180, width: 0, height: 0,
    getContext() { return contesto2D(el.colori); },
    getBoundingClientRect: () => ({ width: 600, height: 180, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto() {
  const nodi = new Map();
  const dammi = (sel) => {
    if (!nodi.has(sel)) nodi.set(sel, elemento());
    return nodi.get(sel);
  };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: {
      body: elemento(),
      createElement: () => elemento(),
      querySelector: dammi,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, text: async () => "{}",
                          headers: { get: () => "application/json" } }),
    AbortController,
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, nodi, dammi };
}

/* Snapshot minimo ma realistico: nomi e indirizzi documentativi, mai quelli
   veri (stessa regola della suite Python). */
function snap(over = {}) {
  return Object.assign({
    devices_summary: { total: 12, online: 9, offline: 3, ts: 1000 },
    services: { summary: { total: 5, ok: 5, down: 0 },
                docker: { containers: [], hosts: [], summary: {} },
                systemd: [], healthchecks: [] },
    wireguard: { total_peers: 4, active_peers: 2 },
    system: { hostname: "rt", model: "M", os_version: "OS", uptime_human: "3d",
              load: [0.1, 0.2, 0.3], memory_used_pct: 40, memory_total_mb: 256 },
    interfaces: [{ name: "wan", ifname: "eth0", up: true, rx_mb: 100, tx_mb: 20 }],
    wan_interface: "wan",
    traffic_series: [{ t: 1000, rx_bps: null, tx_bps: null, latency: 0 },
                     { t: 11000, rx_bps: 2000000, tx_bps: 300000, latency: 18 }],
    collector: { fast_interval: 10 },
    sources: { system: { ok: true, ts: Math.floor(Date.now() / 1000) - 25 } },
    alerts: [], alerts_summary: {},
  }, over);
}

/* Attenzione ai nomi: un container Docker NON ha `ok` (ce l'hanno systemd e gli
   healthcheck), ha `running`. Questo helper riproduce la forma vera dei
   `to_dict()` del backend, perche' un dato di test piu' generoso del vero
   nasconderebbe proprio il difetto che il test dovrebbe trovare. */
const svc = (over = {}) => {
  const base = { name: "uno", label: "Uno", dashboard: true, host: "user" };
  const kind = over.kind || "docker";
  if (kind === "docker") return Object.assign(base, { kind, running: true, url: "", status: "Up 3 days" }, over);
  if (kind === "systemd") return Object.assign(base, { kind, ok: true, available: true, active_state: "active" }, over);
  return Object.assign(base, { kind, ok: true, type: "tcp", target: "192.0.2.1:53", latency_ms: 4 }, over);
};


// ── 1. La scritta ONLINE ───────────────────────────────────────────

test("la WAN si annuncia con un pallino, non con la scritta ONLINE", () => {
  const { ctx } = contesto();
  const su = ctx.dashKpiWan({ name: "wan", ifname: "eth0", up: true, rx_mb: 1, tx_mb: 1 });
  assert.ok(!su.includes("ONLINE"), "la scritta ONLINE non deve piu' esistere");
  assert.ok(su.includes("status-dot s-on"), "serve il pallino verde");
  assert.ok(su.includes("connessa"), "stessa parola della pagina WAN");

  const giu = ctx.dashKpiWan({ name: "wan", ifname: "eth0", up: false, rx_mb: 1, tx_mb: 1 });
  assert.ok(giu.includes("status-dot s-down"));
  assert.ok(giu.includes("val red"), "giu' deve essere rosso, non grigio come tutto il resto");
});


// ── 2. Il difetto sotto tutto: i colori non esistevano ─────────────

test("i colori delle KPI hanno davvero una regola CSS", () => {
  // kpi() passa queste classi da sempre e nessuna era definita: ogni numero
  // rendeva grigio, su o giu' che fosse. Vale anche per Servizi e WireGuard.
  for (const c of ["green", "orange", "red"])
    assert.match(CSS, new RegExp(`\\.kpi\\s+\\.val\\.${c}\\s*\\{`),
      `manca la regola .kpi .val.${c}: il colore tornerebbe invisibile`);
});

test("le caselle di spunta nelle modali non sono stirate a tutta larghezza", () => {
  // `.modal label input` impone width:100% ed e' pensata per i campi di testo.
  assert.match(CSS, /\.modal label input\[type=checkbox\]/);
});


// ── 3-8. Servizi in evidenza ───────────────────────────────────────

test("in dashboard finiscono solo i servizi con la spunta, di ogni famiglia", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [
      svc({ kind: "systemd", name: "a.service", label: "Scelta" }),
      svc({ kind: "systemd", name: "b.service", label: "Esclusa", dashboard: false }),
    ], healthchecks: [
      // Gli healthcheck non hanno `label` nel backend: si mostrano per nome.
      svc({ kind: "healthcheck", name: "Check", label: "" }),
    ], docker: { containers: [
      svc({ name: "cont", label: "Container" }),
      svc({ name: "muto", label: "Muto", dashboard: false }),
    ] } } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(html.includes("Scelta") && html.includes("Container") && html.includes("Check"));
  assert.ok(!html.includes("Esclusa"), "una unit senza spunta non deve comparire");
  assert.ok(!html.includes("Muto"), "un container senza spunta non deve comparire");
});

test("un servizio con URL e' un link, uno senza resta testo", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [
      svc({ kind: "systemd", name: "a.service", label: "SenzaUrl" })],
    healthchecks: [], docker: { containers: [
      svc({ name: "cont", label: "ConUrl", url: "http://192.0.2.9:9000" })] } } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.match(html, /<a href="http:\/\/192\.0\.2\.9:9000" target="_blank" rel="noopener"/);
  assert.ok(!/SenzaUrl<\/a>/.test(html), "systemd non ha un URL da aprire");
});

test("un URL ostile non diventa mai un link", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [], healthchecks: [],
    docker: { containers: [svc({ label: "Cattivo", url: "javascript:alert(1)" })] } } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(!html.includes("javascript:"), "safeHref deve scartare lo schema");
  assert.ok(html.includes("Cattivo"), "ma il servizio resta in elenco");
});

test("ogni riga porta alla pagina Monitoraggio", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [], healthchecks: [],
    docker: { containers: [svc({ label: "Uno" })] } } });
  ctx.__interni.state.snap = s;
  assert.ok(ctx.servicesMini(s).includes('data-vai="services"'));
});

test("i servizi che non vanno stanno in cima", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [], healthchecks: [],
    docker: { containers: [
      svc({ name: "aaa", label: "Aaa", running: true }),
      svc({ name: "zzz", label: "Zzz", running: false }),
    ] } } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(html.indexOf("Zzz") < html.indexOf("Aaa"),
    "chi e' caduto deve leggersi per primo, anche se in ordine alfabetico verrebbe dopo");
});

test("un container acceso ha il pallino verde", () => {
  // Regressione trovata in revisione: `servicesMini` leggeva `x.ok` su tutte e
  // tre le famiglie, ma ContainerInfo.to_dict() espone `running`, non `ok`.
  // Ogni container acceso finiva col pallino rosso, un "giu' da" sotto e la
  // riga spinta in cima come se fosse il guasto da guardare.
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, systemd: [], healthchecks: [],
    docker: { containers: [svc({ label: "Acceso", running: true })] } } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(html.includes("status-dot s-on"), "un container running non e' giu'");
  assert.ok(!html.includes("s-down"));
});

test("i container non scavalcano i servizi davvero caduti", () => {
  const { ctx } = contesto();
  const s = snap({ services: { summary: {}, healthchecks: [], docker: { containers: [
      svc({ name: "aaa", label: "Aaa", running: true })] },
    systemd: [svc({ kind: "systemd", name: "z.service", label: "Zeta",
                    ok: false, active_state: "failed" })] } });
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(html.indexOf("Zeta") < html.indexOf("Aaa"),
    "l'unit caduta viene prima del container acceso");
});

test("lo stato si legge in italiano, non nel gergo del motore", () => {
  const { ctx } = contesto();
  assert.equal(ctx.statoLeggibile({ kind: "docker", running: true }), "attivo");
  assert.equal(ctx.statoLeggibile({ kind: "docker", running: false }), "fermo");
  assert.equal(ctx.statoLeggibile({ kind: "systemd", available: true, active_state: "active" }), "attivo");
  assert.equal(ctx.statoLeggibile({ kind: "systemd", available: true, active_state: "failed" }), "in errore");
  assert.equal(ctx.statoLeggibile({ kind: "systemd", available: false }), "sconosciuto");
  assert.equal(ctx.statoLeggibile({ kind: "healthcheck", ok: true, latency_ms: 12.4 }), "12 ms");
});

test("senza servizi scelti la card dice cosa fare", () => {
  const { ctx } = contesto();
  const s = snap();
  ctx.__interni.state.snap = s;
  const html = ctx.servicesMini(s);
  assert.ok(html.includes("mostra in dashboard"), "deve indicare la spunta, non un trattino");
  assert.ok(html.includes('data-vai="services"'), "e portare dove si imposta");
});


// ── 9. Eta' del dato ───────────────────────────────────────────────

test("la card del router dichiara di quando e' il dato", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = snap();
  const testa = ctx.dashRouterTesta();
  assert.match(testa, /dato di \d+s fa/,
    "uptime e RAM del router possono avere un minuto: senza l'eta' sono una fotografia");
});

test("senza un'ora di raccolta non si inventa un'eta'", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = snap({ sources: {} });
  assert.equal(ctx.etaSorgente("system"), "");
});


// ── 10. Traffico ───────────────────────────────────────────────────

test("il grafico del traffico non disegna piu' la latenza", () => {
  const { ctx } = contesto();
  const tela = elemento();
  ctx.drawTrafficChart(tela, snap().traffic_series);
  assert.ok(tela.colori.includes("#4dd6e0"), "RX deve esserci");
  assert.ok(tela.colori.includes("#5bd97f"), "TX deve esserci");
  assert.ok(!tela.colori.includes("#e6a94d"),
    "la latenza e' uscita dal grafico: costava un secondo asse per un numero");
});

test("la finestra del traffico e' derivata dai dati, non scritta a mano", () => {
  const { ctx } = contesto();
  const punti = Array.from({ length: 121 }, (_, i) => ({ t: i * 1000, rx_bps: 1 }));
  assert.equal(ctx.finestraTraffico({ traffic_series: punti, collector: { fast_interval: 10 } }),
    "ultimi ~20 min");
  // Cambiando l'intervallo in config la frase deve restare vera.
  assert.equal(ctx.finestraTraffico({ traffic_series: punti, collector: { fast_interval: 30 } }),
    "ultimi ~60 min");
  assert.equal(ctx.finestraTraffico({ traffic_series: [] }), "",
    "senza dati non si annuncia una finestra");
});


// ── 11. Ottimizzazione: aggiornamento parziale ─────────────────────

test("gli update non riscrivono piu' l'intera pagina", () => {
  const { ctx, dammi } = contesto();
  const view = dammi("#view");
  const s = snap();
  ctx.__interni.state.snap = s;
  ctx.pageDashboard(view, s);
  const dopoIlPrimoRender = view.scritture;

  // Dieci update: prima ognuno ricostruiva tutto il DOM della pagina di
  // partenza, distruggendo il canvas e la selezione di testo in corso.
  for (let i = 0; i < 10; i++) assert.equal(ctx.refreshDashboard(view, s), true);
  assert.equal(view.scritture, dopoIlPrimoRender,
    "il refresh parziale non deve toccare il contenitore della pagina");
});

test("finche' la pagina non e' costruita si ricade sul render pieno", () => {
  const { ctx, dammi } = contesto();
  // querySelector qui restituisce nodi finti sempre presenti, quindi si prova
  // il contratto al contrario: la rotta dichiara un refresh, e renderIfLive lo
  // usa solo se ritorna true.
  assert.equal(typeof ctx.__interni.PAGES.dashboard.refresh, "function");
  assert.equal(ctx.refreshDashboard(dammi("#view"), snap()), true);
});


// ── Fascia alta: i numeri portano da qualche parte ─────────────────

test("ogni riquadro in alto porta alla pagina che se ne occupa", () => {
  const { ctx } = contesto();
  const html = ctx.dashKpi(snap());
  for (const rotta of ["devices", "services", "wireguard", "wan"])
    assert.ok(html.includes(`data-vai="${rotta}"`), `il riquadro non porta a ${rotta}`);
});

test("prima che arrivino i dati la fascia non si dichiara verde", () => {
  // "–/–" con il pallino verde direbbe "tutto a posto" di una cosa che non si
  // e' ancora guardata: e' la stessa bugia dei numeri vecchi mostrati da nuovi.
  const { ctx } = contesto();
  const html = ctx.dashKpi({});
  assert.ok(!html.includes("s-on"), "nessun pallino verde senza dati");
  assert.ok(!html.includes("val green"), "nessun numero verde senza dati");
  assert.ok(html.includes("in attesa…"));
});

test("i riquadri dicono anche se c'e' qualcosa da fare", () => {
  const { ctx } = contesto();
  assert.ok(ctx.dashKpi(snap()).includes("3 spenti"));
  assert.ok(ctx.dashKpi(snap()).includes("tutti attivi"), "5 servizi su 5");
  const rotto = snap({ services: { summary: { total: 5, ok: 3, down: 2 } } });
  assert.ok(ctx.dashKpi(rotto).includes("2 non rispondono"));
  assert.ok(ctx.dashKpi(rotto).includes("val orange"), "e il numero deve virare");
});

test("con uno solo la frase va al singolare", () => {
  // "1 non rispondono" si legge sbagliato ogni volta che si apre la pagina.
  const { ctx } = contesto();
  const uno = snap({ services: { summary: { total: 5, ok: 4, down: 1 } },
                     devices_summary: { total: 12, online: 11, offline: 1 },
                     wireguard: { total_peers: 4, active_peers: 3 } });
  const html = ctx.dashKpi(uno);
  assert.ok(html.includes("1 non risponde") && !html.includes("1 non rispondono"));
  assert.ok(html.includes("1 spento") && !html.includes("1 spenti"));
  assert.ok(html.includes("1 non connesso") && !html.includes("1 non connessi"));
});


// ── Icone della sidebar ────────────────────────────────────────────

test("la sidebar ha icone disegnate, non glifi che si somigliano", () => {
  // Erano ▦ Dashboard, ▤ Host, ▮ Terminale, ≣ Logs: quattro rettangoli scuri
  // indistinguibili a colpo d'occhio.
  const voci = HTML.match(/<a data-route="[^"]+">\s*<svg/g) || [];
  assert.equal(voci.length, 11, "ogni voce di menu deve avere la sua icona SVG");
  assert.ok(!HTML.includes('class="ico"'), "i glifi monocromatici sono spariti");
  // Nessun font o file esterno: la dashboard gira anche senza internet.
  assert.ok(!/fonts\.googleapis|<img|\.svg"/.test(HTML), "niente risorse esterne");
});

test("le voci di menu sono raggruppate", () => {
  const sez = HTML.match(/<div class="nav-sez"[^>]*>([^<]+)<\/div>/g) || [];
  assert.equal(sez.length, 4);
  assert.match(CSS, /\.nav-sez\s*\{/);
});

test("l'etichetta della KPI sta sopra il valore", () => {
  const { ctx } = contesto();
  const html = ctx.kpi("9", "dispositivi online", "green", { ctx: "3 spenti" });
  assert.ok(html.indexOf('class="sub"') < html.indexOf('class="val'),
    "prima si legge cosa si sta guardando, poi il numero");
});

test("il grafico riempie l'area sotto ogni linea", () => {
  // Due tratti da 2px su fondo scuro si leggono solo fermandosi a guardarli.
  const { ctx } = contesto();
  const tela = elemento();
  ctx.drawTrafficChart(tela, snap().traffic_series);
  assert.ok(tela.colori.some(c => c.startsWith("grad:#4dd6e0")), "area RX");
  assert.ok(tela.colori.some(c => c.startsWith("grad:#5bd97f")), "area TX");
  assert.ok(tela.colori.some(c => c.endsWith("00")), "sfuma a zero verso il basso");
});

test("un buco nei dati spezza anche l'area, non solo la linea", () => {
  const { ctx } = contesto();
  const tela = elemento();
  // Se l'area fosse un poligono unico, il buco verrebbe riempito lo stesso e
  // "nessun dato" diventerebbe indistinguibile da "nessun traffico".
  ctx.drawTrafficChart(tela, [
    { t: 1, rx_bps: 100, tx_bps: 10 }, { t: 2, rx_bps: null, tx_bps: null },
    { t: 3, rx_bps: 300, tx_bps: 30 }, { t: 4, rx_bps: 200, tx_bps: 20 },
  ]);
  assert.ok(tela.colori.includes("#4dd6e0"), "la linea viene comunque disegnata");
});

test("lo stato dei servizi e' un chip colorato", () => {
  const { ctx } = contesto();
  assert.match(ctx.chipStato(svc({ running: true })), /class="chip ok"/);
  assert.match(ctx.chipStato(svc({ running: false })), /class="chip giu"/);
  assert.match(ctx.chipStato(svc({ kind: "systemd", available: false, ok: false })),
    /class="chip ignoto"/);
  for (const c of ["ok", "giu", "ignoto"])
    assert.match(CSS, new RegExp(`\\.chip\\.${c}\\s*\\{`), `manca lo stile .chip.${c}`);
});

test("il motivo lungo di un healthcheck non sfonda la colonna", () => {
  const { ctx } = contesto();
  const lungo = "connessione rifiutata dopo 5s ".repeat(8);
  const chip = ctx.chipStato(svc({ kind: "healthcheck", ok: false, detail: lungo }));
  assert.ok(chip.includes(">non risponde<"), "in cella ci va una parola sola");
  assert.ok(chip.includes("title="), "il motivo per esteso resta, nel title");
});

test("i peer WireGuard si vedono uno per uno, non come conteggio", () => {
  const { ctx } = contesto();
  const s = snap({ wireguard: { total_peers: 2, active_peers: 1, interfaces: [{ peers: [
    { name: "portatile", status: "active", endpoint: "198.51.100.4:51820",
      rx_mb: 12, tx_mb: 3, last_handshake_ago: "40s" },
    { name: "telefono", status: "idle", endpoint: "", rx_mb: 0, tx_mb: 0,
      last_handshake_ago: "2h" },
  ] }] } });
  const html = ctx.wgMini(s);
  assert.ok(html.includes("portatile") && html.includes("telefono"));
  assert.ok(html.indexOf("telefono") < html.indexOf("portatile"),
    "chi non e' connesso sta in cima, come per i servizi");
  assert.ok(html.includes('data-vai="wireguard"'));
  assert.ok(html.includes("nessun endpoint"), "un endpoint vuoto si dichiara");
});

test("senza peer la card lo dice invece di restare vuota", () => {
  const { ctx } = contesto();
  assert.ok(ctx.wgMini({ wireguard: {} }).includes("Nessun peer"));
});

test("la card degli alert esiste solo quando c'e' un alert", () => {
  // Prima era una card fissa che per quasi tutto il tempo diceva "nessun
  // alert", occupando un quarto della pagina per non dire niente.
  const { ctx } = contesto();
  assert.equal(ctx.dashAlertBox(snap()), "");
  const conAlert = snap({ alerts: [{ scope: "services", level: "warn", title: "SearXNG giu'",
    detail: "fermo", since: 1000 }], alerts_summary: { total: 1, warn: 1, by_scope: {} } });
  assert.ok(ctx.dashAlertBox(conAlert).includes("SearXNG"));
});

test("la dashboard usa due colonne disuguali", () => {
  const { ctx, dammi } = contesto();
  const view = dammi("#view");
  ctx.__interni.state.snap = snap();
  ctx.pageDashboard(view, snap());
  assert.ok(view.innerHTML.includes("cols-2-1"));
  assert.match(CSS, /\.grid\.cols-2-1\s*\{\s*grid-template-columns:\s*2fr 1fr/);
});
