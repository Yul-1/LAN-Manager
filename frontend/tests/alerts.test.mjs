/* ===================================================================
   alerts.test.mjs — alert contestuali

   Gli alert vecchi erano una lista di nomi in dashboard piu' tre regole
   fisse nella pagina Host: nessun motivo, nessun "da quando", nessun modo
   di zittirli. Qui si prova cio' che rende utile la sostituzione: ogni
   avviso compare nella pagina che possiede l'oggetto, il contatore in
   sidebar non si accende per le informazioni, e silenziare pretende il
   motivo.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, PAGES };";

function elemento(extra = {}) {
  const figli = [];
  const memo = new Map();
  const el = {
    className: "", innerHTML: "", textContent: "", title: "", hidden: false,
    value: "", style: {}, dataset: {}, children: figli,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    setAttribute() {}, appendChild(f) { figli.push(f); return f; },
    remove() { const i = figli.indexOf(el); if (i >= 0) figli.splice(i, 1); },
    // Memoizzato per selettore: il test deve poter riprendere lo stesso nodo
    // che il codice ha appena configurato (il campo motivo, il pulsante Salva).
    querySelector(sel) {
      if (!memo.has(sel)) memo.set(sel, elemento());
      return memo.get(sel);
    },
    querySelectorAll: () => [],
    addEventListener() {}, focus() {}, closest: () => null,
    getContext: () => new Proxy({}, { get: (_t, k) =>
      (k === "createLinearGradient" ? () => ({ addColorStop() {} }) : () => {}), set: () => true }),
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto() {
  const chiamate = [];
  const ascoltatori = [];
  const body = elemento();
  // I contenitori che il codice cerca per classe: qui sono l'oggetto in esame.
  const boxes = ["dashboard", "services", "resources", "host", "settings"]
    .map(scope => elemento({ dataset: { scope } }));
  const badge = ["dashboard", "devices", "services", "wireguard", "host", "settings"]
    .map(route => elemento({ dataset: { route } }));
  const nodi = new Map([
    ["#conn", elemento()], ["#updated", elemento()], ["#view", elemento()],
    ["#nav-devices", elemento()], ["#nav-services", elemento()],
    ["#nav-wg", elemento()], ["#foot-info", elemento()], ["#sec-banner", elemento()],
    ["#page-title", elemento()],
  ]);
  const perClasse = new Map([[".alert-box", boxes], [".badge.alert", badge]]);

  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: {
      body,
      createElement: () => elemento(),
      querySelector: (sel) => nodi.get(sel) || null,
      querySelectorAll: (sel) => perClasse.get(sel) || [],
      addEventListener: (tipo, fn) => ascoltatori.push([tipo, fn]),
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async (url, opt) => {
      chiamate.push({ url, opt });
      return { ok: true, status: 200, text: async () => "{}",
               headers: { get: () => "application/json" } };
    },
    AbortController,
    setTimeout: (fn) => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  // Il render vero disegnerebbe grafici e aprirebbe chiamate: qui interessano
  // i box e i badge, che si aggiornano prima e indipendentemente.
  ctx.renderRoute = () => {};
  return { ctx, chiamate, ascoltatori, body, boxes, badge, nodi };
}

const A = (over = {}) => Object.assign({
  id: "docker.restart_loop:nas/uno", rule: "docker.restart_loop", subject: "nas/uno",
  level: "warn", scope: "services", title: "Container uno in restart loop",
  detail: "esce subito dopo l'avvio", action: "docker logs uno",
  source: "docker", since: 1000,
}, over);

function snap(alerts, summary) {
  return { alerts, alerts_summary: summary || riassunto(alerts), _ts: 2000 };
}

function riassunto(alerts) {
  const by_scope = {};
  let critical = 0, warn = 0, info = 0;
  alerts.forEach(a => {
    const r = by_scope[a.scope] || (by_scope[a.scope] = { critical: 0, warn: 0, info: 0, total: 0 });
    r[a.level] += 1; r.total += 1;
    if (a.level === "critical") critical += 1;
    else if (a.level === "warn") warn += 1;
    else info += 1;
  });
  return { critical, warn, info, total: alerts.length, by_scope };
}


test("ogni alert compare nella pagina che possiede l'oggetto", () => {
  const { ctx, boxes } = contesto();
  ctx.__interni.state.snap = snap([A(), A({ scope: "host", rule: "host.subnet_duplicata",
    subject: "192.0.2.0/24", title: "Piu' interfacce sulla stessa subnet" })]);
  ctx.aggiornaBoxAlert();
  const per = Object.fromEntries(boxes.map(b => [b.dataset.scope, b.innerHTML]));
  assert.ok(per.services.includes("restart loop"));
  assert.ok(per.host.includes("stessa subnet"));
  assert.ok(!per.services.includes("stessa subnet"), "un alert non deve comparire in due pagine");
  assert.equal(per.dashboard, "", "senza alert il contenitore resta vuoto");
});

test("gli avvisi di sicurezza non si ripetono nel box di pagina", () => {
  // Hanno gia' il banner rosso in cima e la riga nell'indice: nel box sarebbero
  // la terza copia nella stessa schermata.
  const { ctx, boxes } = contesto();
  ctx.__interni.state.snap = snap([A({ scope: "settings", source: "security",
    rule: "security.api_aperta", level: "critical", title: "API senza login" })]);
  ctx.aggiornaBoxAlert();
  const settings = boxes.find(b => b.dataset.scope === "settings");
  assert.equal(settings.innerHTML, "");
  // ma nell'indice della dashboard c'e', che e' da dove si ritrova.
  assert.ok(ctx.alertIndice(ctx.__interni.state.snap.alerts).includes("API senza login"));
});

test("il contatore somma Servizi e Risorse sulla stessa voce di menu", () => {
  const { ctx, badge } = contesto();
  ctx.__interni.state.snap = snap([
    A({ scope: "services" }),
    A({ scope: "resources", rule: "resources.host_ssh_giu", subject: "192.0.2.9" }),
  ]);
  ctx.aggiornaBadgeAlert();
  const services = badge.find(b => b.dataset.route === "services");
  assert.equal(services.textContent, "2", "Risorse sta sotto la voce Monitoraggio");
  assert.equal(services.hidden, false);
});

test("il badge degli alert non tocca quello dei conteggi", () => {
  const { ctx, nodi, badge } = contesto();
  const conteggi = nodi.get("#nav-services");
  conteggi.textContent = "12/20";
  ctx.__interni.state.snap = snap([A()]);
  ctx.aggiornaBadgeAlert();
  assert.equal(conteggi.textContent, "12/20", "sono due badge distinti");
  assert.equal(badge.find(b => b.dataset.route === "services").textContent, "1");
});

test("le informazioni non accendono il contatore", () => {
  // Un badge sempre acceso e' la versione in miniatura del difetto da cui
  // nasce questa fase.
  const { ctx, badge, boxes } = contesto();
  ctx.__interni.state.snap = snap([A({ scope: "host", level: "info",
    rule: "host.link_giu_con_ip", subject: "eth1", title: "eth1 ha un IP ma il link e' giu'" })]);
  ctx.aggiornaBadgeAlert();
  ctx.aggiornaBoxAlert();
  const host = badge.find(b => b.dataset.route === "host");
  assert.equal(host.textContent, "");
  assert.equal(host.hidden, true);
  assert.ok(boxes.find(b => b.dataset.scope === "host").innerHTML.includes("eth1"),
    "in pagina resta visibile: e' un'informazione, non un silenzio");
});

test("senza alert i badge restano spenti", () => {
  const { ctx, badge } = contesto();
  ctx.__interni.state.snap = snap([]);
  ctx.aggiornaBadgeAlert();
  assert.ok(badge.every(b => b.hidden && b.textContent === ""));
});

test("solo i critici colorano di rosso", () => {
  const { ctx, badge } = contesto();
  ctx.__interni.state.snap = snap([A({ scope: "services", level: "critical" })]);
  ctx.aggiornaBadgeAlert();
  assert.ok(badge.find(b => b.dataset.route === "services").className.includes("critical"));
  ctx.__interni.state.snap = snap([A({ scope: "services", level: "warn" })]);
  ctx.aggiornaBadgeAlert();
  assert.ok(!badge.find(b => b.dataset.route === "services").className.includes("critical"));
});

test("il box si aggiorna anche sulle pagine che non si ridisegnano", () => {
  // Host e Impostazioni sono `live: false`: senza l'aggiornamento esplicito in
  // onData il box resterebbe quello di quando si e' entrati nella pagina, cioe'
  // il guasto comparso dopo non si vedrebbe mai.
  const { ctx, boxes } = contesto();
  const host = boxes.find(b => b.dataset.scope === "host");
  ctx.__interni.state.route = "host";
  ctx.__interni.state.snap = snap([]);
  ctx.onData();
  assert.equal(host.innerHTML, "");
  ctx.__interni.state.snap = snap([A({ scope: "host", rule: "host.subnet_duplicata",
    subject: "192.0.2.0/24", title: "Piu' interfacce sulla stessa subnet" })]);
  ctx.onData();
  assert.ok(host.innerHTML.includes("stessa subnet"));
});

test("silenziare chiede il motivo prima di chiamare il backend", async () => {
  const { ctx, chiamate, body } = contesto();
  ctx.openSilenceModal("docker.restart_loop", "nas/uno");
  const ov = body.children[body.children.length - 1];
  ov.querySelector("#sl-reason").value = "   ";
  await ov.querySelector("#sl-save").onclick();
  assert.equal(chiamate.length, 0, "senza motivo non si salva niente");
  assert.notEqual(ov.querySelector("#sl-msg").style.display, "none");
});

test("silenziare manda regola, soggetto e motivo all'endpoint dedicato", async () => {
  const { ctx, chiamate, body } = contesto();
  ctx.openSilenceModal("host.subnet_duplicata", "192.0.2.0/24");
  const ov = body.children[body.children.length - 1];
  ov.querySelector("#sl-reason").value = "due schede volute";
  await ov.querySelector("#sl-save").onclick();
  assert.equal(chiamate.length, 1);
  assert.ok(chiamate[0].url.endsWith("/api/alerts/silence"));
  assert.deepEqual(JSON.parse(chiamate[0].opt.body), {
    rule: "host.subnet_duplicata", subject: "192.0.2.0/24", reason: "due schede volute",
  });
});

test("cliccare una riga dell'indice porta alla pagina che possiede l'oggetto", () => {
  const { ctx, ascoltatori } = contesto();
  ctx.bindAlert();
  const [tipo, gestore] = ascoltatori.find(([t]) => t === "click");
  assert.equal(tipo, "click");
  const riga = { dataset: { vai: "services" } };
  // `closest` risponde per selettore: il gestore ne prova due (prima "a[href]",
  // per non navigare quando si clicca un link vero, poi il contenitore).
  gestore({ target: { closest: (sel) => (sel === "a[href]" ? null : riga) } });
  assert.equal(ctx.__interni.state.route, "services");
});

test("un clic su un link non naviga anche di pagina", () => {
  // I servizi in evidenza sono righe cliccabili che contengono un link: senza
  // la guardia il clic aprirebbe la scheda nuova E porterebbe la dashboard
  // altrove, cosi' tornando indietro ci si ritrovava su un'altra pagina.
  const { ctx, ascoltatori } = contesto();
  ctx.bindAlert();
  const [, gestore] = ascoltatori.find(([t]) => t === "click");
  const riga = { dataset: { vai: "services" } };
  const link = { href: "http://esempio.invalid" };
  gestore({ target: { closest: (sel) => (sel === "a[href]" ? link : riga) } });
  assert.equal(ctx.__interni.state.route, "dashboard", "la pagina non deve cambiare");
});


// ── Nessuna emoji nella UI ─────────────────────────────────────────

test("il pulsante per silenziare e' un'icona disegnata, non un'emoji", () => {
  const { ctx } = contesto();
  ctx.__interni.state.snap = {
    alerts: [{ id: "x:y", rule: "docker.restart_loop", subject: "nas/mosquitto",
               level: "warn", scope: "services", title: "Container in restart loop",
               detail: "esce subito dopo l'avvio", action: "guarda i log",
               source: "docker", since: Math.floor(Date.now() / 1000) - 300 }],
  };
  const html = ctx.alertBoxHtml("services");
  assert.ok(html.includes("<svg"), "l'icona e' disegnata come quelle di sidebar");
  assert.ok(!/[\u{1F000}-\u{1FAFF}\uFE0F]/u.test(html), "emoji nella riga di avviso");
});

test("nessuna emoji nei file della UI", () => {
  // Convenzione di progetto: niente emoji nel codice ne' nei file.
  // I glifi monocromatici usati apposta (✕, ✎, ☰, ❒) non sono emoji: qui si
  // guardano solo i pittogrammi a colori e il selettore di variazione.
  const emoji = /[\u{1F000}-\u{1FAFF}\uFE0F]/u;
  for (const f of ["../app.js", "../index.html", "../styles.css", "../terminal.js"]) {
    const testo = readFileSync(new URL(f, import.meta.url), "utf8");
    assert.ok(!emoji.test(testo), `emoji trovata in ${f}`);
  }
});
