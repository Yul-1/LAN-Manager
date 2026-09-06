/* ===================================================================
   devices.test.mjs — la pagina Dispositivi

   Quattro difetti trovati nel giro su questa pagina, tutti riprodotti
   prima di correggerli:
     1. la mappa disegnava anche i dispositivi NASCOSTI e ignorava i
        filtri, quindi "nascondi" non nascondeva davvero;
     2. `bindMapDrag` riagganciava i listener allo stesso <svg> ad ogni
        update, accumulandone uno ogni 10 secondi;
     3. l'elenco non era ordinato: `registry.all()` restituisce l'ordine
        di scoperta e il registro si ricostruisce ad ogni scan, quindi le
        righe si rimescolavano ogni minuto;
     4. sopra le otto porte aperte le altre sparivano in silenzio.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state, PAGES, map, devFilter };";
const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

function elemento(extra = {}) {
  const memo = new Map();
  const ascoltatori = [];
  const el = {
    className: "", textContent: "", value: "", checked: false, title: "",
    hidden: false, style: {}, dataset: {}, options: { length: 99 },
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    ascoltatori,
    innerHTML: "",
    querySelector(sel) {
      if (!memo.has(sel)) memo.set(sel, elemento());
      return memo.get(sel);
    },
    querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {},
    addEventListener(tipo, fn) { ascoltatori.push([tipo, fn]); },
    removeEventListener() {},
    appendChild(f) { return f; }, append() {}, remove() {}, focus() {},
    closest: () => null, scrollIntoView() {},
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 520, top: 0, left: 0 }),
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
      body: elemento(), createElement: () => elemento(),
      createElementNS: () => elemento(),
      querySelector: dammi, querySelectorAll: () => [], addEventListener() {},
    },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() {} },
    fetch: async () => ({ ok: true, status: 200, text: async () => "{}",
                          headers: { get: () => "application/json" } }),
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, nodi, dammi };
}

/* Indirizzi documentativi, mai quelli veri (stessa regola della suite Python). */
const dev = (o = {}) => Object.assign({
  key: "192.0.2.1", name: "uno", hostname: "", mac: "AA:BB:CC:00:00:01",
  ips: ["192.0.2.1"], type: "server", os: "", subnet: "test", vendor: "",
  open_ports: [], discovered_by: ["arp"], services: [], notes: "", url: "",
  hidden: false, online: true, latency_ms: 3, last_seen: 1000,
}, o);

function conDevice(devices) {
  const { ctx, dammi } = contesto();
  ctx.__interni.state.snap = { devices, meta: { subnets: [] } };
  Object.assign(ctx.__interni.devFilter,
    { subnet: "", status: "", q: "", showHidden: false, detailKey: "" });
  return { ctx, dammi };
}


// ── 1. La mappa non rispettava i filtri ────────────────────────────

/* Cosa la mappa ha davvero disegnato: `map.nodes` tiene un nodo per device
   (piu' il router al centro e un hub per subnet), quindi si guarda li' invece
   di fidarsi della lista passata a startMap. */
function nomiSullaMappa(ctx) {
  return [...ctx.__interni.map.nodes.values()]
    .filter(n => !n.router && !n.hub).map(n => n.name).sort();
}

test("un dispositivo nascosto non finisce sulla mappa", () => {
  const { ctx } = conDevice([
    dev({ key: "192.0.2.1", name: "visibile" }),
    dev({ key: "192.0.2.9", name: "NASCOSTO", ips: ["192.0.2.9"], hidden: true }),
  ]);
  ctx.aggiornaVistaDevice();
  assert.deepEqual(nomiSullaMappa(ctx), ["visibile"],
    "nascondere deve nascondere anche sulla mappa, non solo in elenco");
});

