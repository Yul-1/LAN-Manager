/* ===================================================================
   i18n-copertura.test.mjs — Quante stringhe restano scritte a mano

   La conversione all'i18n si fa su un file da 5500 righe: "mi sembra
   di averle prese tutte" non e' una verifica. Questo test le conta.

   Trova i testi in italiano che finiscono nella pagina SENZA passare
   da `t()`. Il tetto qui sotto e' un debito dichiarato: puo' solo
   scendere. Quando arriva a zero, la UI e' davvero bilingue e la riga
   del tetto si toglie insieme al test.
   =================================================================== */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");

/* Il debito e' chiuso: la conversione e' completa. Il tetto resta a zero e non
   si rialza — una stringa nuova scritta a mano fa fallire il test, che e'
   esattamente cio' che serve perche' la UI resti bilingue. */
const TETTO = 0;

/* Marcatori di prosa italiana. Non e' un riconoscitore di lingua: e'
   una rete che prende il testo dell'interfaccia e lascia passare
   identificatori, chiavi e valori CSS. */
const ITALIANO = /(?:\bil |\bla |\ble |\bgli |\bun |\buna |\bdi |\bdel |\bche |\bnon |\bper |\bcon |\bsono |questo|questa|quando|\bpiu'|\bpuo'|\be' |\bda |\bnel |\balla |\bcome |\bsolo |\banche |\bogni |\bdopo |\bprima |\bsenza |\bgia'|\bnessun|\bmai\b)/i;

const CHIAVE = /^(?:alert|azione|chiedi|comune|conta|dash|device|err|grafico|host|impostazioni|lingua|login|logs|msg|nav|pagina|risorse|servizi|setup|sicurezza|stato|stats|tema|tempo|term|tools|topbar|wan|wg)\./;

function senzaCommenti(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function daTradurre(src) {
  const testo = senzaCommenti(src);
  const trovate = new Set();
  const utile = (v) => {
    v = (v || "").trim();
    if (v.length < 4) return false;
    if (CHIAVE.test(v)) return false;                 // e' gia' una chiave
    if (/^[a-z0-9_-]+$/.test(v)) return false;        // identificatore
    if (/^(https?:|\/api|var\(|#|\.|\$)/.test(v)) return false;
    // Nomi di tasti e di eventi: sono valori dell'API del browser, non testo.
    if (/^(Enter|Escape|Tab|Shift|Control|Alt|Backspace|Delete|Arrow\w+)$/.test(v)) return false;
    // Identificatori tecnici: uguali in ogni lingua, tradurli sarebbe un errore.
    // Nomi di errori del browser e intestazioni HTTP...
    if (/^(AbortError|TypeError|Retry-After|Content-Type|Accept-Language)$/.test(v)) return false;
    // ...unita' di misura e nomi di sistemi operativi.
    if (/^([KMG]bit\/s|Windows|Linux|Docker|WireGuard)$/.test(v)) return false;
    return ITALIANO.test(v) || /^[A-ZÀ-Ù][a-zà-ù]{3,}/.test(v);
  };
  for (const m of testo.matchAll(/"([^"\\\n]{4,})"/g)) if (utile(m[1])) trovate.add(m[1].trim());
  for (const m of testo.matchAll(/>([^<>{}\n]{4,})</g)) {
    // Le chiavi gia' risolte (`${h(t("x.y"))}`) lasciano il nome della chiave
    // dentro il pattern: e' testo gia' tradotto, non da tradurre.
    if (utile(m[1]) && !/^[a-z]+\.[a-zA-Z]/.test(m[1].trim())) trovate.add(m[1].trim());
  }
  return [...trovate];
}

test("nessun testo scritto a mano in app.js", () => {
  const restanti = daTradurre(APP);
  assert.deepEqual(restanti, [],
    "testo dell'interfaccia scritto a mano: deve passare da t() e stare nei cataloghi");
});



test("i file gia' convertiti restano puliti", () => {
  // Cornice, accesso e primo avvio sono stati convertiti per primi perche'
  // sono cio' che si vede prima di ogni altra cosa: qui non deve tornare
  // testo scritto a mano.
  const i = APP.indexOf("async function renderSetup()");
  const f = APP.indexOf("document.addEventListener(\"DOMContentLoaded\"");
  assert.ok(i > 0 && f > i, "le funzioni di avvio non si trovano piu'");
  const restanti = daTradurre(APP.slice(i, f));
  assert.deepEqual(restanti, [], "testo italiano tornato nelle schermate d'avvio");
});

test("nessuna variabile locale si chiama `t`", () => {
  // `t` e' la funzione di traduzione globale. Una `const t` locale la oscura in
  // TUTTO il blocco, comprese le righe che la precedono (temporal dead zone):
  // l'errore non compare dove si dichiara la variabile ma dove si traduce, e
  // in un punto che magari nessun test tocca. E' successo davvero durante la
  // conversione: la pagina Tools, la ricerca nei log e la navigazione degli
  // alert si erano rotte tutte insieme.
  const decl = [...senzaCommenti(APP).matchAll(/\b(?:const|let|var)\s+t\s*=/g)];
  assert.equal(decl.length, 0, "una variabile locale chiamata `t` oscura la traduzione");

  // Stessa cosa per i parametri di funzione: `(t) => ...` non e' in temporal
  // dead zone, ma dentro il suo corpo `t` non e' piu' la traduzione.
  const par = [...senzaCommenti(APP).matchAll(/\(\s*t\s*\)\s*=>/g)];
  assert.equal(par.length, 0, "un parametro chiamato `t` oscura la traduzione");
});
