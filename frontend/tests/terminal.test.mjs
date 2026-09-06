/* ===================================================================
   terminal.test.mjs — Emulatore ANSI (frontend/terminal.js)

   Girano con il runner integrato di node:  node --test frontend/tests
   Nessun package.json, nessun npm install. Su homeserver node non c'e' e
   scripts/test-js.sh esce 0 dichiarandolo: sono test di sviluppo locale.

   Le asserzioni guardano la griglia `term.active`, dove ogni cella e'
   { c, f, b, a } = carattere, colore testo, colore sfondo, attributi.
   =================================================================== */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

import { creaContesto, creaElemento } from "./dom-shim.mjs";

const SORGENTE = readFileSync(new URL("../terminal.js", import.meta.url), "utf8");

const ESC = "\x1b";
const A_BOLD = 1, A_INVERSE = 16;

function nuovoTerminale(opts = {}) {
  const ctx = createContext(creaContesto());
  runInContext(SORGENTE, ctx);
  return new ctx.window.Terminal(creaElemento(), opts);
}

/* Testo di una riga, senza gli spazi di riempimento a destra. */
const riga = (t, y) => t.active[y].map((c) => c.c).join("").replace(/\s+$/, "");

/* ── Scrittura di base ───────────────────────────────────────── */

test("scrive testo semplice sulla prima riga", () => {
  const t = nuovoTerminale();
  t.write("ciao");
  assert.equal(riga(t, 0), "ciao");
  assert.equal(t.x, 4);
  assert.equal(t.y, 0);
});

test("il ritorno a capo porta a inizio riga successiva", () => {
  const t = nuovoTerminale();
  t.write("uno\r\ndue");
  assert.equal(riga(t, 0), "uno");
  assert.equal(riga(t, 1), "due");
  assert.equal(t.x, 3);
});

test("il testo va a capo da solo a fine riga", () => {
  const t = nuovoTerminale();
  t.write("x".repeat(t.cols + 3));
  assert.equal(riga(t, 0).length, t.cols);
  assert.equal(riga(t, 1), "xxx");
});

test("il backspace arretra il cursore", () => {
  const t = nuovoTerminale();
  t.write("abc\b\b");
  assert.equal(t.x, 1);
});

test("il tab porta alla tabulazione successiva", () => {
  const t = nuovoTerminale();
  t.write("ab\tc");
  assert.equal(t.x, 9);
});

/* ── Movimento del cursore ───────────────────────────────────── */

test("CUP porta il cursore in una posizione assoluta", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[5;10Hqui`);
  assert.equal(riga(t, 4).trimStart(), "qui");
  assert.equal(t.y, 4);
});

test("CUP senza parametri torna in alto a sinistra", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[5;10H${ESC}[Hx`);
  assert.equal(t.y, 0);
  assert.equal(riga(t, 0), "x");
});

test("il cursore non esce dalla griglia", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[999;999H`);
  assert.ok(t.y <= t.rows - 1);
  assert.ok(t.x <= t.cols - 1);
});

test("CUF e CUB spostano il cursore in orizzontale", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[5Cx`);
  assert.equal(riga(t, 0), "     x");
  t.write(`${ESC}[3Dy`);
  assert.equal(t.x, 4);
});

/* ── Cancellazioni ───────────────────────────────────────────── */

test("EL 0 cancella da cursore a fine riga", () => {
  const t = nuovoTerminale();
  t.write(`abcdef${ESC}[3D${ESC}[0K`);
  assert.equal(riga(t, 0), "abc");
});

test("EL 1 cancella da inizio riga al cursore", () => {
  const t = nuovoTerminale();
  t.write(`abcdef${ESC}[3D${ESC}[1K`);
  assert.equal(riga(t, 0).slice(0, 4), "    ");
  assert.equal(riga(t, 0).trim(), "ef");
});

test("EL 2 cancella tutta la riga", () => {
  const t = nuovoTerminale();
  t.write(`abcdef${ESC}[2K`);
  assert.equal(riga(t, 0), "");
});

test("ED 2 pulisce lo schermo", () => {
  const t = nuovoTerminale();
  t.write(`uno\r\ndue\r\ntre${ESC}[2J`);
  for (let y = 0; y < 3; y += 1) assert.equal(riga(t, y), "");
});

/* ── Attributi e colori ──────────────────────────────────────── */

test("SGR 1 marca il grassetto", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[1mG`);
  assert.equal(t.active[0][0].a & A_BOLD, A_BOLD);
});

test("SGR 0 azzera tutti gli attributi", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[1;31;7mA${ESC}[0mB`);
  const dopo = t.active[0][1];
  assert.equal(dopo.a, 0);
  assert.equal(dopo.f, null);
  assert.equal(dopo.b, null);
});

