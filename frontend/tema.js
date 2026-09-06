/* ===================================================================
   LANMng — scelta del tema, prima che la pagina venga dipinta

   Deve girare PRIMA del foglio di stile: se lo facesse app.js, che sta in
   fondo al body, ad ogni caricamento si vedrebbe un lampo del tema
   sbagliato. E deve essere un file a parte, non uno script inline nella
   pagina: il vhost manda `Content-Security-Policy: default-src 'self'`,
   che l'inline lo rifiuta in silenzio (visto sull'istanza, 0.1.80 —
   funzionava in locale ad aprire il file, non da nginx).

   Qui c'e' solo la scelta: l'elenco dei temi validi e il selettore stanno
   in app.js, i colori in styles.css. Un valore sconosciuto non fa danni,
   semplicemente non corrisponde a nessun blocco e resta il dark.
   =================================================================== */
(function () {
  var t = null;
  // Il localStorage puo' essere inaccessibile (finestra privata, storage
  // bloccato dal browser): senza scelta salvata si segue il sistema.
  try { t = window.localStorage.getItem("lanmng.tema"); } catch (e) { t = null; }
  if (!t) t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
    ? "chiaro" : "dark";
  document.documentElement.setAttribute("data-tema", t);
})();