test("i filtri valgono anche per la mappa", () => {
  const { ctx } = conDevice([
    dev({ key: "192.0.2.1", name: "acceso" }),
    dev({ key: "192.0.2.2", name: "spento", ips: ["192.0.2.2"], online: false }),
  ]);
  ctx.__interni.devFilter.status = "offline";
  ctx.aggiornaVistaDevice();
  assert.deepEqual(nomiSullaMappa(ctx), ["spento"]);
});


// ── 2. Listener accumulati sulla mappa ─────────────────────────────

test("riagganciare la mappa non accumula listener", () => {
  // La pagina si aggiorna ogni 10s e da quando ha un refresh parziale l'<svg>
  // e' sempre lo stesso elemento: senza guardia, un listener per ciclo.
  const { ctx, dammi } = conDevice([dev()]);
  const svg = dammi("#lanmap");
  for (let i = 0; i < 5; i++) ctx.startMap(ctx.filteredDevices());
  const suSvg = svg.ascoltatori.filter(([t]) => t === "mousedown" || t === "touchstart");
  assert.equal(suSvg.length, 2, `agganciati ${suSvg.length} listener invece di 2`);
});


// ── 3. Ordine stabile ──────────────────────────────────────────────

test("l'elenco e' ordinato per subnet e IP, non per ordine di scoperta", () => {
  const { ctx } = conDevice([
    dev({ key: "192.0.2.20", name: "venti", ips: ["192.0.2.20"] }),
    dev({ key: "198.51.100.5", name: "altra", ips: ["198.51.100.5"], subnet: "zeta" }),
    dev({ key: "192.0.2.3", name: "tre", ips: ["192.0.2.3"] }),
    dev({ key: "192.0.2.100", name: "cento", ips: ["192.0.2.100"] }),
  ]);
  assert.deepEqual(ctx.filteredDevices().map(d => d.name),
    ["tre", "venti", "cento", "altra"],
    "IP in ordine numerico (non alfabetico: .100 viene dopo .20), subnet raggruppate");
});

test("l'ordine non cambia quando un dispositivo si spegne", () => {
  // Ordinare per stato farebbe saltare di posto la card proprio mentre la
  // si guarda.
  const { ctx } = conDevice([
    dev({ key: "192.0.2.1", name: "a", ips: ["192.0.2.1"] }),
    dev({ key: "192.0.2.2", name: "b", ips: ["192.0.2.2"], online: false }),
    dev({ key: "192.0.2.3", name: "c", ips: ["192.0.2.3"] }),
  ]);
  assert.deepEqual(ctx.filteredDevices().map(d => d.name), ["a", "b", "c"]);
});

test("un dispositivo senza IP va in fondo invece di sparire", () => {
  const { ctx } = conDevice([
    dev({ key: "AA:BB:CC:00:00:09", name: "solo-mac", ips: [], subnet: "" }),
    dev({ key: "192.0.2.1", name: "con-ip" }),
  ]);
  assert.deepEqual(ctx.filteredDevices().map(d => d.name), ["con-ip", "solo-mac"]);
});


// ── 4. Porte troncate in silenzio ──────────────────────────────────

test("oltre otto porte il resto viene dichiarato, non nascosto", () => {
  const { ctx } = contesto();
  const html = ctx.portChips([22, 80, 443, 8080, 9000, 3000, 5432, 6379, 11434, 5000]);
  assert.ok(html.includes("+2"), "deve dire quante ne restano");
  assert.ok(html.includes("11434"), "e tenerle nel title, per poterle leggere");
});

test("senza porte non si disegna una riga vuota", () => {
  const { ctx } = contesto();
  assert.equal(ctx.portChips([]), "");
  assert.equal(ctx.portChips(undefined), "");
});


// ── Elenco a righe, scheda al clic ────────────────────────────────