test("i colori base impostano testo e sfondo", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[31;42mX`);
  const cella = t.active[0][0];
  assert.ok(cella.f, "colore del testo non impostato");
  assert.ok(cella.b, "colore di sfondo non impostato");
});

test("i 256 colori vengono interpretati", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[38;5;196mR`);
  assert.ok(t.active[0][0].f);
});

test("SGR 7 marca il video inverso", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[7mX`);
  assert.equal(t.active[0][0].a & A_INVERSE, A_INVERSE);
});

/* ── Scroll e scrollback ─────────────────────────────────────── */

test("superata l'ultima riga il contenuto scorre e finisce nello scrollback", () => {
  const t = nuovoTerminale();
  for (let i = 0; i < t.rows + 3; i += 1) t.write(`riga${i}\r\n`);
  assert.ok(t.scrollback.length >= 3);
  assert.equal(riga(t, t.rows - 1), "");
  assert.equal(t.scrollback[0].map((c) => c.c).join("").trim(), "riga0");
});

test("lo scrollback non supera il tetto configurato", () => {
  const t = nuovoTerminale({ scrollback: 10 });
  for (let i = 0; i < 100; i += 1) t.write(`r${i}\r\n`);
  assert.ok(t.scrollback.length <= 10, `scrollback = ${t.scrollback.length}`);
});

test("DECSTBM limita lo scorrimento a una regione", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[2;4r`);
  assert.equal(t.scrollTop, 1);
  assert.equal(t.scrollBot, 3);
});

/* ── Buffer alternativo ──────────────────────────────────────── */

test("il buffer alternativo isola il contenuto e poi lo restituisce", () => {
  // E' il meccanismo con cui top, htop e vim non cancellano la sessione.
  const t = nuovoTerminale();
  t.write("prima");
  t.write(`${ESC}[?1049h`);
  assert.notEqual(t.alt, null);
  assert.equal(riga(t, 0), "");
  t.write("dentro");
  assert.equal(riga(t, 0), "dentro");
  t.write(`${ESC}[?1049l`);
  assert.equal(t.alt, null);
  assert.equal(riga(t, 0), "prima");
});

test("nel buffer alternativo non si accumula scrollback", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[?1049h`);
  const prima = t.scrollback.length;
  for (let i = 0; i < t.rows + 5; i += 1) t.write(`r${i}\r\n`);
  assert.equal(t.scrollback.length, prima);
});

/* ── Modalita' private ───────────────────────────────────────── */

test("il cursore si nasconde e si rimostra", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[?25l`);
  assert.equal(t.cursorVisible, false);
  t.write(`${ESC}[?25h`);
  assert.equal(t.cursorVisible, true);
});

test("le frecce in modalita' applicazione si attivano", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[?1h`);
  assert.equal(t.appCursor, true);
});

test("il bracketed paste si attiva", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[?2004h`);
  assert.equal(t.bracketedPaste, true);
});

/* ── Robustezza del parser ───────────────────────────────────── */

test("una sequenza spezzata fra due write viene ricomposta", () => {
  // Con un PTY l'output arriva a pezzi: il parser deve conservare lo stato.
  const t = nuovoTerminale();
  t.write(`${ESC}[3`);
  t.write("1mR");
  assert.ok(t.active[0][0].f, "il colore si e' perso fra i due write");
  assert.equal(riga(t, 0), "R");
});

test("una sequenza sconosciuta non stampa caratteri spuri", () => {
  const t = nuovoTerminale();
  t.write(`${ESC}[99999zciao`);
  assert.equal(riga(t, 0), "ciao");
});

test("un ESC isolato non lascia il parser bloccato", () => {
  // Il carattere subito dopo l'ESC viene consumato come finale della sequenza
  // (ESC c e' il reset VT100): e' il comportamento giusto, lo fa anche xterm.
  // Quello che conta e' che il parser torni a "text" e non ingoi tutto il resto.
  const t = nuovoTerminale();
  t.write(`${ESC}`);
  assert.equal(t.parse.state, "esc");
  t.write("ciao");
  assert.equal(t.parse.state, "text");
  assert.equal(riga(t, 0), "iao");
});

/* ── Dimensioni ──────────────────────────────────────────────── */

test("fit calcola righe e colonne dalle misure della cella", () => {
  const t = nuovoTerminale();
  assert.equal(t.cols, 100);
  assert.equal(t.rows, 30);
});

test("il ridimensionamento non perde il contenuto", () => {
  const t = nuovoTerminale();
  t.write("contenuto");
  t.resize(60, 20);
  assert.equal(t.cols, 60);
  assert.equal(t.rows, 20);
  assert.equal(riga(t, 0), "contenuto");
});

test("resize non scende sotto le dimensioni minime imposte dal contenuto", () => {
  const t = nuovoTerminale();
  t.write("x".repeat(50));
  t.resize(20, 5);
  assert.equal(t.active[0].length, 20);
  assert.equal(t.active.length, 5);
});
