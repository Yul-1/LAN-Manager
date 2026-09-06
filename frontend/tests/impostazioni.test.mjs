/* ===================================================================
   impostazioni.test.mjs — Pagina Impostazioni

   Tre difetti:
     1. mezza pagina chiede di "riavviare il servizio" (cinque volte) e
        nessuno diceva **come** si fa: il proprietario non e' uno
        sviluppatore, e un ordine senza istruzioni non e' un'istruzione;
     2. uscire dalla pagina con l'editor della configurazione modificato
        buttava via il lavoro **in silenzio**: alla visita dopo la
        pagina si ricostruisce da capo rileggendo il file;
     3. i pulsanti di salvataggio non dicevano di stare lavorando, e due
        clic mandavano due salvataggi.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES };";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false, checked: false, options: { length: 0 },
    // Il riavvio conferma con un toast, e toast() sfoltisce la pila guardando
    // i figli del contenitore.
    children: [],
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {},
    appendChild(f) { el.children.push(f); return f; },
    append() {}, remove() {}, focus() {}, closest: () => null,
    // Uscendo dalla pagina si disegna la dashboard, che ha dei grafici.
    getContext: () => new Proxy({}, { get: (_t, k) =>
      (k === "createLinearGradient" ? () => ({ addColorStop() {} }) : () => {}), set: () => true }),
    clientWidth: 600, clientHeight: 200, width: 0, height: 0,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

const YAML_INIZIALE = "router:\n  host: 192.0.2.1\n";

/* `salvaOk: false` fa fallire SOLO il salvataggio: la lettura iniziale deve
   riuscire, altrimenti l'editor non ha nemmeno un testo di partenza. */
function contesto({ salvaOk = true, lento = false, confermaSi = false,
                    riavvio = { available: true, policy: "unless-stopped",
                                container: "abc123", reason: "" } } = {}) {
  const chiamate = [];
  const nodi = new Map();
  const conferme = [];
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => ({ matches: false }) },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    confirm: (testo) => { conferme.push(testo); return confermaSi; },   // di serie "no, resto qui"
    fetch: async (url, init) => {
      const metodo = (init && init.method) || "GET";
      chiamate.push({ url, metodo });
      if (lento) await new Promise(r => setTimeout(r, 40));
      const vaBene = salvaOk || metodo === "GET";
      const corpo = url.includes("/api/config/restart")
          ? (metodo === "POST" ? { ok: true, detail: "riavvio in corso" } : riavvio)
        : url.includes("/api/config/backups") ? { backups: [] }
        : url.includes("/api/config/secrets") ? { secrets: [] }
        : url.includes("/api/config/section/") ? { value: [] }
        : url.includes("/api/alerts/rules") ? { rules: [] }
        : url.includes("/api/config/") ? { yaml: YAML_INIZIALE, path: "/app/config/config.yaml" }
        : {};
      return { ok: vaBene, status: vaBene ? 200 : 400,
               headers: { get: () => "application/json" },
               text: async () => JSON.stringify(vaBene ? corpo : { detail: "riga 3: indentazione" }) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate, conferme };
}

async function apri(ctx) {
  const view = elemento();
  ctx.__interni.state.snap = { alerts: [], alerts_summary: {} };
  ctx.__interni.state.route = "settings";
  await ctx.pageSettings(view);
  await new Promise(r => setTimeout(r, 20));    // le sette letture in parallelo
  return view;
}


// ── 1. Come si riavvia ─────────────────────────────────────────────

test("la pagina dice come si riavvia il servizio, non solo che va fatto", async () => {
  const { ctx } = contesto();
  const view = await apri(ctx);
  assert.ok(view.innerHTML.includes("docker compose restart"),
    "chiedere un riavvio senza dire come si fa non e' un'istruzione");
  assert.match(view.innerHTML, /sessioni aperte \(terminale compreso\) si chiudono/,
    "e si dice cosa comporta");
});


// ── 1-bis. Riavviare dalla pagina ──────────────────────────────────
//  Il servizio non si riavvia da se': esce, e a riaccenderlo e' la politica di
//  riavvio del container. Dove non c'e' chi lo riaccenda, offrire il pulsante
//  vorrebbe dire offrire di spegnere la dashboard.

test("il pulsante di riavvio compare quando c'e' chi riaccende il servizio", async () => {
  const { ctx, dammi } = contesto();
  await apri(ctx);
  assert.match(dammi("#riavvio-azione").innerHTML, /Riavvia il servizio/);
  assert.match(dammi("#riavvio-azione").innerHTML, /unless-stopped/,
    "si dice anche chi lo riaccende, non solo che si puo'");
  assert.equal(typeof dammi("#riavvio-ora").onclick, "function");
});