test("l'elenco resta una tabella a righe", () => {
  // Venti dispositivi si confrontano scorrendo una colonna con gli occhi, non
  // saltando fra venti riquadri.
  const { ctx, dammi } = conDevice([dev({ name: "homeserver" }), dev({ key: "192.0.2.2", name: "altro", ips: ["192.0.2.2"] })]);
  ctx.renderDeviceList();
  const html = dammi("#dlist").innerHTML;
  assert.ok(html.includes("<table"), "l'elenco e' una tabella");
  assert.ok(html.includes("homeserver") && html.includes("altro"));
  assert.ok(!html.includes("dev-scheda"), "chiuso non mostra nessuna scheda");
});

test("la scheda compare solo per la riga su cui si e' cliccato", () => {
  const { ctx, dammi } = conDevice([
    dev({ key: "192.0.2.1", name: "uno" }),
    dev({ key: "192.0.2.2", name: "due", ips: ["192.0.2.2"] }),
  ]);
  ctx.__interni.devFilter.detailKey = "192.0.2.2";
  ctx.renderDeviceList();
  const html = dammi("#dlist").innerHTML;
  assert.equal((html.match(/dev-scheda/g) || []).length, 1);
  assert.ok(html.includes("dev-riga aperta"), "la riga aperta si distingue");
  assert.ok(html.includes("expand open"), "e la freccia e' girata");
});

test("la scheda mostra i campi del dispositivo", () => {
  const { ctx } = conDevice([]);
  const html = ctx.deviceCard(dev({ name: "nas", ips: ["192.0.2.7"], os: "Debian 12",
    vendor: "Synology", hostname: "nas.lan", notes: "in cantina" }));
  for (const atteso of ["nas.lan", "192.0.2.7", "Debian 12", "Synology", "in cantina"])
    assert.ok(html.includes(atteso), `manca ${atteso}`);
});

