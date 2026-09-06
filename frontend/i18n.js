/* ===================================================================
   i18n.js — Traduzione dell'interfaccia

   Caricato PRIMA di app.js (come tema.js), cosi' `t()` esiste gia'
   quando parte il primo render: caricarlo dopo farebbe comparire per un
   istante le chiavi al posto del testo.

   I cataloghi sono <script> e non fetch(): la CSP del vhost e'
   `default-src 'self'` e la dashboard deve funzionare anche senza
   internet. Nessun build step, come il resto del frontend.
   =================================================================== */
(function () {
  "use strict";

  const CHIAVE_LS = "lanmng-lingua";
  const LINGUE = ["en", "it"];
  const RIPIEGO = "en";

  /* La lingua scelta a mano vince su tutto e resta: e' una preferenza del
     browser di chi guarda, non della macchina. Poi il browser, poi inglese. */
  function lingueDelBrowser() {
    const nav = typeof navigator === "object" ? navigator : {};
    const elenco = (nav.languages && nav.languages.length ? nav.languages
                    : [nav.language || ""]);
    for (const l of elenco) {
      const corta = String(l || "").toLowerCase().split("-")[0];
      if (LINGUE.includes(corta)) return corta;
    }
    return "";
  }

  function leggiSalvata() {
    try {
      const v = localStorage.getItem(CHIAVE_LS);
      return LINGUE.includes(v) ? v : "";
    } catch (e) {
      // Contesti dove localStorage esiste ma lancia (private browsing,
      // storage bloccato): non e' un motivo per non mostrare la pagina.
      return "";
    }
  }

  const I18N = {
    lang: RIPIEGO,
    cataloghi: {},          // riempito dai file en.js / it.js
    disponibili: LINGUE,
  };

  /* Registrazione di un catalogo. I file di lingua chiamano questa. */
  I18N.registra = function (lingua, voci) {
    I18N.cataloghi[lingua] = Object.assign(I18N.cataloghi[lingua] || {}, voci);
  };

  /* Chiave -> testo. Sostituisce {nome} con params.nome.

     Una chiave mancante NON diventa stringa vuota: si mostra la chiave.
     Un buco silenzioso in pagina non si nota e resta li' per mesi; una
     chiave visibile si vede subito e si corregge. */
  I18N.t = function (chiave, params) {
    const cat = I18N.cataloghi[I18N.lang] || {};
    const rip = I18N.cataloghi[RIPIEGO] || {};
    let testo = cat[chiave];
    if (testo === undefined) testo = rip[chiave];
    if (testo === undefined) return chiave;
    if (params) {
      testo = String(testo).replace(/\{(\w+)\}/g, (intero, nome) =>
        (params[nome] === undefined ? intero : String(params[nome])));
    }
    return testo;
  };

  /* Plurale. Il catalogo dichiara `chiave_one` e `chiave_other`; si passa
     `n` e si sceglie con Intl.PluralRules, che conosce le regole della
     lingua invece di dare per scontato che siano due come in italiano. */
  I18N.tp = function (chiave, n, params) {
    let forma = n === 1 ? "one" : "other";
    try {
      forma = new Intl.PluralRules(I18N.locale()).select(n);
    } catch (e) { /* Intl assente o locale invalido: resta la regola binaria */ }
    const p = Object.assign({ n: n }, params);
    const cat = I18N.cataloghi[I18N.lang] || {};
    const rip = I18N.cataloghi[RIPIEGO] || {};
    for (const suffisso of [forma, "other", "one"]) {
      const k = chiave + "_" + suffisso;
      if (cat[k] !== undefined || rip[k] !== undefined) return I18N.t(k, p);
    }
    return chiave;
  };

  /* Locale completo per Intl (date, numeri, tempi relativi). */
  I18N.locale = function () {
    return I18N.lang === "it" ? "it-IT" : "en-US";
  };

  I18N.setLang = function (lingua) {
    if (!LINGUE.includes(lingua)) return;
    I18N.lang = lingua;
    try { localStorage.setItem(CHIAVE_LS, lingua); } catch (e) { /* vedi sopra */ }
    if (typeof document === "object" && document.documentElement) {
      document.documentElement.lang = lingua;
    }
  };

  /* Applica le traduzioni al markup statico di index.html.
     `data-i18n` -> testo del nodo; `data-i18n-attr="titolo:chiave,..."` ->
     attributi (title, placeholder, aria-label). */
  I18N.applicaStatico = function (radice) {
    const r = radice || document;
    r.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = I18N.t(el.getAttribute("data-i18n"));
    });
    r.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.getAttribute("data-i18n-attr").split(",").forEach((coppia) => {
        const [attr, chiave] = coppia.split(":").map((x) => x.trim());
        if (attr && chiave) el.setAttribute(attr, I18N.t(chiave));
      });
    });
  };

  /* Lingua iniziale. `default_language` dalla config non e' ancora nota
     qui (arriva con lo snapshot): la applica app.js con setLang se
     l'utente non ha gia' scelto. */
  I18N.lang = leggiSalvata() || lingueDelBrowser() || RIPIEGO;
  if (typeof document === "object" && document.documentElement) {
    document.documentElement.lang = I18N.lang;
  }
  I18N.sceltaEsplicita = Boolean(leggiSalvata());

  const glob = typeof globalThis === "object" ? globalThis : window;
  glob.I18N = I18N;
  glob.t = I18N.t;
  glob.tp = I18N.tp;
})();