test("senza chi lo riaccenda il pulsante non c'e', e si dice perche'", async () => {
  const { ctx, dammi } = contesto({ riavvio: { available: false, policy: "no", container: "",
    reason: "il container ha politica di riavvio 'no'" } });
  await apri(ctx);
  const html = dammi("#riavvio-azione").innerHTML;
  assert.ok(!html.includes("<button"), "un pulsante che spegne e basta non si offre");
  assert.match(html, /politica di riavvio 'no'/);
});

test("riavviare chiede conferma, e un no non chiama niente", async () => {
  const { ctx, dammi, chiamate, conferme } = contesto();
  await apri(ctx);
  await dammi("#riavvio-ora").onclick();
  assert.ok(conferme.some(t => /Riavviare il servizio/.test(t)));
  assert.ok(conferme.some(t => /terminale SSH compreso/.test(t)),
    "chi preme deve sapere che chiude le sessioni aperte");
  assert.ok(!chiamate.some(c => c.url.includes("/api/config/restart") && c.metodo === "POST"));
});

test("confermato, il riavvio parte davvero", async () => {
  const { ctx, dammi, chiamate } = contesto({ confermaSi: true });
  await apri(ctx);
  await dammi("#riavvio-ora").onclick();
  assert.ok(chiamate.some(c => c.url.includes("/api/config/restart") && c.metodo === "POST"));
});

test("con l'editor modificato il riavvio avvisa prima di perdere il lavoro", async () => {
  const { ctx, dammi, conferme } = contesto();
  await apri(ctx);
  dammi("#cfg-yaml").value = "router:\n  host: 192.0.2.99\n";   // non salvato
  await dammi("#riavvio-ora").onclick();
  assert.match(conferme[0], /modifiche non salvate/,
    "il riavvio le perde: va detto prima, non dopo");
});


// ── 2. Modifiche non salvate ───────────────────────────────────────

test("uscendo con modifiche non salvate si chiede conferma", async () => {
  const { ctx, dammi, conferme } = contesto();
  await apri(ctx);
  dammi("#cfg-yaml").value = YAML_INIZIALE + "  port: 22\n";
  assert.equal(ctx.cfgSporco(), true);
  ctx.go("dashboard");
  assert.equal(conferme.length, 1, "senza conferma il lavoro spariva in silenzio");
  assert.equal(ctx.__interni.state.route, "settings", "rispondendo no si resta");
});

test("senza modifiche non si chiede niente", async () => {
  const { ctx, conferme } = contesto();
  await apri(ctx);
  ctx.go("dashboard");
  assert.equal(conferme.length, 0);
});

test("dopo il salvataggio l'editor non e' piu' 'sporco'", async () => {
  const { ctx, dammi } = contesto();
  await apri(ctx);
  dammi("#cfg-yaml").value = YAML_INIZIALE + "  port: 22\n";
  await dammi("#cfg-save").onclick();
  assert.equal(ctx.cfgSporco(), false);
  assert.equal(dammi("#cfg-dirty").textContent, "");
});

test("un salvataggio fallito lascia le modifiche in piedi", async () => {
  const { ctx, dammi, conferme } = contesto({ salvaOk: false });
  await apri(ctx);
  dammi("#cfg-yaml").value = YAML_INIZIALE + "storto:\n";
  await dammi("#cfg-save").onclick();
  assert.equal(ctx.cfgSporco(), true,
    "dando per salvato quello che il backend ha rifiutato si perderebbe il lavoro all'uscita");
  ctx.go("dashboard");
  assert.equal(conferme.length, 1);
});


// ── 3. Pulsanti che lavorano in silenzio ───────────────────────────

test("il pulsante Salva dice di stare salvando e non accetta due clic", async () => {
  const { ctx, dammi, chiamate } = contesto({ lento: true });
  await apri(ctx);
  const btn = dammi("#cfg-save");
  dammi("#cfg-yaml").value = YAML_INIZIALE + "  port: 22\n";
  const prima = chiamate.filter(c => c.metodo === "PUT").length;
  const uno = btn.onclick();
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, "salvataggio…");
  const due = btn.onclick();                 // secondo clic mentre salva
  await Promise.all([uno, due]);
  assert.equal(chiamate.filter(c => c.metodo === "PUT").length, prima + 1, "un salvataggio solo");
  assert.equal(btn.disabled, false, "e il pulsante torna utilizzabile");
});

test("ricaricare l'editor con modifiche in piedi chiede conferma", async () => {
  const { ctx, dammi, conferme, chiamate } = contesto();
  await apri(ctx);
  dammi("#cfg-yaml").value = "altro\n";
  const prima = chiamate.length;
  await dammi("#cfg-reload").onclick();
  assert.equal(conferme.length, 1);
  assert.equal(chiamate.length, prima, "rispondendo no non si rilegge nulla");
});