test("l'URL del pannello e' un link, e uno ostile non lo diventa", () => {
  const { ctx } = conDevice([]);
  assert.match(ctx.deviceCard(dev({ url: "http://192.0.2.7:5000" })),
    /<a href="http:\/\/192\.0\.2\.7:5000" target="_blank" rel="noopener"/);
  // Uno schema non consentito resta visibile come TESTO — cosi' si vede cosa
  // si e' scritto — ma non diventa mai un href su cui si possa cliccare.
  const cattivo = ctx.deviceCard(dev({ url: "javascript:alert(1)" }));
  assert.ok(cattivo.includes("javascript:alert(1)"));
  assert.ok(!/href="javascript:/.test(cattivo));
});

test("senza risultati la pagina lo dice invece di restare bianca", () => {
  const { ctx, dammi } = conDevice([dev()]);
  ctx.__interni.devFilter.q = "niente-che-esista";
  ctx.renderDeviceList();
  assert.ok(dammi("#dlist").innerHTML.includes("Nessun dispositivo"));
});


// ── Le card delle interfacce ───────────────────────────────────────

const IFACE = [
  { name: "eth0", mac: "aa:bb:cc:00:00:01", mtu: 1500, state: "up", master: "",
    addresses: [{ ip: "192.0.2.10", prefix: 24, family: "inet" },
                { ip: "fe80::1", prefix: 64, family: "inet6" }] },
  { name: "docker0", mac: "02:42:00:00:00:01", mtu: 1500, state: "down", master: "br-1",
    addresses: [{ ip: "172.17.0.1", prefix: 16, family: "inet" }] },
];

test("le interfacce dell'host sono una card ciascuna", () => {
  const { ctx } = conDevice([]);
  const html = ctx.deviceCard(dev({ interfaces: IFACE }));
  assert.equal((html.match(/iface-card/g) || []).length, 2);
  assert.ok(html.includes("eth0") && html.includes("docker0"));
  assert.ok(html.includes("192.0.2.10/24"), "IPv4 con il prefisso");
  assert.ok(html.includes("fe80::1/64"), "e anche IPv6");
  assert.ok(html.includes("br-1"), "l'appartenenza a un bridge e' un dato utile");
});

test("un'interfaccia spenta si riconosce", () => {
  const { ctx } = conDevice([]);
  const html = ctx.ifaceCard(IFACE[1]);
  assert.ok(html.includes("iface-card giu"));
  assert.match(CSS, /\.iface-card\.giu\s*\{/);
});

test("le righe vuote non si disegnano", () => {
  // Un'interfaccia senza IP non deve lasciare "IPv4" con il vuoto accanto.
  const { ctx } = conDevice([]);
  const html = ctx.ifaceCard({ name: "lo", state: "unknown", addresses: [], mtu: 65536 });
  assert.ok(!html.includes("IPv6"));
  assert.ok(!html.includes("master"));
  assert.ok(html.includes("65536"));
});

test("dove le interfacce non ci sono, la scheda spiega perche'", () => {
  // Si leggono via SSH: per il telefono e la stampante non ci saranno mai, e
  // un riquadro vuoto sembrerebbe un difetto.
  const { ctx } = conDevice([]);
  const html = ctx.deviceCard(dev({ interfaces: [] }));
  assert.ok(html.includes("via SSH"));
  assert.ok(html.includes('data-vai="settings"'), "e porta dove si configurano");
  assert.ok(!html.includes("iface-card"));
});

/* ── I placeholder degli indirizzi vengono dalla config ─────────────
   Un indirizzo scritto a mano nel frontend contraddice la regola del
   progetto ("niente IP nel codice ne' nel frontend") e suggerisce a chi
   installa una rete che non e' la sua. I due modali dei dispositivi lo
   facevano entrambi: uno con un indirizzo di documentazione, l'altro con
   "192.168.x.x". */

// I modali si costruiscono con document.createElement: si intercetta la
// creazione per leggere l'HTML che ci finisce dentro.
function htmlModale(ctx, apri) {
  const creati = [];
  const originale = ctx.document.createElement;
  ctx.document.createElement = () => { const e = elemento(); creati.push(e); return e; };
  try { apri(); } finally { ctx.document.createElement = originale; }
  return creati.map(e => e.innerHTML).join("");
}

function conSubnet(cidr) {
  const { ctx } = contesto();
  ctx.__interni.state.snap = { meta: { subnets: cidr ? [{ cidr, label: "LAN" }] : [] } };
  return ctx;
}

test("il placeholder dell'IP segue la subnet configurata", () => {
  const ctx = conSubnet("10.20.30.0/24");
  for (const [nome, apri] of [["modifica", () => ctx.openEditModal(null, "k")],
                              ["aggiungi", () => ctx.openAddModal()]]) {
    const html = htmlModale(ctx, apri);
    assert.ok(html.includes('placeholder="10.20.30.10"'),
      `modale ${nome}: l'esempio deve venire dalla config, non dal codice`);
  }
});

test("senza subnet configurate il placeholder resta vuoto", () => {
  // Meglio nessun suggerimento che uno inventato: e' la stessa regola per cui
  // una sorgente non disponibile non produce dati finti.
  const ctx = conSubnet("");
  for (const [nome, apri] of [["modifica", () => ctx.openEditModal(null, "k")],
                              ["aggiungi", () => ctx.openAddModal()]]) {
    const html = htmlModale(ctx, apri);
    assert.ok(!/placeholder="\d+\.\d+\.\d+\.\d+"/.test(html),
      `modale ${nome}: nessun indirizzo inventato senza config`);
  }
});

test("nessun indirizzo privato scritto a mano in app.js", () => {
  // La riga "192.168.x.x" era sfuggita al controllo dei dati personali, che
  // cercava 192.168 seguito da una cifra. Qui si guarda il sorgente intero.
  const righe = SORGENTE.split("\n")
    .filter(r => /\b(10|192\.168|172\.(1[6-9]|2\d|3[01])|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.[\dx]+\.[\dx]+\b/.test(r))
    .filter(r => !r.trim().startsWith("*") && !r.trim().startsWith("//"));
  assert.deepEqual(righe, [], "indirizzi privati nel codice: devono venire dalla config");
});
