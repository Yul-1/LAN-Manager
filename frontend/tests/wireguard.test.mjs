/* ===================================================================
   wireguard.test.mjs — Pagina WireGuard

   Sei difetti, visti sulla pagina vera con lo stato vero del router
   (una interfaccia `wgclient`, un peer verso il relay EC2):
     1. il peer principale si presentava come una riga di base64 (la sua
        chiave pubblica tagliata), perche' il catalogo dei nomi e' vuoto
        e niente diceva dove si danno i nomi;
     2. testi in inglese in mezzo all'italiano: "1m ago", "never",
        "active", "idle" — anche nella card della dashboard;
     3. "idle" era la stessa parola, con lo stesso colore, per un peer
        mai collegato e per uno caduto sette ore fa;
     4. la pagina non diceva l'eta' del dato ne' da dove arriva (la VPN
        gira sul router, non su questo host), e dava RX/TX totali come
        se fossero la velocita' del momento;
     5. "Nessuna interfaccia WireGuard" era la stessa frase con il
        router muto e con il router che risponde senza WireGuard;
     6. nessun aggiornamento parziale: la pagina si riscriveva intera e
        l'avviso di sorgente non seguiva i dati.
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

function contesto() {
  const nodi = new Map();
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => null },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi,
                // Solo l'avviso di sorgente: e' quello che il refresh parziale
                // deve riscrivere, e va osservato.
                querySelectorAll: (sel) => sel === ".src-nota" ? [dammi(".src-nota")] : [],
                addEventListener() {} },
    console, location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, headers: { get: () => "application/json" },
                          text: async () => "{}" }),
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi };
}

const ora = () => Math.floor(Date.now() / 1000);
const CHIAVE = "0jz4/hFPrJ86ex/oQ1w0mNQ7cQ1sB2hFq0mF3Z9pXk8=";

/* Lo stato come lo manda il backend: `name` gia' ripiegato sulla chiave
   tagliata quando il catalogo non ha un nome (services/wireguard.py). */
function peer(over = {}) {
  return Object.assign({
    name: CHIAVE.slice(0, 16) + "…", public_key: CHIAVE,
    endpoint: "203.0.113.10:51820", allowed_ips: ["10.100.0.0/24"],
    last_handshake: ora() - 62, last_handshake_ago: "1m ago",
    rx_mb: 10.59, tx_mb: 369.7, status: "active",
  }, over);
}

function snap(over = {}) {
  const p = over.peers || [peer()];
  return {
    ts: ora(),
    sources: { wireguard: { ok: true, ts: ora() - 6 } },
    meta: { router_name: "gateway", subnets: [] },
    wireguard: {
      relay: "203.0.113.10", ts: ora(),
      total_peers: p.length, active_peers: p.filter(x => x.status === "active").length,
      interfaces: [{ name: "wgclient", listen_port: 56224, peers: p,
                     total_peers: p.length,
                     active_peers: p.filter(x => x.status === "active").length }],
    },
    alerts: [], devices: [], services: {}, docker: {}, system: {}, resources: {},
  };
}

function pagina(ctx, s) {
  const view = elemento();
  ctx.__interni.state.snap = s;
  ctx.pageWireGuard(view, s);
  return view.innerHTML;
}


// ── 1. Il nome del peer ────────────────────────────────────────────

test("un peer senza nome non si presenta come una riga di base64", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap());
  assert.ok(!html.includes(CHIAVE.slice(0, 16) + "…</td>"),
    "il nome mostrato era la chiave pubblica tagliata");
  assert.ok(html.includes("relay 203.0.113.10"),
    "il peer punta all'endpoint dichiarato come relay in configurazione: lo si dice");
  assert.ok(html.includes("wireguard.peer_names"),
    "e la pagina dice dove si danno i nomi ai peer");
});

test("il nome del catalogo, quando c'e', vince su tutto", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap({ peers: [peer({ name: "relay EC2" })] }));
  assert.ok(html.includes("relay EC2"));
  assert.ok(!html.includes("wireguard.peer_names"),
    "e il suggerimento sui nomi non compare se non serve");
});


// ── 2 e 3. Stato leggibile, e distinto ─────────────────────────────

test("gli stati sono in italiano e distinguono 'mai' da 'caduto'", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap({ peers: [
    peer({ name: "relay", status: "active" }),
    peer({ name: "telefono", status: "idle", last_handshake: 0, last_handshake_ago: "never" }),
    peer({ name: "portatile", status: "idle", last_handshake: ora() - 26000,
           last_handshake_ago: "7h ago" }),
  ] }));
  assert.ok(html.includes(">attivo<"));
  assert.ok(html.includes(">mai collegato<"), "un peer mai visto non e' un peer caduto");
  assert.ok(/fermo da 7h/.test(html), "e uno caduto dice da quanto");
  for (const inglese of ["1m ago", "never", "7h ago", ">active<", ">idle<"])
    assert.ok(!html.includes(inglese), `testo inglese rimasto: ${inglese}`);
});

