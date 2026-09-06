/* ===================================================================
   i18n.test.mjs — Motore di traduzione e cataloghi

   Due proprieta' che non si vedono guardando la pagina:
     1. i due cataloghi devono avere le STESSE chiavi. Una chiave che
        esiste solo in italiano funziona finche' si guarda in italiano,
        poi in inglese ripiega e nessuno se ne accorge finche' non lo
        segnala un utente;
     2. una chiave mancante non deve diventare stringa vuota. Un buco in
        pagina non si nota; una chiave visibile si corregge subito.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

import { SORGENTE_APP } from "./sorgente.mjs";

const leggi = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const MOTORE = leggi("../i18n.js");
const CAT_EN = leggi("../i18n/en.js");
const CAT_IT = leggi("../i18n/it.js");
const INDEX = leggi("../index.html");
const APP = leggi("../app.js");

/* Contesto minimo: nessun localStorage e nessun navigator, cioe' il caso
   peggiore. Il motore deve reggerlo senza esplodere. */
function motore(extra = {}) {
  const ctx = createContext(Object.assign({
    console, Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp, Intl,
  }, extra));
  runInContext(MOTORE + "\n" + CAT_EN + "\n" + CAT_IT, ctx);
  return ctx;
}

function chiavi(sorgente) {
  const dentro = sorgente.slice(sorgente.indexOf("{"), sorgente.lastIndexOf("}"));
  return new Set([...dentro.matchAll(/^\s*"([^"]+)":/gm)].map(m => m[1]));
}

// ── I cataloghi ────────────────────────────────────────────────────

test("i due cataloghi hanno esattamente le stesse chiavi", () => {
  const en = chiavi(CAT_EN), it = chiavi(CAT_IT);
  assert.deepEqual([...it].filter(k => !en.has(k)), [],
    "chiavi solo in italiano: in inglese ripiegherebbero in silenzio");
  assert.deepEqual([...en].filter(k => !it.has(k)), [],
    "chiavi solo in inglese: in italiano comparirebbe il testo inglese");
  assert.ok(en.size > 50, "il catalogo sembra troppo piccolo, controlla il parsing");
});

test("ogni chiave di plurale ha entrambe le forme", () => {
  for (const [nome, sorgente] of [["en", CAT_EN], ["it", CAT_IT]]) {
    const ks = [...chiavi(sorgente)];
    for (const k of ks.filter(x => x.endsWith("_one"))) {
      assert.ok(ks.includes(k.replace(/_one$/, "_other")),
        `${nome}: manca la forma plurale di ${k}`);
    }
    for (const k of ks.filter(x => x.endsWith("_other"))) {
      assert.ok(ks.includes(k.replace(/_other$/, "_one")),
        `${nome}: manca la forma singolare di ${k}`);
    }
  }
});

test("nessun catalogo contiene indirizzi o nomi di macchine", () => {
  // Il catalogo e' testo dell'interfaccia: un indirizzo qui dentro finirebbe
  // in pagina e nell'export pubblico.
  for (const [nome, sorgente] of [["en", CAT_EN], ["it", CAT_IT]]) {
    assert.ok(!/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/.test(sorgente),
      `${nome}: c'e' un indirizzo IP nel catalogo`);
  }
});

// ── Il motore ──────────────────────────────────────────────────────

test("una chiave mancante si vede, non sparisce", () => {
  const ctx = motore();
  assert.equal(ctx.t("chiave.che.non.esiste"), "chiave.che.non.esiste");
});

test("una chiave presente solo in inglese ripiega invece di sparire", () => {
  const ctx = motore();
  runInContext('I18N.registra("en", { "prova.solo.en": "only english" });', ctx);
  ctx.I18N.setLang("it");
  assert.equal(ctx.t("prova.solo.en"), "only english");
});

test("i parametri vengono sostituiti, e quelli mancanti restano visibili", () => {
  const ctx = motore();
  runInContext('I18N.registra("en", { "p": "{a} e {b}" });', ctx);
  assert.equal(ctx.t("p", { a: "uno", b: "due" }), "uno e due");
  assert.equal(ctx.t("p", { a: "uno" }), "uno e {b}",
    "un parametro mancante deve saltare all'occhio");
});

test("il plurale segue le regole della lingua, non un ternario", () => {
  const ctx = motore();
  ctx.I18N.setLang("it");
  assert.equal(ctx.tp("conta.spento", 1), "1 spento");
  assert.equal(ctx.tp("conta.spento", 3), "3 spenti");
  ctx.I18N.setLang("en");
  assert.equal(ctx.tp("conta.spento", 1), "1 down");
  assert.equal(ctx.tp("conta.spento", 0), "0 down");
});

test("senza localStorage ne' navigator il motore parte lo stesso", () => {
  // E' il caso della navigazione privata e degli storage bloccati: se il
  // motore esplodesse qui, la pagina resterebbe bianca.
  const ctx = motore();
  assert.equal(ctx.I18N.lang, "en", "senza indizi si ripiega sull'inglese");
  assert.equal(typeof ctx.t, "function");
});

test("la lingua del browser viene riconosciuta, quella ignota no", () => {
  assert.equal(motore({ navigator: { languages: ["it-IT", "en"] } }).I18N.lang, "it");
  assert.equal(motore({ navigator: { languages: ["de-DE"] } }).I18N.lang, "en");
  assert.equal(motore({ navigator: { language: "it" } }).I18N.lang, "it");
});

test("la scelta esplicita vince sul browser", () => {
  const salvato = { "lanmng-lingua": "en" };
  const ctx = motore({
    navigator: { languages: ["it-IT"] },
    localStorage: { getItem: (k) => salvato[k] ?? null, setItem() {} },
  });
  assert.equal(ctx.I18N.lang, "en", "chi ha scelto non deve vedersi cambiare lingua");
  assert.equal(ctx.I18N.sceltaEsplicita, true);
});

test("un localStorage che lancia non ferma la pagina", () => {
  const ctx = motore({
    localStorage: { getItem() { throw new Error("bloccato"); },
                    setItem() { throw new Error("bloccato"); } },
  });
  assert.equal(typeof ctx.t, "function");
  ctx.I18N.setLang("it");             // non deve propagare l'eccezione
  assert.equal(ctx.I18N.lang, "it");
});

// ── Aggancio con la pagina ─────────────────────────────────────────

test("index.html carica i18n prima dei cataloghi e i cataloghi prima di app.js", () => {
  const i = (s) => INDEX.indexOf(s);
  assert.ok(i("i18n.js?v=") > 0, "index.html non carica il motore");
  assert.ok(i("i18n.js?v=") < i("i18n/en.js?v="), "il motore va prima dei cataloghi");
  assert.ok(i("i18n/en.js?v=") < i("app.js?v="), "i cataloghi vanno prima di app.js");
  assert.ok(i("i18n/it.js?v=") < i("app.js?v="));
});

test("ogni data-i18n di index.html punta a una chiave che esiste", () => {
  const en = chiavi(CAT_EN);
  const usate = [...INDEX.matchAll(/data-i18n="([^"]+)"/g)].map(m => m[1]);
  const attrs = [...INDEX.matchAll(/data-i18n-attr="([^"]+)"/g)]
    .flatMap(m => m[1].split(",").map(x => x.split(":")[1].trim()));
  assert.ok(usate.length >= 15, "le voci di menu non sono marcate");
  for (const k of [...usate, ...attrs]) {
    assert.ok(en.has(k), `data-i18n="${k}" non esiste nel catalogo`);
  }
});

test("ogni chiave usata da app.js esiste nel catalogo", () => {
  // Le chiamate a chiave letterale si controllano qui; quelle costruite a
  // runtime (conta(n, chiave)) passano dalle costanti gia' verificate sopra.
  const en = chiavi(CAT_EN);
  const usate = new Set([...APP.matchAll(/\bt\("([a-z][\w.]*\.[\w.]+)"/g)].map(m => m[1]));
  const mancanti = [...usate].filter(k => !en.has(k));
  assert.deepEqual(mancanti, [], "chiavi usate in app.js ma assenti dal catalogo");
  assert.ok(usate.size > 10, "il conteggio sembra sbagliato, controlla la regex");
});

test("cambiare lingua cambia davvero il testo reso dalla SPA", () => {
  // Prova end-to-end del giro: catalogo -> t() -> stringa usata da app.js.
  const ctx = createContext({
    console, Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp, Intl,
    Map, Set, Promise, document: { addEventListener() {}, documentElement: {},
      querySelectorAll: () => [] },
    window: { addEventListener() {} }, location: { hash: "" },
    setTimeout: () => 0, setInterval: () => 0, AbortController,
  });
  runInContext(SORGENTE_APP, ctx);
  ctx.I18N.setLang("it");
  const it = ctx.durata(Math.floor(Date.now() / 1000) - 3600);
  ctx.I18N.setLang("en");
  const en = ctx.durata(Math.floor(Date.now() / 1000) - 3600);
  assert.equal(it, "1h");
  assert.equal(en, "1h");
  ctx.I18N.setLang("it");
  assert.equal(ctx.t("tempo.mai"), "mai");
  ctx.I18N.setLang("en");
  assert.equal(ctx.t("tempo.mai"), "never");
});

// ── Lingua predefinita del servizio ────────────────────────────────

test("la lingua della config si applica a chi non ha scelto", () => {
  const ctx = motore({ document: { documentElement: {}, querySelectorAll: () => [] } });
  ctx.I18N.sceltaEsplicita = false;
  runInContext(ESTRATTO, ctx);
  assert.equal(ctx.applicaLinguaPredefinita({ default_language: "it" }), true);
  assert.equal(ctx.I18N.lang, "it");
});

test("la lingua della config NON scavalca chi ha scelto", () => {
  // Sarebbe il difetto peggiore: la lingua cambierebbe sotto le mani ad ogni
  // ricaricamento, e chi l'ha scelta non capirebbe perche'.
  const ctx = motore({ document: { documentElement: {}, querySelectorAll: () => [] } });
  ctx.I18N.setLang("en");
  ctx.I18N.sceltaEsplicita = true;
  runInContext(ESTRATTO, ctx);
  assert.equal(ctx.applicaLinguaPredefinita({ default_language: "it" }), false);
  assert.equal(ctx.I18N.lang, "en", "la scelta dell'utente e' stata scavalcata");
});

test("senza lingua nella config non cambia niente", () => {
  const ctx = motore({ document: { documentElement: {}, querySelectorAll: () => [] } });
  runInContext(ESTRATTO, ctx);
  assert.equal(ctx.applicaLinguaPredefinita({}), false);
  assert.equal(ctx.applicaLinguaPredefinita(null), false);
});

/* La funzione si preleva dal sorgente vero: copiarla qui vorrebbe dire
   provare una copia, che puo' divergere da quella che gira. */
const ESTRATTO = (() => {
  const i = APP.indexOf("function applicaLinguaPredefinita");
  const f = APP.indexOf("\nfunction onData()");
  return APP.slice(i, f);
})();
