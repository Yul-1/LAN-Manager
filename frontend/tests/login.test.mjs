/* ===================================================================
   login.test.mjs — Primo accesso e sessione

   La meccanica del 401 (pannello unico, ripresa della chiamata) sta in
   errori.test.mjs. Qui il pezzo che mancava: la
   schermata di **primo accesso**, dove si crea la password admin.

   Il difetto: la password si scriveva una volta sola, e non si vede
   mentre la si scrive. Un refuso chiudeva fuori dalla dashboard chi la
   stava creando, e per rientrare bisognava mettere le mani nei file sul
   server (secrets.env). Anche il minimo di sei caratteri si scopriva
   solo dopo il rifiuto del backend.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const SORGENTE = readFileSync(new URL("../app.js", import.meta.url), "utf8")
  + "\n;globalThis.__interni = { state };";

function elemento(extra = {}) {
  const memo = new Map();
  const el = {
    className: "", textContent: "", innerHTML: "", value: "", dataset: {}, style: {},
    hidden: false, disabled: false,
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector(sel) { if (!memo.has(sel)) memo.set(sel, elemento()); return memo.get(sel); },
    querySelectorAll: () => [],
    setAttribute() {}, addEventListener() {}, appendChild(f) { return f; },
    append() {}, remove() {}, focus() {}, closest: () => null,
    getBoundingClientRect: () => ({ width: 600, height: 200, top: 0, left: 0 }),
  };
  Object.assign(el, extra);
  return el;
}

function contesto(esiti = {}) {
  const chiamate = [];
  const nodi = new Map();
  const ricaricato = { fatto: false };
  const dammi = (sel) => { if (!nodi.has(sel)) nodi.set(sel, elemento()); return nodi.get(sel); };
  const ctx = createContext({
    window: { addEventListener() {}, devicePixelRatio: 1, matchMedia: () => ({ matches: false }) },
    document: { body: elemento(), createElement: () => elemento(), createElementNS: () => elemento(),
                querySelector: dammi, querySelectorAll: () => [], addEventListener() {} },
    console,
    location: { hash: "", host: "x", protocol: "http:", reload() { ricaricato.fatto = true; } },
    fetch: async (url, init) => {
      chiamate.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      const rotta = Object.keys(esiti).find(k => url.includes(k));
      const e = rotta ? esiti[rotta] : { ok: true, body: {} };
      return { ok: e.ok !== false, status: e.ok === false ? (e.status || 400) : 200,
               headers: { get: () => "application/json" },
               text: async () => JSON.stringify(e.ok === false ? { detail: e.detail || "" } : (e.body || {})) };
    },
    AbortController, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Map, Set, Promise, Number, String, Array, Object, Boolean,
    WebSocket: function () { this.close = () => {}; },
  });
  runInContext(SORGENTE, ctx);
  return { ctx, dammi, chiamate, ricaricato };
}

/* Apre la schermata di primo accesso e ritorna i campi. */
function primoAccesso(ctx, dammi) {
  ctx.renderLogin({ password_set: false, username: "admin" });
  return { pass: dammi("#lg-pass"), pass2: dammi("#lg-pass2"), form: dammi("#login"),
           msg: dammi("#lg-msg") };
}


test("al primo accesso la password si scrive due volte", () => {
  const { ctx, dammi } = contesto();
  ctx.renderLogin({ password_set: false, username: "admin" });
  assert.ok(ctx.document.body.innerHTML.includes("lg-pass2"),
    "senza conferma un refuso chiude fuori chi sta creando la password");
  assert.ok(ctx.document.body.innerHTML.includes("almeno 6 caratteri"),
    "e il minimo si dice prima, non dopo il rifiuto");
});

test("due password diverse non vengono nemmeno mandate", async () => {
  const { ctx, dammi, chiamate } = contesto();
  const f = primoAccesso(ctx, dammi);
  f.pass.value = "unaPassword"; f.pass2.value = "unAltra";
  await f.form.onsubmit({ preventDefault() {} });
  assert.equal(chiamate.length, 0, "niente da salvare finche' non coincidono");
  assert.match(f.msg.textContent, /non coincidono/);
});

test("una password troppo corta si ferma qui, col motivo", async () => {
  const { ctx, dammi, chiamate } = contesto();
  const f = primoAccesso(ctx, dammi);
  f.pass.value = "corta"; f.pass2.value = "corta";
  await f.form.onsubmit({ preventDefault() {} });
  assert.equal(chiamate.length, 0);
  assert.match(f.msg.textContent, /minimo 6/);
});

test("con le due uguali si crea la password e si entra", async () => {
  const { ctx, dammi, chiamate, ricaricato } = contesto();
  const f = primoAccesso(ctx, dammi);
  // Nel browser il campo utente nasce con il valore dello stato; il DOM finto
  // non legge gli attributi, quindi lo si scrive a mano.
  dammi("#lg-user").value = "admin";
  f.pass.value = "passwordBuona"; f.pass2.value = "passwordBuona";
  await f.form.onsubmit({ preventDefault() {} });
  assert.deepEqual(chiamate.map(c => c.url.replace(/^.*\/api/, "/api")),
                   ["/api/auth/password", "/api/auth/login"]);
  assert.equal(chiamate[1].body.username, "admin");
  assert.equal(ricaricato.fatto, true, "entrati: la dashboard si ricarica");
});

test("se la creazione della password fallisce non si tenta il login", async () => {
  const { ctx, dammi, chiamate } = contesto({
    "/api/auth/password": { ok: false, status: 400, detail: "password troppo corta" } });
  const f = primoAccesso(ctx, dammi);
  f.pass.value = "passwordBuona"; f.pass2.value = "passwordBuona";
  await f.form.onsubmit({ preventDefault() {} });
  assert.equal(chiamate.length, 1, "un login con una password mai salvata fallirebbe e basta");
  assert.match(f.msg.textContent, /troppo corta/);
});

test("all'accesso normale la conferma non c'e' e la password non si ricrea", async () => {
  const { ctx, dammi, chiamate } = contesto();
  ctx.renderLogin({ password_set: true, username: "admin" });
  assert.ok(!ctx.document.body.innerHTML.includes("lg-pass2"));
  dammi("#lg-pass").value = "qualunque";
  await dammi("#login").onsubmit({ preventDefault() {} });
  assert.deepEqual(chiamate.map(c => c.url.replace(/^.*\/api/, "/api")), ["/api/auth/login"]);
});

test("credenziali rifiutate: si resta sul form, col messaggio del backend", async () => {
  const { ctx, dammi, ricaricato } = contesto({
    "/api/auth/login": { ok: false, status: 401, detail: "credenziali non valide" } });
  ctx.renderLogin({ password_set: true, username: "admin" });
  dammi("#lg-pass").value = "sbagliata";
  await dammi("#login").onsubmit({ preventDefault() {} });
  assert.equal(ricaricato.fatto, false);
  assert.match(dammi("#lg-msg").textContent, /credenziali non valide/);
});
