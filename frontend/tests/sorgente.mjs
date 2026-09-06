/* Sorgente della SPA per i test: i18n, cataloghi e app.js concatenati nello
   STESSO ordine di index.html. Senza i cataloghi `t()` non esiste e qualunque
   render esplode; con un ordine diverso esisterebbe ma vuoto.

   La lingua e' fissata a "it" in coda: la suite asserisce sul testo italiano, e
   lasciarla dedurre dal `navigator` (assente nel vm) la farebbe dipendere da
   dove gira. I test che verificano la scelta della lingua la impostano da soli
   sul contesto. */
import { readFileSync } from "node:fs";

const leggi = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

export const SORGENTE_APP = [
  leggi("../i18n.js"),
  leggi("../i18n/en.js"),
  leggi("../i18n/it.js"),
  leggi("../app.js"),
  ';if (typeof I18N === "object") I18N.lang = "it";',
].join("\n");
