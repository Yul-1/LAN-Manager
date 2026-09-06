/* ===================================================================
   mobile.test.mjs — navigazione da telefono

   Sotto i 760px la sidebar sta fuori schermo e l'unico modo di raggiungerla
   e' l'hamburger nell'header. In origine il CSS la nascondeva e
   basta: da telefono si restava bloccati sulla pagina di partenza.

   Qui si verifica la parte con logica, non il CSS: l'hamburger apre e chiude
   il drawer, scegliere una voce lo richiude (altrimenti coprirebbe la pagina
   appena aperta), lo scrim e il tasto Esc lo chiudono, e il fuoco da tastiera
   entra nel drawer e torna all'hamburger.
   =================================================================== */
import { readFileSync } from "node:fs";
import { SORGENTE_APP } from "./sorgente.mjs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = SORGENTE_APP
  + "\n;globalThis.__interni = { state, PAGES };";

/* classList vera: il drawer e' aperto o chiuso in base a una classe sul body,
   quindi un finto no-op renderebbe il test cieco proprio sull'oggetto in esame. */
function classList(el) {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => { const v = on === undefined ? !set.has(c) : on;
      v ? set.add(c) : set.delete(c); return v; },
    _set: set,
  };
}

function elemento(nome = "", dentroSidebar = false) {
  const el = {
    nome, className: "", textContent: "", value: "", checked: false,
    dataset: {}, style: {}, hidden: true, tabIndex: -1,
    attributi: {}, fuochi: 0, _dentroSidebar: dentroSidebar,
    innerHTML: "",
    setAttribute(k, v) { this.attributi[k] = v; },
    getAttribute(k) { return this.attributi[k]; },
    // closest(".sidebar") decide se il fuoco va riportato all'hamburger.
    closest(sel) { return sel === ".sidebar" && this._dentroSidebar ? elemento("sidebar") : null; },
    querySelector: () => elemento(),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, remove() {},
    appendChild(f) { return f; },
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  el.classList = classList(el);
  return el;
}

/* DOM minimo: il body (con la classe che comanda il drawer), l'hamburger,
   lo scrim, le voci di menu e i pochi nodi che il router tocca. */
function contesto() {
  const body = elemento("body");
  const btn = elemento("nav-toggle");
  const scrim = elemento("nav-scrim");
  const voci = ["dashboard", "stats"].map((r) => {
    const a = elemento("nav-" + r, true);
    a.dataset.route = r;
    return a;
  });
  voci[0].className = "active";
  const nodi = {
    "#nav-toggle": btn, "#nav-scrim": scrim,
    "#nav a.active": voci[0], "#nav a": voci[0],
    "#view": elemento("view"), "#page-title": elemento("page-title"),
  };
  const ascoltatori = {};
  const document = {
    body,
    activeElement: null,
    querySelector: (sel) => nodi[sel] || elemento(sel),
    querySelectorAll: (sel) => (sel === "#nav a" ? voci : []),
    createElement: () => elemento(),
    addEventListener(tipo, cb) { (ascoltatori[tipo] ||= []).push(cb); },
  };
  // focus() vero: aggiorna document.activeElement, che e' l'unico modo per
  // sapere se il fuoco stava nel drawer quando lo si chiude.
  for (const el of [btn, scrim, ...voci])
    el.focus = function () { this.fuochi += 1; document.activeElement = this; };

  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1,
              matchMedia: () => ({ matches: false }) },
    document, console,
    location: { hash: "", reload() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval: () => {},
    cancelAnimationFrame: () => {},
    Date, Math, JSON, WebSocket: function () {},
  });
  return { ctx, body, btn, scrim, voci, ascoltatori };
}

/* Il router e' gia' coperto altrove: qui interessa solo l'effetto di go()
   sul drawer, quindi le pagine non vengono davvero disegnate. */
function zittisciPagine(ctx) {
  for (const p of Object.values(ctx.__interni.PAGES)) p.render = () => {};
}


test("l'hamburger apre e chiude il drawer", () => {
  const { ctx, body, btn, scrim } = contesto();
  runInContext(SORGENTE, ctx);
  ctx.bindMobileNav();

  assert.equal(body.classList.contains("nav-open"), false, "parte chiuso");
  btn.onclick();
  assert.equal(body.classList.contains("nav-open"), true,
    "toccare l'hamburger non apre il menu: da telefono non si puo' navigare");
  assert.equal(scrim.hidden, false, "lo scrim deve coprire la pagina");
  assert.equal(btn.getAttribute("aria-expanded"), "true");

  btn.onclick();
  assert.equal(body.classList.contains("nav-open"), false, "il secondo tocco richiude");
  assert.equal(scrim.hidden, true);
  assert.equal(btn.getAttribute("aria-expanded"), "false");
});


test("scegliere una voce richiude il drawer", () => {
  const { ctx, body, btn } = contesto();
  runInContext(SORGENTE, ctx);
  zittisciPagine(ctx);
  ctx.bindMobileNav();

  btn.onclick();
  ctx.go("stats");
  assert.equal(ctx.__interni.state.route, "stats", "la pagina deve cambiare");
  assert.equal(body.classList.contains("nav-open"), false,
    "il drawer resta aperto sopra la pagina appena scelta");
});


test("lo scrim e il tasto Esc chiudono il drawer", () => {
  const { ctx, body, btn, scrim, ascoltatori } = contesto();
  runInContext(SORGENTE, ctx);
  ctx.bindMobileNav();

  btn.onclick();
  scrim.onclick();
  assert.equal(body.classList.contains("nav-open"), false, "toccare fuori chiude");

  btn.onclick();
  ascoltatori.keydown.forEach((cb) => cb({ key: "Escape" }));
  assert.equal(body.classList.contains("nav-open"), false, "Esc chiude");
});


test("il fuoco entra nel drawer e torna all'hamburger", () => {
  const { ctx, btn, voci } = contesto();
  runInContext(SORGENTE, ctx);
  ctx.bindMobileNav();

  btn.onclick();
  assert.equal(voci[0].fuochi, 1, "aprendo, il fuoco va sulla voce attiva");
  btn.onclick();
  assert.equal(btn.fuochi, 1, "chiudendo, il fuoco torna all'hamburger");
});


test("chiudere il drawer non sposta il fuoco di chi non stava dentro", () => {
  const { ctx, btn } = contesto();
  runInContext(SORGENTE, ctx);
  ctx.bindMobileNav();

  // Caso desktop: il drawer non c'e', ma go() chiama comunque closeNav().
  // Se il fuoco non e' nel drawer non deve essere rubato dall'hamburger.
  ctx.setNavOpen(true);
  const altrove = elemento("campo-ricerca");
  altrove.focus = function () { this.fuochi += 1; };
  ctx.document.activeElement = altrove;
  ctx.setNavOpen(false);
  assert.equal(btn.fuochi, 0, "il fuoco e' stato spostato via da dove stava l'utente");
});