test("anche la card della dashboard smette di parlare inglese", () => {
  const { ctx } = contesto();
  const s = snap({ peers: [peer({ name: "casa" }),
                           peer({ status: "idle", last_handshake: 0, last_handshake_ago: "never" })] });
  ctx.__interni.state.snap = s;          // come durante il render della dashboard
  const html = ctx.wgMini(s);
  assert.ok(!html.includes("1m ago") && !html.includes("never"));
  assert.ok(html.includes("relay 203.0.113.10"), "e nemmeno li' il nome e' una chiave");
});


// ── 4. Da dove arriva il dato, e cosa sono quei numeri ─────────────

test("la pagina dichiara sorgente, eta' del dato e cosa sono RX/TX", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap());
  assert.ok(html.includes("gateway"), "la VPN gira sul router, e la pagina lo dice");
  assert.ok(/dato di \d+s fa/.test(html), "eta' del dato");
  assert.ok(html.includes("totali"), "RX/TX sono contatori, non una velocita'");
});


// ── 5. Stati vuoti distinti ────────────────────────────────────────

test("il vuoto dice la sua causa", () => {
  const vuota = (over) => {
    const { ctx } = contesto();
    const s = snap();
    s.wireguard.interfaces = [];
    s.wireguard.total_peers = 0; s.wireguard.active_peers = 0;
    Object.assign(s, over);
    return pagina(ctx, s);
  };
  assert.match(vuota({ sources: {} }), /In attesa del primo giro/);
  assert.match(vuota({ sources: { wireguard: { ok: false, error: "ssh: timeout" } } }),
               /il router non risponde/);
  assert.match(vuota({}), /non ha interfacce WireGuard attive/);
});


// ── 6. Aggiornamento parziale ──────────────────────────────────────

test("la pagina si aggiorna per parti e l'avviso segue i dati", () => {
  const { ctx, dammi } = contesto();
  const view = elemento();
  ctx.__interni.state.snap = snap();
  ctx.pageWireGuard(view, ctx.__interni.state.snap);
  assert.equal(typeof ctx.__interni.PAGES.wireguard.refresh, "function",
    "senza `refresh` la pagina si riscriveva intera ad ogni update");

  // La sorgente cade: nessun dato nuovo, quindi nessun ts nuovo.
  const rotto = snap();
  rotto.sources = { wireguard: { ok: false, error: "ssh: connessione rifiutata", ts: ora() - 90 } };
  ctx.__interni.state.snap = rotto;
  assert.equal(ctx.refreshWireGuard(view, rotto), true);
  assert.ok(dammi(".src-nota").innerHTML.includes("connessione rifiutata"),
    "l'avviso sta fuori dal corpo: senza aggiornarlo restava quello di prima");
  assert.ok(dammi("#wg-corpo").innerHTML.includes("peer attivi"), "e il corpo si riscrive");
});


test("senza nome ne' relay, il peer prende il suo indirizzo nella VPN", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap({ peers: [peer({
    endpoint: "203.0.113.7:38112", allowed_ips: ["10.100.0.9/32"] })] }));
  assert.ok(html.includes(">10.100.0.9\n") || html.includes("10.100.0.9"),
    "l'indirizzo /32 e' il suo, e si legge meglio di una chiave");
});

test("il nome del router compare solo se lo si conosce", () => {
  const { ctx } = contesto();
  const s = snap();
  delete s.meta;
  const html = pagina(ctx, s);
  assert.ok(!html.includes("(router)"), "senza nome si scriveva 'sul router (router)'");
  assert.ok(pagina(ctx, snap()).includes("(gateway)"));
});


test("un peer scollegato non viene dipinto come un guasto", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap({ peers: [
    peer({ name: "relay" }),
    peer({ name: "telefono", status: "idle", last_handshake: ora() - 26000 }),
  ] }));
  // Il chip rosso e' riservato ai guasti: un peer si collega quando serve.
  assert.ok(!/chip giu/.test(html), "chip rosso su un peer semplicemente scollegato");
  assert.ok(html.includes("s-on"), "col tunnel su il pallino resta verde");
  assert.ok(!html.includes("Nessun peer collegato"), "e non si avvisa di nulla");
});

test("quando non e' collegato nessuno, invece, lo si dice", () => {
  const { ctx } = contesto();
  const html = pagina(ctx, snap({ peers: [
    peer({ name: "relay", status: "idle", last_handshake: ora() - 900 }),
  ] }));
  assert.ok(html.includes("s-warn"));
  assert.ok(html.includes("Nessun peer collegato"));
});
