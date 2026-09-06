/* ===================================================================
   LANMng — frontend SPA (vanilla JS, nessun build step)
   - Una connessione WebSocket riceve snapshot + update e aggiorna lo
     stato globale; la pagina attiva viene ri-renderizzata.
   - Router a hash (#/devices, ...). Nessuna libreria esterna.
   =================================================================== */

const state = { snap: {}, route: "dashboard", connected: false, datoRicevutoA: 0 };

/* ── Utility ──────────────────────────────────────────────────────── */
const $ = (sel, root = document) => root.querySelector(sel);
const h = (s) => (s == null ? "" : String(s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"));
// URL sicuro per un href: solo http/https (blocca javascript:, data:, ecc.).
// Ritorna "" se lo schema non e' consentito; l'attributo va poi passato per h().
const safeHref = (u) => { const s = String(u == null ? "" : u).trim();
  return /^https?:\/\//i.test(s) ? s : ""; };

function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtMB(mb) { return fmtBytes((Number(mb) || 0) * 1048576); }
/* Velocita' di rete: i contatori sono in bit/s, si mostra l'unita' adatta. */
function fmtRate(bps) {
  if (bps == null) return "—";
  const u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"];
  let n = Number(bps) || 0, i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
/* Ultimo punto della serie con la velocita' calcolata (i primi sono null). */
function lastTrafficPoint(series) {
  for (let i = (series || []).length - 1; i >= 0; i--)
    if (series[i].rx_bps != null) return series[i];
  return {};
}
function ago(ts) {
  if (!ts) return t("tempo.mai");
  return t("tempo.fa", { v: durata(ts) });
}

/* Intervallo trascorso, senza il "fa": serve dove la frase e' gia' "da ..." o
   "fermo da ...", che con il "fa" diventerebbe "da 5m fa". E' la funzione base
   e `ago` ci aggiunge il suffisso: prima era il contrario, con una regex che
   toglieva " fa" — e quella regex conosce una lingua sola. */
function durata(ts) {
  if (!ts) return t("tempo.mai");
  const d = Math.floor(Date.now() / 1000) - ts;
  if (d < 60) return t("tempo.secondi", { n: d });
  if (d < 3600) return t("tempo.minuti", { n: Math.floor(d / 60) });
  if (d < 86400) return t("tempo.ore", { n: Math.floor(d / 3600) });
  return t("tempo.giorni", { n: Math.floor(d / 86400) });
}

/* ── Tema ─────────────────────────────────────────────────────────
   I colori stanno nelle variabili CSS (`:root[data-tema=...]` in styles.css):
   qui si sceglie soltanto QUALE blocco e' attivo. La scelta e' del browser che
   guarda e non della macchina — dal telefono si puo' volere un tema diverso che
   dal PC — quindi vive nel localStorage e non richiede di riavviare il servizio.
   L'attributo lo stampa gia' lo script in testa a index.html, prima che il CSS
   dipinga: queste funzioni servono al selettore in Impostazioni. */
const TEMA_KEY = "lanmng.tema";
// nome/nota sono CHIAVI, non testo: il selettore le risolve quando disegna,
// cosi' cambiare lingua non richiede di ricostruire questa tabella.
const TEMI = [
  { id: "dark",      nome: "tema.dark",      nota: "tema.dark.nota" },
  { id: "pastello",  nome: "tema.pastello",  nota: "tema.pastello.nota" },
  { id: "neon",      nome: "tema.neon",      nota: "tema.neon.nota" },
  { id: "chiaro",    nome: "tema.chiaro",    nota: "tema.chiaro.nota" },
  { id: "contrasto", nome: "tema.contrasto", nota: "tema.contrasto.nota" },
  // L'id resta "ambra": e' quello gia' scritto nei localStorage dei browser,
  // cambiarlo farebbe ripartire tutti dal dark. Cambia solo il nome mostrato.
  { id: "ambra",     nome: "tema.ambra",     nota: "tema.ambra.nota" },
];

function temaAttivo() {
  const el = document.documentElement;
  const id = (el && el.getAttribute && el.getAttribute("data-tema")) || "";
  return TEMI.some(x => x.id === id) ? id : "dark";
}

function applicaTema(id) {
  // Un id sconosciuto (localStorage scritto a mano, tema tolto da una versione
  // successiva) vale come "dark": meglio il default che una pagina senza colori.
  const tema = TEMI.some(t => t.id === id) ? id : "dark";
  const el = document.documentElement;
  if (el) el.setAttribute("data-tema", tema);
  // Il localStorage puo' non esserci (finestra privata, storage bloccato): il
  // tema si applica lo stesso, semplicemente non sopravvive al ricaricamento.
  try { window.localStorage.setItem(TEMA_KEY, tema); }
  catch (e) { console.warn(t("tema.nonSalvato"), e.message); }
  ridisegnaTema();
  return tema;
}

/* Canvas e SVG non si ridipingono da soli quando cambiano le variabili CSS:
   i loro colori sono stati letti al momento del disegno. Non si rifa' la pagina
   intera con renderRoute(): il tema si cambia da Impostazioni, e ricostruire
   quella pagina butterebbe via l'editor YAML non salvato. */
function ridisegnaTema() {
  document.querySelectorAll("canvas").forEach(c => { if (c._dati) ridisegnaGrafico(c); });
  // map.svg resta valorizzato anche dopo aver lasciato la mappa: si ridisegna
  // solo se e' ancora quello montato nella pagina.
  if (map.svg && $("#lanmap") === map.svg) { buildMapDom(); legend(); }
}

// Glifi monocromatici (niente emoji: coerenza col resto della UI).
const ICON = { router: "⟐", desktop: "▣", laptop: "▭", server: "▤", vm: "❒",
  mobile: "▯", cloud: "◇", ap: "≋", printer: "▦", unknown: "○" };

/* Subnet, nome router: dalla config (snapshot.meta), nessun valore hardcoded. */
function metaSubnets() { return (state.snap.meta && state.snap.meta.subnets) || []; }
function subnetColor(label, ripiego) {
  const s = metaSubnets().find(x => x.label === label);
  return (s && s.color) || ripiego || paletteTema().muted;
}
function routerName() { return (state.snap.meta && state.snap.meta.router_name) || "router"; }
/* Indirizzo d'esempio per i placeholder dei campi IP: ricavato dalla prima
   subnet configurata, mai scritto qui. Vuoto se la config non c'e' ancora:
   meglio nessun suggerimento che uno che indica una rete non tua. */
function esempioIP() {
  const m = /^(\d+\.\d+\.\d+)\.\d+\/\d+$/.exec((metaSubnets()[0] || {}).cidr || "");
  return m ? `${m[1]}.10` : "";
}
/* Host noti (per suggerire dove monitorare un servizio): host Docker + IP dei
   device che espongono servizi. Solo suggerimenti: il campo resta libero. */
function knownHosts() {
  const set = new Set();
  (state.snap.docker?.hosts || []).forEach(x => x && x.name && set.add(x.name));
  (state.snap.devices || []).forEach(d => {
    if ((d.services || []).length) (d.ips || []).forEach(ip => set.add(ip));
  });
  return [...set];
}

/* ── Chiamate all'API ─────────────────────────────────────────────────
   Un solo punto per parlare col backend. Prima c'erano 39 chiamate diverse
   scritte a mano, in cinque forme: un guasto di rete, un 500 e un timeout
   producevano tutti la stessa stringa "Errore: rete", e 38 catch ingoiavano
   l'eccezione senza nemmeno loggarla.

   `api()` non solleva mai: chi chiama guarda `r.ok` e, se serve,
   `r.error.messaggio` — gia' in italiano, con l'azione da fare. */

const API_TIMEOUT = 12000;

/* Budget di tempo per endpoint. Un tetto unico o strozza il lavoro lento e
   legittimo (nmap dalla pagina Tools ha 180s di budget nel backend, vedi
   services/nettools.py) o e' cosi' largo da non accorgersi mai di un backend
   morto. Questi valori stanno SOTTO il proxy_read_timeout di nginx: il
   messaggio lo deve scrivere LANMng, non una pagina d'errore del proxy. */
const API_BUDGET = [
  [/^\/api\/tools\/run\b/, 200000],
  [/^\/api\/devices\/scan\b/, 120000],
  [/^\/api\/services\/refresh\b/, 90000],
  [/^\/api\/config\/backups\/[^/]+\/restore\b/, 30000],
  [/^\/api\/host\//, 25000],
  [/^\/api\/history\//, 20000],
];
function apiBudget(url) {
  const v = API_BUDGET.find(([re]) => re.test(url));
  return v ? v[1] : API_TIMEOUT;
}

/* Causa e azione per ogni tipo di fallimento. Il `detail` del backend, quando
   c'e', si aggiunge: dice il caso specifico, non sostituisce la spiegazione. */
const API_CAUSA = {
  rete:      ["err.rete", "err.rete.azione"],
  timeout:   ["err.timeout", "err.timeout.azione"],
  sessione:  ["err.sessione", "err.sessione.azione"],
  vietato:   ["err.vietato", "err.vietato.azione"],
  csrf:      ["err.csrf", "err.csrf.azione"],
  assente:   ["err.assente", "err.assente.azione"],
  conflitto: ["err.conflitto", "err.conflitto.azione"],
  invalido:  ["err.invalido", "err.invalido.azione"],
  limite:    ["err.limite", "err.limite.azione"],
  servizio:  ["err.servizio", "err.servizio.azione"],
  gateway:   ["err.gateway", "err.gateway.azione"],
  server:    ["err.server", "err.server.azione"],
  risposta:  ["err.risposta", "err.risposta.azione"],
};

/* Il 422 di FastAPI rompe la convenzione: `detail` e' una lista di oggetti e
   non una frase. Senza questo, interpolarlo stampa "[object Object]". */
function apiDetail422(d) {
  if (!Array.isArray(d)) return d == null ? "" : String(d);
  return d.map(e => {
    const campo = (e.loc || []).filter(x => x !== "body").join(".");
    return campo ? `${campo}: ${e.msg}` : String(e.msg || "");
  }).join("; ");
}

function apiClassifica(status, detail) {
  if (status === 401) return "sessione";
  if (status === 403) return /origine/i.test(detail) ? "csrf" : "vietato";
  if (status === 404) return "assente";
  if (status === 409) return "conflitto";
  if (status === 400 || status === 422) return "invalido";
  if (status === 429) return "limite";
  // Un 503 con un `detail` nostro NON viene dal proxy: e' require_session che
  // chiede una password admin. Chiamarlo "backend giu'" manderebbe a cercare
  // un guasto che non c'e'.
  if (status === 503) return detail ? "servizio" : "gateway";
  if (status === 502 || status === 504) return "gateway";
  if (status >= 500) return "server";
  return "invalido";
}

function apiErrore(kind, status, detail, extra = {}) {
  const [kCausa, kAzione] = API_CAUSA[kind] || API_CAUSA.server;
  const causa = t(kCausa), azione = kAzione ? t(kAzione) : "";
  const messaggio = causa
    + (detail ? " — " + detail : "")
    + (extra.retryAfter ? ". " + t("err.riprovaFra", { n: extra.retryAfter })
       : azione ? ". " + azione + "." : "");
  return {
    ok: false, status, data: extra.data || {},
    error: { kind, status, detail, messaggio, retryAfter: extra.retryAfter || 0 },
  };
}

async function apiUnaVolta(url, init, ms) {
  const ac = new AbortController();
  const scade = setTimeout(() => ac.abort(), ms);
  let r;
  try {
    r = await fetch(url, Object.assign({}, init, { signal: ac.signal }));
  } catch (e) {
    return apiErrore(e && e.name === "AbortError" ? "timeout" : "rete", 0, "");
  } finally {
    clearTimeout(scade);
  }
  // Il corpo si legge SEMPRE come testo: il 504 di nginx arriva in HTML e
  // r.json() esploderebbe nascondendo lo stato vero della risposta.
  let testo = "";
  try { testo = await r.text(); } catch { /* connessione caduta a meta' corpo */ }
  let data = null;
  if (testo) { try { data = JSON.parse(testo); } catch { data = null; } }
  if (r.ok) {
    if (data === null && testo) return apiErrore("risposta", r.status, testo.slice(0, 160));
    return { ok: true, status: r.status, data: data || {} };
  }
  const detail = data && data.detail !== undefined
    ? (r.status === 422 ? apiDetail422(data.detail) : String(data.detail))
    : "";
  const dopo = r.headers && r.headers.get ? Number(r.headers.get("Retry-After")) : 0;
  return apiErrore(apiClassifica(r.status, detail), r.status, detail,
    { retryAfter: dopo || 0, data: data || {} });
}

/* opt: { method, body, headers, timeout, retry, auth }
   - body oggetto: serializzato qui (resta una stringa, perche' la ripresa
     dopo il login lo rimanda tal quale)
   - auth: false disabilita l'interceptor 401 (obbligatorio su /api/auth/*,
     altrimenti un login sbagliato aprirebbe un login dentro il login) */
async function api(url, opt = {}) {
  const metodo = (opt.method || "GET").toUpperCase();
  const init = { method: metodo, headers: Object.assign({}, opt.headers) };
  if (opt.body !== undefined) {
    init.body = typeof opt.body === "string" ? opt.body : JSON.stringify(opt.body);
    if (!init.headers["Content-Type"]) init.headers["Content-Type"] = "application/json";
  }
  const ms = opt.timeout || apiBudget(url);
  // Si ritenta una sola volta, e solo cio' che non ha certamente prodotto
  // effetti: una GET caduta sulla rete o su un 502 di nginx in riavvio. Mai
  // una mutazione (potrebbe essere gia' passata) e mai un timeout: l'attesa
  // c'e' gia' stata, ripeterla raddoppia il carico su un backend in affanno.
  const tentativi = (metodo === "GET" && opt.retry !== false) ? 2 : 1;
  let esito;
  for (let i = 0; i < tentativi; i++) {
    esito = await apiUnaVolta(url, init, ms);
    if (esito.ok || (esito.error.kind !== "rete" && esito.error.kind !== "gateway")) break;
    if (i + 1 < tentativi) await new Promise(r => setTimeout(r, 400));
  }
  if (!esito.ok && esito.error.kind === "sessione" && opt.auth !== false)
    return apiDopoLogin(url, init, ms);
  return esito;
}

/* ── Notifiche ────────────────────────────────────────────────────────
   Tre modi di dire che qualcosa e' andato storto, con una regola netta:
   banner = stato permanente (.sec-banner), toast = evento, nota inline =
   risultato di un form, accanto al campo che l'ha causato.

   I due avvisi di sistema che c'erano prima bloccavano la pagina e non si
   potevano copiare. */

const TOAST_DURATA = { ok: 3000, info: 4000, warn: 6000, err: 8000 };
const TOAST_MAX = 4;

/* Il contenitore si crea alla prima notifica invece di stare in index.html:
   renderLogin() sostituisce document.body e cancellerebbe un nodo statico. */
function toastBox() {
  let el = $("#toasts");
  if (!el) {
    el = document.createElement("div");
    el.id = "toasts";
    el.className = "toasts";
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  return el;
}

const toastVivi = new Map();

/* o = { level, durata (0 = resta finche' non si chiude), azione: {label, onclick}, chiave } */
function toast(testo, o = {}) {
  const box = toastBox();
  const level = o.level || "info";
  // La chiave deduplica: su una pagina che si aggiorna ogni 10s un errore
  // ricorrente produrrebbe altrimenti un toast per ciclo.
  const chiave = o.chiave || "";
  if (chiave && toastVivi.has(chiave)) {
    const vecchio = toastVivi.get(chiave);
    if (vecchio.parentNode) { vecchio.remove(); toastVivi.delete(chiave); }
  }
  const el = document.createElement("div");
  el.className = "toast " + level;
  el.innerHTML = `<div class="msg">${h(testo)}</div>
    ${o.azione ? `<button class="act" type="button">${h(o.azione.label)}</button>` : ""}
    <button class="x" type="button" aria-label="Chiudi">×</button>`;
  const chiudi = () => {
    // Solo se la mappa punta ancora a QUESTO elemento: un toast sfrattato dal
    // tetto chiude piu' tardi, e cancellare la chiave a quel punto toglierebbe
    // la deduplica a quello nuovo che nel frattempo ha preso il suo posto.
    if (chiave && toastVivi.get(chiave) === el) toastVivi.delete(chiave);
    if (el.remove) el.remove();
  };
  const bx = el.querySelector(".x");
  if (bx) bx.onclick = chiudi;
  const ba = el.querySelector(".act");
  if (ba && o.azione) ba.onclick = () => { chiudi(); o.azione.onclick(); };
  box.appendChild(el);
  if (chiave) toastVivi.set(chiave, el);
  // Oltre il tetto si tolgono i piu' vecchi: una pila infinita coprirebbe la
  // pagina proprio mentre si cerca di capire cosa non va.
  while (box.children.length > TOAST_MAX) box.children[0].remove();
  const durata = o.durata === undefined ? TOAST_DURATA[level] : o.durata;
  if (durata) setTimeout(chiudi, durata);
  return el;
}

/* Scorciatoia dall'errore di api(). Senza azione l'errore si autochiude;
   con un'azione resta, perche' sparire mentre si sta per ripetere l'operazione
   e' esattamente il momento sbagliato. */
function toastErrore(err, azione) {
  return toast(err.messaggio, {
    level: "err", chiave: "api:" + err.kind,
    durata: azione ? 0 : TOAST_DURATA.err, azione,
  });
}

/* Nota che riempie una card quando la pagina non ha nessun dato da mostrare.
   Sostituisce i tre "API non raggiungibile" e l'uso di .empty come messaggio
   d'errore: dice il motivo e offre il ritentativo. */
function noteErrore(err, idRiprova) {
  return `<div class="cfg-note err">${h(err.messaggio)}${idRiprova
    ? ` <button class="btn" id="${h(idRiprova)}" style="margin-left:10px">&#8635; Riprova</button>`
    : ""}</div>`;
}

/* ── Sessione scaduta ─────────────────────────────────────────────────
   Il 401 non e' una pagina in cui finire: e' un'interruzione. Si chiede la
   password dove sta l'utente, si rifa' la chiamata e chi l'aveva iniziata
   riceve il risultato come se nulla fosse. Prima la sessione scaduta era
   gestita solo nella pagina Tools: altrove la pagina restava vuota. */

/* Stato del gate a partire dall'esito di /api/auth/status. Funzione pura,
   quindi verificabile senza DOM. Prima si guardava solo `password_set` su un
   oggetto lasciato a {} dal catch: con l'API giu' la dashboard accusava il
   proprietario di non aver mai impostato la password. "Non lo so" ora e' uno
   stato vero. */
function statoGate(esito) {
  if (!esito.ok) return "offline";
  if (esito.data.session) return "ok";
  return esito.data.password_set ? "login" : "senza-password";
}

/* Form di login riusabile: rende i campi dentro `box` e risolve true quando
   la sessione e' aperta, false se si rinuncia. E' l'unico punto che fa login
   in tutta la SPA; il gate delle pagine sensibili e il pannello della sessione
   scaduta lo mettono solo in due contenitori diversi. */
function formLogin(box, opt = {}) {
  return new Promise((resolve) => {
    box.innerHTML = `<form class="controls" style="margin:0">
      <input type="password" class="lg-pass" placeholder="password admin" autocomplete="current-password">
      <button class="btn" type="submit" style="border-color:var(--teal);color:var(--teal)">${h(t("login.entra"))}</button>
      ${opt.annulla ? `<button class="btn lg-no" type="button">${h(t("comune.annulla"))}</button>` : ""}
      </form><div class="lg-note"></div>`;
    const form = box.querySelector("form");
    const nota = box.querySelector(".lg-note");
    const campo = box.querySelector(".lg-pass");
    if (campo && campo.focus) campo.focus();
    const no = box.querySelector(".lg-no");
    if (no) no.onclick = () => resolve(false);
    form.onsubmit = async (e) => {
      e.preventDefault();
      // auth:false: senza, un login rifiutato ricadrebbe nell'interceptor e
      // aprirebbe un login dentro il login.
      const r = await api("/api/auth/login", {
        method: "POST", body: { password: campo.value }, auth: false, retry: false,
      });
      if (r.ok) return resolve(true);
      nota.innerHTML = `<div class="cfg-note err">${h(r.error.messaggio)}</div>`;
      campo.value = "";
      if (campo.focus) campo.focus();
    };
  });
}

/* Pannello sovrapposto per la sessione scaduta. Risolve true se si e' rientrati. */
async function pannelloLogin() {
  const esito = await api("/api/auth/status", { auth: false, retry: false, timeout: 4000 });
  const stato = statoGate(esito);
  if (stato === "ok") return true;
  const ov = document.createElement("div");
  ov.className = "modal-ov";
  document.body.appendChild(ov);
  const chiudi = (v, risolvi) => { ov.remove(); risolvi(v); };
  return new Promise((resolve) => {
    if (stato === "offline") {
      ov.innerHTML = `<div class="modal"><h3>${h(t("sicurezza.accessoNonVerificabile"))}</h3>
        ${noteErrore(esito.error)}
        <div class="modal-actions"><button class="btn lg-close">${h(t("comune.chiudi"))}</button></div></div>`;
    } else if (stato === "senza-password") {
      ov.innerHTML = `<div class="modal"><h3>${h(t("sicurezza.passwordMancante"))}</h3>
        <div class="muted">${h(t("sicurezza.impostaneUna"))}</div>
        <div class="modal-actions"><button class="btn lg-close">${h(t("comune.chiudi"))}</button></div></div>`;
    } else {
      ov.innerHTML = `<div class="modal"><h3>${h(t("sicurezza.sessioneScaduta"))}</h3>
        <div class="muted" style="margin-bottom:12px">${h(t("sicurezza.rientra"))}</div>
        <div class="lg-box"></div></div>`;
      formLogin(ov.querySelector(".lg-box"), { annulla: true })
        .then((v) => chiudi(v, resolve));
    }
    const c = ov.querySelector(".lg-close");
    if (c) c.onclick = () => chiudi(false, resolve);
    ov.onclick = (e) => { if (e.target === ov) chiudi(false, resolve); };
  });
}

/* Una sola promessa condivisa: cinque chiamate che prendono 401 insieme
   attendono lo stesso pannello, non ne aprono cinque. */
let loginInCorso = null;
function chiediLogin() {
  if (!loginInCorso)
    loginInCorso = pannelloLogin().finally(() => { loginInCorso = null; });
  return loginInCorso;
}

async function apiDopoLogin(url, init, ms) {
  const rientrato = await chiediLogin();
  if (!rientrato) return apiErrore("sessione", 401, "");
  // Una sola ripresa, con apiUnaVolta e non con api(): se il 401 si ripete
  // (password giusta ma permesso mancante) si torna al chiamante con l'errore
  // invece di riaprire il pannello all'infinito.
  return apiUnaVolta(url, init, ms);
}

/* ── WebSocket ──────────────────────────────────────────────────────
   Prima la riconnessione era a intervallo fisso di 2s, senza tetto: col
   backend spento erano trenta tentativi al minuto per ore. E l'indicatore
   diceva solo "live"/"offline", mentre l'orario accanto era quello del
   browser al momento dell'ultimo messaggio: restava fermo su un'ora
   plausibile e faceva sembrare fresco un dato di mezz'ora prima. */

const WS_BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000];
let wsTentativo = 0, wsSock = null, wsProssimo = 0;
/* Diagnosi gia' fatta per questa serie di cadute: si chiede all'API una volta
   sola, non ad ogni tentativo. Si azzera quando arriva del dato vero. */
let wsDiagnosticato = false;

function wsRitardo() {
  const base = WS_BACKOFF[Math.min(wsTentativo, WS_BACKOFF.length - 1)];
  // Jitter +/-25%: con piu' schede aperte non tornano tutte nello stesso istante.
  return Math.round(base * (0.75 + Math.random() * 0.5));
}

/* Perche' lo stream non riparte, quando il browser non lo dice. Tre esiti:
   il backend non risponde (lo dice gia' il contatore), la sessione e' scaduta
   (si chiede la password e si riprende), oppure l'API risponde con una
   sessione buona e allora il problema e' il percorso /ws — un proxy che non
   inoltra l'upgrade, o l'origine non consentita. */
async function diagnosticaWS() {
  const st = await api("/api/auth/status", { auth: false, retry: false, timeout: 4000 });
  if (!st.ok) return;                       // backend giu': il conto alla rovescia basta
  if (!st.data.session) {
    const dentro = await chiediLogin();
    if (dentro) { wsTentativo = 0; wsDiagnosticato = false; connectWS(); return; }
    toast(t("msg.wsSessione"),
          { level: "warn", durata: 0, chiave: "ws-sessione",
            azione: { label: t("comune.rientra"), onclick: () => { wsTentativo = 0; connectWS(); } } });
    return;
  }
  toast(t("msg.wsNonParte"),
        { level: "warn", durata: 0, chiave: "ws-percorso",
          azione: { label: t("azione.riprova"), onclick: () => { wsTentativo = 0; wsDiagnosticato = false; connectWS(); } } });
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  wsSock = ws;
  let keepalive = null;
  ws.onopen = () => {
    // Il contatore NON si azzera qui: una connessione che si apre e muore
    // subito riporterebbe il backoff a 1s per sempre. Si azzera all'arrivo
    // del primo dato vero (vedi onmessage).
    keepalive = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
    aggiornaStatoConnessione();
  };
  ws.onclose = (ev) => {
    if (keepalive) { clearInterval(keepalive); keepalive = null; }
    wsSock = null;
    const motivo = (ev && ev.reason) || "";
    if (motivo === "origine") {
      // Insistere e' inutile: e' una configurazione sbagliata, non un guasto.
      wsProssimo = 0;
      toast(t("msg.wsOrigine"), { level: "err", durata: 0, chiave: "ws-origine" });
      aggiornaStatoConnessione();
      return;
    }
    if (motivo === "sessione") {
      // Ritentare ogni due secondi con la sessione scaduta non riapriva mai
      // lo stream: si chiede la password una volta e si riprova solo dopo.
      wsProssimo = 0;
      aggiornaStatoConnessione();
      chiediLogin().then((dentro) => {
        if (dentro) { wsTentativo = 0; connectWS(); return; }
        // Rinunciando al login non si riconnette da soli: senza questo avviso
        // l'indicatore resterebbe "offline" per sempre e l'unica via d'uscita
        // sarebbe ricaricare la pagina, senza sapere perche'.
        toast(t("msg.wsSessione"),
              { level: "warn", durata: 0, chiave: "ws-sessione",
                azione: { label: t("comune.rientra"), onclick: () => { wsTentativo = 0; connectWS(); } } });
      });
      return;
    }
    // Nessun motivo: il rifiuto e' avvenuto durante l'handshake — il server
    // chiude prima di accettare (origine o sessione, vedi main.py) e il
    // browser riceve un 1006 muto, senza codice ne' testo. I due rami qui
    // sopra, che leggono `ev.reason`, in quel caso non scattano mai: la
    // dashboard restava a "riconnessione fra Ns" per ore, con i dati fermi e
    // nessuno che dicesse che la sessione era scaduta. Si chiede all'API.
    if (!motivo && wsTentativo >= 1 && !wsDiagnosticato) {
      wsDiagnosticato = true;
      diagnosticaWS();
    }
    const attesa = wsRitardo();
    wsTentativo += 1;
    wsProssimo = Date.now() + attesa;
    aggiornaStatoConnessione();
    setTimeout(connectWS, attesa);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "snapshot" || msg.type === "update") {
      wsTentativo = 0;                 // dati veri: la connessione regge
      wsProssimo = 0;
      wsDiagnosticato = false;         // se ricadra', si torna a indagare
      state.snap = msg.data || {};
      state.datoRicevutoA = Date.now();
      onData();
    }
  };
}

/* Eta' del dato misurata come DURATA dalla ricezione, non come orario: cosi'
   e' immune anche allo sfasamento fra l'orologio del browser e quello del
   server, e soprattutto continua a scorrere quando gli update si fermano. */
function etaDato() {
  if (!state.datoRicevutoA) return { testo: "—", fermo: false };
  const s = Math.round((Date.now() - state.datoRicevutoA) / 1000);
  const soglia = ((state.snap.collector || {}).fast_interval || 10) * 3;
  return {
    testo: s < 60 ? `agg. ${s}s fa` : `fermo da ${Math.floor(s / 60)}m`,
    fermo: s > soglia,
  };
}

/* Tre stati onesti al posto di due: connesso, in attesa del prossimo
   tentativo (con il conto alla rovescia), oppure fermo. */
function statoConnessione() {
  const eta = etaDato();
  if (wsSock && wsSock.readyState === 1 && !eta.fermo) return { cls: "ok", testo: "live" };
  if (wsSock && wsSock.readyState === 1) return { cls: "warn", testo: "dati fermi" };
  if (wsProssimo) {
    const fra = Math.max(0, Math.round((wsProssimo - Date.now()) / 1000));
    return { cls: "warn", testo: `riconnessione fra ${fra}s` };
  }
  return { cls: "down", testo: "offline" };
}

function aggiornaStatoConnessione() {
  const st = statoConnessione();
  state.connected = st.cls === "ok";
  const el = $("#conn");
  if (el) {
    el.className = "pill " + st.cls;
    el.innerHTML = `<span class="dot"></span> ${h(st.testo)}`;
  }
  const up = $("#updated");
  if (up) {
    const eta = etaDato();
    up.className = "pill" + (eta.fermo ? " warn" : "");
    up.textContent = eta.testo;
  }
}

/* Chiamata ad ogni snapshot/update: aggiorna chrome + pagina attiva. */
/* Lingua predefinita del servizio (config `ui.default_language`).

   Si applica SOLO a chi non ha gia' scelto nel proprio browser: il contrario —
   la config che scavalca la scelta — farebbe cambiare lingua sotto le mani ad
   ogni ricaricamento, senza che si capisca perche'. */
function applicaLinguaPredefinita(meta) {
  const lingua = (meta || {}).default_language;
  if (!lingua || I18N.sceltaEsplicita || lingua === I18N.lang) return false;
  I18N.lang = lingua;
  if (document.documentElement) document.documentElement.lang = lingua;
  I18N.applicaStatico();
  return true;
}

function onData() {
  const s = state.snap;
  aggiornaStatoConnessione();
  // badge sidebar
  const dev = s.devices_summary || {};
  $("#nav-devices").textContent = dev.total ? `${dev.online}/${dev.total}` : "";
  const svc = (s.services || {}).summary || {};
  $("#nav-services").textContent = svc.total ? `${svc.ok}/${svc.total}` : "";
  const wg = s.wireguard || {};
  $("#nav-wg").textContent = wg.total_peers != null ? `${wg.active_peers}/${wg.total_peers}` : "";
  // footer: info router
  const sys = s.system || {};
  $("#foot-info").innerHTML = sys.hostname
    ? `${h(sys.hostname)}<br>up ${h(sys.uptime_human || "")}<br>RAM ${sys.memory_used_pct ?? "?"}%`
    : "—";
  // Prima di renderIfLive: le pagine a refresh parziale e quelle `live: false`
  // non rifanno il render, e senza questo il box resterebbe fermo a quando si
  // e' entrati nella pagina.
  aggiornaBadgeAlert();
  aggiornaBoxAlert();
  // Lo stato di sicurezza ora e' nello snapshot: prima era una fotografia
  // scattata al boot, e chiudere la falla dalla UI non spegneva il banner.
  applicaLinguaPredefinita(s.meta);
  if (Array.isArray(s.security)) renderSecurityBanner(s.security);
  renderIfLive();
}

/* ── Router ───────────────────────────────────────────────────────── */
const PAGES = {
  dashboard: { title: "pagina.dashboard", render: pageDashboard, refresh: refreshDashboard },
  // refresh: aggiornamento parziale sugli update live (vedi renderIfLive), cosi'
  // i controlli restano in piedi e chi sta scrivendo nel filtro non perde il cursore.
  devices:   { title: "pagina.devices", render: pageDevices, refresh: refreshDevices },
  // Sezione monitoraggio: due sotto-tab (Servizi | Risorse) sotto la stessa
  // voce di menu. `nav` dice quale voce di sidebar resta accesa.
  services:  { title: "pagina.services", render: pageServices,
               refresh: refreshServices, nav: "services" },
  resources: { title: "pagina.resources", render: pageResources,
               refresh: refreshResources, nav: "services" },
  wireguard: { title: "pagina.wireguard", render: pageWireGuard, refresh: refreshWireGuard },
  stats:     { title: "pagina.stats", render: pageStats, refresh: refreshStats },
  wan:       { title: "pagina.wan", render: pageWan, refresh: refreshWan },
  // logs: on-demand, non si auto-rigenera ad ogni update (preserva i filtri).
  logs:      { title: "pagina.logs", render: pageLogs, live: false },
  tools:     { title: "pagina.tools", render: pageTools, live: false },
  host:      { title: "pagina.host", render: pageHost, live: false },
  terminal:  { title: "pagina.terminal", render: pageTerminal, live: false },
  settings:  { title: "pagina.settings", render: pageSettings, live: false },
};

function go(route) {
  if (!PAGES[route]) route = "dashboard";
  // Uscire da Impostazioni con l'editor modificato buttava via il lavoro senza
  // dire niente: la pagina si ridisegna da capo alla prossima visita.
  if (state.route === "settings" && route !== "settings" && cfgSporco()
      && !confirm(t("chiedi.uscireSenzaSalvare")))
    return;
  if (route !== "devices") stopMap();
  if (route !== "terminal") stopTerminal();
  // Lasciare aperto lo stream dei log uscendo dalla pagina terrebbe occupata
  // una connessione (e, sul router, un giro di SSH ogni tot secondi) per una
  // pagina che nessuno sta piu' guardando.
  if (route !== "logs") stopLogTail();
  if (route !== "services" && route !== "resources") stopMonitorEta();
  state.route = route;
  closeNav();                       // da telefono il drawer copre la pagina scelta
  location.hash = "#/" + route;
  const navRoute = PAGES[route].nav || route;
  document.querySelectorAll("#nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.route === navRoute));
  $("#page-title").textContent = t(PAGES[route].title);
  renderRoute();
}
function renderRoute() {
  const p = PAGES[state.route]; if (!p) return;
  p.render($("#view"), state.snap);
}
/* Ri-render su update solo per le pagine "live" (non per i logs).
   Se la pagina sa aggiornarsi per parti (refresh) si usa quella strada: rifare
   l'intera pagina distrugge i campi in cui l'utente sta scrivendo. */
function renderIfLive() {
  const p = PAGES[state.route];
  if (!p || p.live === false) return;
  if (p.refresh && p.refresh($("#view"), state.snap)) return;
  renderRoute();
}
window.addEventListener("hashchange", () => {
  const r = (location.hash.replace(/^#\/?/, "") || "dashboard");
  if (r !== state.route) go(r);
});

/* ── Navigazione mobile (drawer) ──────────────────────────────────── */
/* Sotto i 760px la sidebar sta fuori schermo e si apre con l'hamburger.
   Prima veniva soltanto nascosta dal CSS: senza nulla al suo posto, da
   telefono si restava bloccati sulla pagina di partenza. */
function navIsOpen() { return document.body.classList.contains("nav-open"); }

function setNavOpen(open) {
  document.body.classList.toggle("nav-open", open);
  const scrim = $("#nav-scrim");
  if (scrim) scrim.hidden = !open;
  const btn = $("#nav-toggle");
  if (btn) {
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? t("topbar.chiudiMenu") : t("topbar.apriMenu"));
  }
  // Il fuoco segue il drawer: aperto va sulla voce attiva, chiuso torna
  // all'hamburger — ma solo se stava dentro il drawer, altrimenti navigare
  // da mouse su desktop sposterebbe il fuoco ad ogni cambio pagina.
  const act = document.activeElement;
  if (open) {
    const first = $("#nav a.active") || $("#nav a");
    if (first && first.focus) first.focus();
  } else if (btn && btn.focus && act && act.closest && act.closest(".sidebar")) {
    btn.focus();
  }
}

function closeNav() { if (navIsOpen()) setNavOpen(false); }

function bindMobileNav() {
  const btn = $("#nav-toggle");
  if (btn) btn.onclick = () => setNavOpen(!navIsOpen());
  const scrim = $("#nav-scrim");
  if (scrim) scrim.onclick = () => closeNav();
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNav(); });
  // Ruotando il telefono si puo' risalire sopra la soglia mobile: li' la
  // sidebar torna fissa, e lasciare il drawer "aperto" farebbe ricomparire lo
  // scrim al primo ritorno in verticale.
  const mq = window.matchMedia && window.matchMedia("(max-width: 760px)");
  if (mq && mq.addEventListener)
    mq.addEventListener("change", (e) => { if (!e.matches) closeNav(); });
}

/* ===================================================================
   PAGINE
   =================================================================== */

function card(title, body, right = "") {
  return `<div class="card"><h3>${h(title)}${right ? `<span class="right">${right}</span>` : ""}</h3>${body}</div>`;
}
/* Una card che continua a mostrare i numeri del giro precedente senza dirlo
   sta mentendo: questi due lo dichiarano, riusando `sources` dello snapshot.
   Vuoti quando la sorgente sta bene, cosi' si possono mettere ovunque. */
function badgeSorgente(nome) {
  const s = (state.snap.sources || {})[nome];
  if (!s || s.ok) return "";
  return `<span class="pill down" title="${h(s.error || "")}">
    <span class="dot"></span> fermo da ${h(durata(s.since))}</span>`;
}
/* Eta' del dato quando la sorgente sta BENE: `badgeSorgente` parla solo quando
   e' rotta, e nel frattempo la pagina mostrava numeri senza dire di quando
   fossero. Il giro lento e' di 60s, quindi "uptime" e "RAM" del router possono
   avere tranquillamente un minuto: dirlo e' la differenza fra un dato e una
   fotografia. Vuoto se non c'e' un'ora di raccolta. */
function etaSorgente(nome) {
  const s = (state.snap.sources || {})[nome];
  if (!s || !s.ok || !s.ts) return "";
  return `<span class="muted">dato di ${h(ago(s.ts))}</span>`;
}

function notaSorgente(nome) {
  return `<div class="src-nota">${notaSorgenteHtml(nome)}</div>`;
}

/* Aggiorna l'avviso senza ridisegnare la pagina: serve alle pagine che si
   aggiornano per parti (Dispositivi, Risorse), dove il render completo non
   viene rifatto e l'avviso resterebbe quello di quando si e' entrati. */
function aggiornaNoteSorgente(nomi) {
  document.querySelectorAll(".src-nota").forEach((el, i) => {
    el.innerHTML = notaSorgenteHtml(nomi[i]);
  });
}

function notaSorgenteHtml(nome) {
  const s = (state.snap.sources || {})[nome];
  if (!s || s.ok) return "";
  return `<div class="cfg-note err">${h(s.error || t("comune.sorgenteNonRaggiungibile"))}${
    s.ts ? h(t("comune.ultimoDatoValido", { q: ago(s.ts) })) : h(t("comune.nessunDatoMaiRaccolto"))}</div>`;
}

/* `o` e' facoltativo, cosi' le KPI delle altre pagine restano com'erano:
     dot  pallino di stato accanto al numero (s-on/s-warn/s-down/s-off)
     ctx  riga di contesto sotto l'etichetta ("3 offline", t("stato.tuttiAttivi"))
     vai  rotta di destinazione: rende la card cliccabile (delegation in bindAlert) */
/* L'etichetta sta SOPRA il valore: dice cosa stai per leggere prima che tu lo
   legga, invece di farlo indovinare dal numero e spiegarlo dopo. */
function kpi(val, sub, cls = "", o = {}) {
  const vai = o.vai ? ` data-vai="${h(o.vai)}" title="${h(t("azione.vaiA", { dove: (PAGES[o.vai] ? t(PAGES[o.vai].title) : o.vai) }))}"` : "";
  return `<div class="card kpi${o.vai ? " clic" : ""}"${vai}>
    <div class="sub">${h(sub)}</div>
    <div class="val ${cls}">${o.dot ? `<span class="status-dot ${h(o.dot)}"></span>` : ""}${val}</div>
    ${o.ctx ? `<div class="ctx">${h(o.ctx)}</div>` : ""}</div>`;
}

/* ── Alert contestuali ─────────────────────────────────────────────
   Ogni alert dichiara la pagina che possiede l'oggetto (`scope`): compare li',
   non in una tab a parte. Il calcolo sta tutto nel backend (services/alerts.py):
   qui si disegna e basta, cosi' non esistono due elenchi da tenere allineati. */

function alertsPerScope(scope) {
  return (state.snap.alerts || []).filter(a => a.scope === scope);
}

/* Contenitore vuoto quando non c'e' niente: si puo' mettere in testa a
   qualunque pagina senza lasciare spazi bianchi. La classe e' sua (non
   `.src-nota`, che aggiornaNoteSorgente accoppia ai nomi per posizione). */
function alertBox(scope) {
  return `<div class="alert-box" data-scope="${h(scope)}">${alertBoxHtml(scope)}</div>`;
}

function alertBoxHtml(scope) {
  // Gli avvisi di sicurezza hanno gia' il banner rosso in cima alla pagina:
  // ripeterli qui li mostrerebbe tre volte nella stessa schermata.
  const righe = alertsPerScope(scope).filter(a => a.source !== "security");
  return righe.map(alertRiga).join("");
}

/* Campanella sbarrata, disegnata: era l'unica emoji rimasta nella UI (le
   convenzioni di progetto non ne vogliono), e per giunta la rendeva a colori
   e con un font diverso da tutto il resto. Stessa forma delle icone di
   sidebar: tratto su `currentColor`, cosi' segue il colore del bottone. */
const ICONA_SILENZIA = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="2" stroke-linecap="round" aria-hidden="true">
  <path d="M18 8a6 6 0 0 0-9.3-5"/><path d="M6 9v3l-2 4h13"/>
  <path d="M10.5 20a2 2 0 0 0 3 0"/><line x1="3" y1="3" x2="21" y2="21"/></svg>`;

function alertRiga(a) {
  const dot = a.level === "critical" ? "s-down" : a.level === "warn" ? "s-warn" : "s-off";
  return `<div class="alert-riga ${h(a.level)}">
    <span class="status-dot ${dot}"></span>
    <div class="alert-testo">
      <b>${h(a.title)}</b> <span class="muted">· ${h(t("tempo.da", { v: durata(a.since) }))}</span>
      <div>${h(a.detail)}</div>
      ${a.action ? `<div class="alert-azione">${h(a.action)}</div>` : ""}
    </div>
    <button class="iconbtn" title="${h(t("alert.silenziaPerSempre"))}"
      data-silenzia="${h(a.rule)}" data-soggetto="${h(a.subject)}">${ICONA_SILENZIA}</button>
  </div>`;
}

/* Aggiorna i box senza ridisegnare la pagina. Obbligatorio: sulle pagine a
   refresh parziale il render completo non viene rifatto, e su quelle
   `live: false` non viene rifatto mai — il box resterebbe quello di quando si
   e' entrati, cioe' proprio il caso da segnalare non si vedrebbe. */
function aggiornaBoxAlert() {
  document.querySelectorAll(".alert-box").forEach(el => {
    el.innerHTML = alertBoxHtml(el.dataset.scope);
  });
}

/* Contatore per voce di menu. Servizi e Risorse stanno sotto Monitoraggio,
   quindi si somma sulla voce (`PAGES[scope].nav`), non sullo scope. Gli `info`
   restano visibili in pagina ma non accendono il badge. */
function aggiornaBadgeAlert() {
  const perScope = (state.snap.alerts_summary || {}).by_scope || {};
  const perVoce = {};
  Object.entries(perScope).forEach(([scope, r]) => {
    const voce = (PAGES[scope] || {}).nav || scope;
    const c = perVoce[voce] || (perVoce[voce] = { critical: 0, warn: 0 });
    c.critical += r.critical || 0;
    c.warn += r.warn || 0;
  });
  document.querySelectorAll(".badge.alert").forEach(el => {
    const c = perVoce[el.dataset.route] || { critical: 0, warn: 0 };
    const tot = c.critical + c.warn;
    el.textContent = tot ? String(tot) : "";
    el.className = "badge alert" + (c.critical ? " critical" : "");
    el.hidden = !tot;
    el.title = tot ? `${c.critical} critici, ${c.warn} da guardare` : "";
  });
}

/* Un solo gestore per tutta la pagina: i box si riscrivono ad ogni update e
   ri-agganciare i pulsanti ogni volta significherebbe dimenticarsene in uno
   dei punti in cui si riscrivono. */
function bindAlert() {
  document.addEventListener("click", (e) => {
    if (!e.target || !e.target.closest) return;
    // Una riga cliccabile puo' contenere un link vero (il servizio in evidenza
    // che si apre in una scheda nuova): senza questa guardia il clic aprirebbe
    // la scheda E cambierebbe pagina sotto, lasciando la dashboard altrove.
    if (e.target.closest("a[href]")) return;
    const bersaglio = e.target.closest("[data-silenzia],[data-vai]");
    if (!bersaglio) return;
    if (bersaglio.dataset.silenzia) return openSilenceModal(bersaglio.dataset.silenzia, bersaglio.dataset.soggetto || "");
    go(bersaglio.dataset.vai);
  });
}

/* Silenziamento: modale e non un prompt di sistema, perche' il motivo e'
   obbligatorio e va spiegato perche' lo chiediamo. */
function openSilenceModal(rule, subject) {
  const ov = document.createElement("div");
  ov.className = "modal-ov";
  ov.innerHTML = `<div class="modal">
    <h3>${h(t("alert.silenziaTitolo"))}</h3>
    <div class="muted" style="font-size:12px;margin-bottom:10px">
      Non suonera' piu', per sempre, finche' non lo riattivi da
      Impostazioni → Alert silenziati. Il salvataggio riscrive
      <code>config.yaml</code> (con backup automatico) perdendone i commenti.
    </div>
    <label>${h(t("alert.regola"))}<input type="text" value="${h(rule)}" disabled></label>
    <label>${h(t("alert.soggetto"))}<input type="text" value="${h(subject || t("alert.tuttaLaRegola"))}" disabled></label>
    <label>${h(t("alert.motivo"))}<input type="text" id="sl-reason" placeholder="${h(t("alert.motivoEsempio"))}"></label>
    <div id="sl-msg" class="cfg-note err" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn" id="sl-cancel">${h(t("comune.annulla"))}</button>
      <button class="btn" id="sl-save" style="border-color:var(--teal);color:var(--teal)">${h(t("alert.silenzia"))}</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  $("#sl-cancel", ov).onclick = close;
  const campo = $("#sl-reason", ov);
  if (campo.focus) campo.focus();
  $("#sl-save", ov).onclick = async () => {
    const reason = campo.value.trim();
    const msg = $("#sl-msg", ov);
    if (!reason) {
      msg.textContent = t("alert.serveMotivo");
      msg.style.display = "";
      return;
    }
    const r = await api("/api/alerts/silence", { method: "POST", body: { rule, subject, reason } });
    if (!r.ok) { msg.textContent = r.error.messaggio; msg.style.display = ""; return; }
    close();
    // Nessun "riavvia il servizio": la sezione viene riletta dal file.
    toast(t("msg.avvisoSilenziato"), { level: "ok" });
  };
}

/* ── Dashboard ────────────────────────────────────────────────────── */

/* "1 non rispondono" si legge sbagliato, e su una dashboard che si guarda ogni
   giorno si nota ogni volta. Con uno solo si usa il singolare. */
/* Plurale dal catalogo: `chiave_one` / `chiave_other`, scelti con
   Intl.PluralRules. Prima le due forme erano scritte a ogni chiamata, quindi
   ogni lingua nuova avrebbe richiesto di toccare tutte e tredici. */
function conta(n, chiave) {
  return tp(chiave, n);
}

/* La fascia in alto. Ogni riquadro dice tre cose: quanti, di quanti, e cosa
   c'e' da fare — e porta alla pagina che se ne occupa, che era il pezzo
   mancante (i numeri c'erano, ma restavano un vicolo cieco). */
function dashKpi(s) {
  const dev = s.devices_summary || {}, svc = (s.services || {}).summary || {};
  const wg = s.wireguard || {};
  const wan = (s.interfaces || []).find(i => i.name === s.wan_interface);
  const wgGiu = (wg.total_peers ?? 0) - (wg.active_peers ?? 0);
  // Finche' il dato non e' arrivato il riquadro resta spento: un pallino verde
  // su "–/–" direbbe "tutto a posto" di una cosa che non si e' ancora guardata.
  const attesaDev = dev.total == null, attesaSvc = svc.total == null;
  return `
    ${kpi(`${dev.online ?? "–"}<span class="unit">/${dev.total ?? "–"}</span>`,
      "dispositivi online", attesaDev ? "" : "green",
      { dot: attesaDev ? "s-off" : "s-on", vai: "devices",
        ctx: attesaDev ? t("stato.inAttesa") : (dev.offline ? conta(dev.offline, "conta.spento") : t("stato.tuttiAccesi")) })}
    ${kpi(`${svc.ok ?? "–"}<span class="unit">/${svc.total ?? "–"}</span>`,
      "servizi attivi", attesaSvc ? "" : (svc.down ? "orange" : "green"),
      { dot: attesaSvc ? "s-off" : (svc.down ? "s-down" : "s-on"), vai: "services",
        ctx: attesaSvc ? t("stato.inAttesa") : (svc.down ? conta(svc.down, "conta.nonRisponde") : t("stato.tuttiAttivi")) })}
    ${kpi(`${wg.active_peers ?? "–"}<span class="unit">/${wg.total_peers ?? "–"}</span>`,
      "peer WireGuard", wg.active_peers ? "green" : "",
      { dot: wg.active_peers ? "s-on" : "s-off", vai: "wireguard",
        ctx: wg.total_peers == null ? "" : (wgGiu ? conta(wgGiu, "conta.nonConnesso") : t("stato.tuttiConnessi")) })}
    ${dashKpiWan(wan)}`;
}

/* La WAN non si annuncia piu' con la scritta "ONLINE": un pallino verde si
   legge senza leggere, e "connessa"/"giu'" e' la stessa parola che usa la
   pagina WAN, cosi' le due non si contraddicono. */
function dashKpiWan(wan) {
  if (!wan) return kpi("—", "WAN", "", { dot: "s-off", vai: "wan", ctx: "nessuna interfaccia di uplink" });
  // `testo`: una parola non e' un numero e a 30px mono uscirebbe dal riquadro.
  return kpi(wan.up ? "connessa" : "giù", `WAN ${wan.ifname || wan.name || ""}`,
    (wan.up ? "green" : "red") + " testo",
    { dot: wan.up ? "s-on" : "s-down", vai: "wan",
      ctx: `${fmtMB(wan.rx_mb)} RX · ${fmtMB(wan.tx_mb)} TX totali` });
}

/* Quanto arco di tempo copre la finestra viva del traffico. Derivata dai dati
   (numero di punti x intervallo del giro rapido), non scritta a mano: se un
   giorno l'intervallo cambia in config, la frase resta vera. */
function finestraTraffico(s) {
  const n = (s.traffic_series || []).length;
  const passo = (s.collector || {}).fast_interval || 10;
  if (n < 2) return "";
  const min = Math.round((n - 1) * passo / 60);
  return min >= 1 ? `ultimi ~${min} min` : `ultimi ~${(n - 1) * passo}s`;
}

/* Numeri del traffico: fuori dal grafico, cosi' si leggono anche quando la
   linea e' schiacciata in basso. La latenza e' un numero e basta — sul grafico
   costava un secondo asse e una terza linea per un valore che sta fermo. */
function dashTrafficoNum(s) {
  const now = lastTrafficPoint(s.traffic_series);
  return `
    <span class="rate"><i style="background:var(--teal)"></i>↓ ${h(fmtRate(now.rx_bps))}</span>
    <span class="rate"><i style="background:var(--green)"></i>↑ ${h(fmtRate(now.tx_bps))}</span>
    <span class="muted">latenza ${now.latency ? h(Math.round(now.latency)) + " ms" : "—"}</span>`;
}

/* Peer WireGuard in dashboard. Il dato lo raccogliamo dal router ad ogni giro
   rapido (endpoint, handshake, byte) ma finora la dashboard ne mostrava solo
   il conteggio: "2/4" non dice QUALE dei quattro non c'e'. */
function wgMini(s) {
  const wg = s.wireguard || {};
  const peers = (wg.interfaces || []).flatMap(i => i.peers || []);
  if (!peers.length) return `<div class="empty">${h(t("wg.nessunPeer"))}</div>`;
  // Prima chi non e' connesso, poi per nome: stesso criterio dei servizi.
  const righe = peers.slice().sort((a, b) =>
    ((a.status === "active") === (b.status === "active") ? 0 : a.status === "active" ? 1 : -1) ||
    String(a.name).localeCompare(String(b.name)));
  return `<table><tbody>${righe.map(p => {
    const su = p.status === "active";
    return `<tr data-vai="wireguard" style="cursor:pointer" title="apri WireGuard">
      <td><span class="status-dot ${su ? "s-on" : "s-warn"}"></span>${h(nomePeer(p))}
        <div class="muted mono" style="font-size:11px">${h(p.endpoint || t("wg.nessunEndpoint"))}</div></td>
      <td class="right mono muted nowrap" style="font-size:11px">
        ↓ ${h(fmtMB(p.rx_mb))}<br>↑ ${h(fmtMB(p.tx_mb))}</td>
      <td class="right nowrap"><span class="chip ${su ? "ok" : "ignoto"}"
        title="ultimo handshake">${h(p.last_handshake ? ago(p.last_handshake) : "mai")}</span></td>
    </tr>`;
  }).join("")}</tbody></table>`;
}

function dashRouter(s) {
  const sys = s.system || {};
  if (!sys.hostname) return `<div class="empty">In attesa dati router…</div>`;
  return `<table><tbody>
    <tr><td class="muted">${h(t("host.modello"))}</td><td class="mono">${h(sys.model)}</td></tr>
    <tr><td class="muted">OS</td><td class="mono">${h(sys.os_version)}</td></tr>
    <tr><td class="muted">${h(t("host.uptime"))}</td><td class="mono">${h(sys.uptime_human)}</td></tr>
    <tr><td class="muted">${h(t("host.load"))}</td><td class="mono">${(sys.load || []).map(x => x.toFixed(2)).join("  ")}</td></tr>
    <tr><td class="muted">RAM</td><td class="mono">${sys.memory_used_pct}% di ${sys.memory_total_mb} MB</td></tr>
  </tbody></table>`;
}

/* L'indice degli alert sta in cima e a tutta larghezza, e SOLO quando c'e'
   qualcosa: prima era una card fissa in griglia, mezza vuota quando andava
   tutto bene — cioe' quasi sempre — e occupava un quarto della pagina per
   dire "nessun alert", cosa che i contatori in sidebar dicono gia'. */
function dashAlertBox(s) {
  const a = s.alerts || [];
  if (!a.length) return "";
  return card(t("alert.titolo"), alertIndice(a), alertConteggi(s));
}

function pageDashboard(view, s) {
  view.innerHTML = `
    <div id="dash-alerts">${dashAlertBox(s)}</div>
    <div class="grid cols-4" style="margin-bottom:14px" id="dash-kpi">${dashKpi(s)}</div>
    <div class="grid cols-2-1" style="margin-bottom:14px">
      ${card(t("dash.trafficoWan"), `<canvas class="chart tall" id="dash-traffic"></canvas>
        ${notaSorgente("traffic")}
        <div class="rates" id="dash-traffico-num">${dashTrafficoNum(s)}</div>`,
        `<span id="dash-traffico-testa">${dashTrafficoTesta(s)}</span>`)}
      ${card(t("dash.peerWireguard"), `<div id="dash-wg">${wgMini(s)}</div>`,
        `<span class="muted" data-vai="wireguard" style="cursor:pointer">dettagli →</span>`)}
    </div>
    <div class="grid cols-2-1">
      ${card(t("dash.serviziInEvidenza"), `<div id="dash-svc">${servicesMini(s)}</div>`,
        `<span class="muted" data-vai="services" style="cursor:pointer">gestisci →</span>`)}
      ${card(t("dash.sistemaRouter"), `<div id="dash-sys">${dashRouter(s)}</div>`,
        `<span id="dash-sys-testa">${dashRouterTesta()}</span>`)}
    </div>`;
  drawTrafficChart($("#dash-traffic"), s.traffic_series || []);
  // "giu' da 3g" accanto ai servizi caduti: arriva dallo storico, non dallo
  // snapshot. La guardia `nuovi` evita il rimbalzo render -> carica -> render.
  loadStatiStorici().then(nuovi => {
    if (nuovi && state.route === "dashboard") renderIfLive();
  });
}

function dashTrafficoTesta(s) {
  const f = finestraTraffico(s);
  return `${badgeSorgente("traffic")}
    ${f ? `<span class="muted">${h(f)}</span> · ` : ""}
    <span class="muted" data-vai="stats" style="cursor:pointer">storico →</span>`;
}

function dashRouterTesta() {
  return `${badgeSorgente("system")} ${etaSorgente("system")}`;
}

/* Aggiornamento parziale: la dashboard e' la pagina di partenza e restava
   aperta per ore riscrivendosi INTERA ogni 10 secondi — DOM ricostruito,
   canvas distrutto e ricreato, selezione di testo persa mentre si leggeva.
   Qui si riscrivono solo i pezzi che cambiano. Ritorna false finche' la
   pagina non e' stata costruita, cosi' renderIfLive ricade sul render pieno. */
function refreshDashboard(view, s) {
  const kpiBox = $("#dash-kpi");
  if (!kpiBox || !$("#dash-traffic")) return false;
  kpiBox.innerHTML = dashKpi(s);
  $("#dash-traffico-num").innerHTML = dashTrafficoNum(s);
  $("#dash-traffico-testa").innerHTML = dashTrafficoTesta(s);
  // Card intera, non solo il contenuto: quando l'ultimo alert rientra la card
  // deve sparire, e quando ne arriva uno deve comparire senza ricaricare.
  $("#dash-alerts").innerHTML = dashAlertBox(s);
  $("#dash-wg").innerHTML = wgMini(s);
  $("#dash-sys").innerHTML = dashRouter(s);
  $("#dash-sys-testa").innerHTML = dashRouterTesta();
  $("#dash-svc").innerHTML = servicesMini(s);
  aggiornaNoteSorgente(["traffic"]);
  drawTrafficChart($("#dash-traffic"), s.traffic_series || []);
  return true;
}

/* Indice cliccabile: ogni riga porta alla pagina che possiede l'oggetto.
   E' l'unico punto in cui compaiono anche gli avvisi di sicurezza, perche' e'
   da qui che si trovano. */
function alertIndice(alerts) {
  if (!alerts.length) return `<div class="empty">${h(t("alert.nessuno"))}</div>`;
  return `<table><tbody>${alerts.map(a => {
    const dot = a.level === "critical" ? "s-down" : a.level === "warn" ? "s-warn" : "s-off";
    return `<tr class="${a.level === "info" ? "" : "alert-row " + h(a.level)}" data-vai="${h(a.scope)}" style="cursor:pointer">
      <td><span class="status-dot ${dot}"></span>${h(a.title)}
        <div class="muted">${h(a.detail)}</div></td>
      <td class="right muted nowrap">${h(PAGES[a.scope] ? t(PAGES[a.scope].title) : a.scope)}<br>${h(t("tempo.da", { v: durata(a.since) }))}</td>
    </tr>`;
  }).join("")}</tbody></table>`;
}

function alertConteggi(s) {
  const r = s.alerts_summary || {};
  if (!r.total) return "";
  const parti = [];
  if (r.critical) parti.push(`<span class="err">${r.critical} critici</span>`);
  if (r.warn) parti.push(`<span style="color:var(--orange)">${r.warn} da guardare</span>`);
  if (r.info) parti.push(`<span class="muted">${r.info} info</span>`);
  return parti.join(" · ");
}

/* "Va o non va", con la chiave giusta per ogni famiglia. I container Docker
   NON hanno `ok`: il loro booleano si chiama `running` (vedi ContainerInfo.
   to_dict in docker_client.py, dove `status` e' lo stato macchina e `state` la
   stringa leggibile). Leggere `ok` su un container da `undefined`, cioe' un
   pallino rosso e un "giu' da" sotto ogni container acceso. */
function serviceOk(x) {
  return x.kind === "docker" ? !!x.running : !!x.ok;
}

/* Stato in italiano al posto della stringa grezza del motore: "Up 3 days" e
   "active" dicono la stessa cosa in due lingue diverse, e in dashboard servono
   tre parole confrontabili a colpo d'occhio, non il gergo di ogni sorgente. */
function statoLeggibile(x) {
  if (x.kind === "docker") return x.running ? "attivo" : "fermo";
  if (x.kind === "systemd") {
    if (!x.available) return "sconosciuto";
    if (x.active_state === "active") return "attivo";
    if (x.active_state === "failed") return "in errore";
    return x.active_state === "unknown" ? "sconosciuto" : "fermo";
  }
  if (x.kind === "windows_service") {
    // Senza questo ramo la voce cadrebbe in fondo, fra gli healthcheck, e la
    // colonna direbbe "NaN ms" per un servizio che non ha nessuna latenza.
    if (!x.available) return "sconosciuto";
    if (x.state === "not-found") return t("servizi.nonInstallato");
    return x.state === "running" ? "attivo" : "fermo";
  }
  return x.ok ? `${Math.round(x.latency_ms)} ms` : t("servizi.nonRisponde");
}

/* Il chip dice lo stato in una parola; il motivo per esteso sta nel title,
   perche' `detail` puo' essere lungo duecento caratteri e in tabella sfonderebbe
   la colonna proprio nella riga che si vuole leggere. */
function chipStato(x) {
  const ok = serviceOk(x);
  const senzaStato = x.kind === "systemd" || x.kind === "windows_service";
  const ignoto = senzaStato && !x.available;
  const cls = ignoto ? "ignoto" : ok ? "ok" : "giu";
  const titolo = x.kind === "healthcheck" && !ok && x.detail ? x.detail
    : x.kind === "systemd" ? `${x.active_state || "?"}/${x.sub_state || "?"}`
    : x.kind === "windows_service" ? (x.error || `avvio: ${x.start_type || "?"}`)
    : x.kind === "docker" ? (x.status || "") : "";
  return `<span class="chip ${cls}"${titolo ? ` title="${h(titolo)}"` : ""}>${h(statoLeggibile(x))}</span>`;
}

/* Servizi in evidenza: quelli che il proprietario ha scelto con la spunta, di
   qualunque famiglia. Prima erano i container pinnati piu' le unit critiche,
   cioe' due flag nati per altro (il pin ordina la tabella, `critical` alza un
   alert) usati come se dicessero "voglio vederlo in prima pagina". */
function servicesMini(s) {
  const svc = s.services || {};
  const scelti = [
    ...(svc.docker?.containers || []),
    ...(svc.systemd || []),
    ...(svc.windows_services || []),
    ...(svc.healthchecks || []),
  ].filter(x => x.dashboard);

  if (!scelti.length) return `<div class="empty" data-vai="services" style="cursor:pointer">
    Nessun servizio scelto per la dashboard.<br>
    <span class="muted" style="font-size:12px">Aprine uno da Monitoraggio e spunta
    "mostra in dashboard".</span></div>`;

  // Chi non va sta in cima: e' l'unica riga che chiede di essere letta subito.
  const righe = scelti.slice().sort((a, b) =>
    (serviceOk(a) === serviceOk(b) ? 0 : serviceOk(a) ? 1 : -1) ||
    String(a.label || a.name).localeCompare(String(b.label || b.name)));

  return `<table><tbody>${righe.map(x => {
    const nome = x.label || x.name;
    const ok = serviceOk(x);
    // L'URL c'e' solo dove ha senso: il pin dei container e gli healthcheck
    // HTTP, il cui bersaglio E' gia' l'indirizzo da aprire. systemd non ne ha.
    const url = safeHref(x.kind === "healthcheck" && x.type === "http" ? x.target : x.url);
    const punto = ok ? "s-on"
      : ((x.kind === "systemd" || x.kind === "windows_service") && !x.available
        ? "s-off" : "s-down");
    return `<tr data-vai="services" style="cursor:pointer" title="apri Monitoraggio">
      <td><span class="status-dot ${punto}"></span>${url
        ? `<a href="${h(url)}" target="_blank" rel="noopener" title="apri ${h(nome)}">${h(nome)} ↗</a>`
        : h(nome)}
        ${ok ? "" : giuDa(x.kind, x.host, x.name)}</td>
      <td class="right">${chipStato(x)}</td>
      <td class="right muted" style="width:1%">→</td></tr>`;
  }).join("")}</tbody></table>`;
}

/* ── Dispositivi (+ mappa) ────────────────────────────────────────── */
let devFilter = { subnet: "", status: "", q: "", showHidden: false, detailKey: "" };
/* Scan manuale: resta "in corso" finche' non arriva un risultato piu' recente
   di quello mostrato al momento del click (o finche' non scade il timeout). */
let devScan = { pending: false, sinceTs: 0, clickedAt: 0 };

/* Aggiornamento parziale della pagina Dispositivi: tabella, mappa e stato del
   pulsante di scan. I controlli (ricerca, filtri) NON vengono ricostruiti, cosi'
   scrivere nel campo di ricerca mentre arriva un update non fa perdere il cursore.
   Ritorna false se la pagina non e' ancora stata costruita (serve il render pieno). */
function refreshDevices(view, s) {
  if (!$("#dlist") || !$("#lanmap")) return false;
  // Le subnet arrivano dallo snapshot: se cambiano, la tendina va rifatta.
  if ($("#dsub").options.length !== metaSubnets().length + 1) return false;
  aggiornaNoteSorgente(["devices"]);
  syncScanButton((s.devices_summary || {}).ts || 0);
  aggiornaVistaDevice();
  return true;
}

/* Il pulsante resta "in corso" finche' non arriva un risultato piu' recente di
   quello mostrato al click (o finche' non scade il timeout). */
function syncScanButton(scanTs) {
  if (devScan.pending && (scanTs > devScan.sinceTs || Date.now() - devScan.clickedAt > 120000))
    devScan.pending = false;
  const b = $("#dscan");
  if (!b) return;
  b.disabled = devScan.pending;
  b.textContent = devScan.pending ? "scansione in corso…" : "↻ Scansiona";
}

function pageDevices(view, s) {
  const scanTs = (s.devices_summary || {}).ts || 0;
  view.innerHTML = `
    ${alertBox("devices")}
    ${notaSorgente("devices")}
    <div class="mapwrap" style="margin-bottom:16px">
      <svg id="lanmap"></svg>
      <div class="map-zoom" id="map-zoom">
        <button class="btn" data-zoom="in" title="Ingrandisci">+</button>
        <button class="btn" data-zoom="out" title="Riduci">−</button>
        <button class="btn" data-zoom="reset" title="Torna alla vista intera">⤢</button>
      </div>
      <div class="map-legend" id="map-legend"></div>
    </div>
    <div class="controls">
      <input type="text" id="dq" placeholder="cerca nome / IP / MAC…" value="${h(devFilter.q)}">
      <select id="dsub">
        <option value="">${h(t("device.tutteLeSubnet"))}</option>
        ${metaSubnets().map(s => `<option value="${h(s.label)}">${h(s.label)} · ${h(s.cidr)}</option>`).join("")}
      </select>
      <div class="seg" id="dstatus">
        ${[["", "tutti"], ["online", "online"], ["offline", "offline"]].map(([v, lbl]) =>
          `<button data-v="${v}"${devFilter.status === v ? ' class="active"' : ""}>${lbl}</button>`).join("")}
      </div>
      <button class="btn" id="dscan">↻ Scansiona</button>
      <button class="btn" id="dadd" style="border-color:var(--teal);color:var(--teal)">+ Aggiungi</button>
      <label class="muted" style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="dhidden"> mostra nascosti</label>
      <span class="muted" id="dcount" style="margin-left:auto"></span>
    </div>
    <div id="dlist"></div>`;

  loadStatiStorici().then(nuovi => {
    if (nuovi && state.route === "devices") renderDeviceList();
  });
  $("#dsub").value = devFilter.subnet;
  $("#dhidden").checked = devFilter.showHidden;
  $("#dhidden").onchange = (e) => { devFilter.showHidden = e.target.checked; aggiornaVistaDevice(); };
  $("#dq").oninput = (e) => { devFilter.q = e.target.value; aggiornaVistaDevice(); };
  $("#dsub").onchange = (e) => { devFilter.subnet = e.target.value; aggiornaVistaDevice(); };
  $("#dstatus").querySelectorAll("button").forEach(b => b.onclick = () => {
    $("#dstatus").querySelectorAll("button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); devFilter.status = b.dataset.v; aggiornaVistaDevice();
  });
  $("#dscan").onclick = async (e) => {
    // Il riferimento e' il risultato piu' recente al momento del click, non
    // quello dell'ultimo render pieno: la pagina ora si aggiorna per parti.
    devScan = { pending: true, sinceTs: (state.snap.devices_summary || {}).ts || 0,
                clickedAt: Date.now() };
    e.target.disabled = true; e.target.textContent = "scansione in corso…";
    const r = await api("/api/devices/scan", { method: "POST" });
    if (!r.ok) {                 // cooldown o backend giu': niente attesa infinita
      devScan.pending = false;
      e.target.disabled = false;
      e.target.textContent = r.error.kind === "limite" ? "attendi…" : "↻ Scansiona";
      toastErrore(r.error);
    }
  };
  $("#dadd").onclick = () => openAddModal();
  syncScanButton(scanTs);
  aggiornaVistaDevice();
}

/* Chiave d'ordinamento: subnet, poi IP in ordine numerico, poi nome.
   `registry.all()` restituisce l'ordine di scoperta e il registro viene
   ricostruito ad ogni scan: senza ordinare, l'elenco si rimescolava ogni
   minuto sotto il cursore. Non si ordina per stato acceso/spento apposta —
   un dispositivo che si spegne salterebbe di posto proprio mentre lo guardi. */
function devOrdine(d) {
  const ip = (d.ips || [])[0] || "";
  const n = ip.split(".").map(Number);
  const num = n.length === 4 && n.every(x => Number.isFinite(x))
    ? ((n[0] * 256 + n[1]) * 256 + n[2]) * 256 + n[3]
    : Number.MAX_SAFE_INTEGER;      // i device senza IP (solo MAC) vanno in fondo
  return [d.subnet || "\uffff", num, (d.name || "").toLowerCase()];
}

function filteredDevices() {
  const q = devFilter.q.toLowerCase();
  return (state.snap.devices || []).filter(d => {
    if (d.hidden && !devFilter.showHidden) return false;
    if (devFilter.subnet && d.subnet !== devFilter.subnet) return false;
    if (devFilter.status === "online" && !d.online) return false;
    if (devFilter.status === "offline" && d.online) return false;
    if (q) {
      const hay = `${d.name} ${d.hostname} ${d.mac} ${(d.ips || []).join(" ")} ${d.os}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }).sort((a, b) => {
    const x = devOrdine(a), y = devOrdine(b);
    return x[0].localeCompare(y[0]) || (x[1] - y[1]) || x[2].localeCompare(y[2]);
  });
}

/* Elenco + mappa insieme: i filtri valgono per entrambi, altrimenti la mappa
   continua a disegnare quello che l'elenco ha appena escluso. */
function aggiornaVistaDevice() {
  renderDeviceList();
  startMap(filteredDevices());
}

/* Porte aperte come chip. Sopra le otto si tronca, ma dicendolo: prima le
   eccedenti sparivano in silenzio e la riga sembrava completa. */
function portChips(porte) {
  const p = porte || [];
  if (!p.length) return "";
  const mostrate = p.slice(0, 8).map(x => `<span>${h(x)}</span>`).join("");
  return `<div class="dev-ports">${mostrate}${
    p.length > 8 ? `<span class="piu" title="${h(p.join(", "))}">+${p.length - 8}</span>` : ""}</div>`;
}

/* Elenco a righe: per confrontare venti dispositivi una tabella si scorre con
   gli occhi, una griglia di schede no. Il dettaglio invece e' una scheda, che
   compare sotto la riga quando ci si clicca. */
function renderDeviceList() {
  const box = $("#dlist"); if (!box) return;
  const list = filteredDevices();
  const hiddenTot = (state.snap.devices || []).filter(d => d.hidden).length;
  const c = $("#dcount");
  if (c) c.textContent = `${list.length} dispositivi` + (hiddenTot ? ` · ${hiddenTot} nascosti` : "");
  if (!list.length) {
    box.innerHTML = `<div class="card"><div class="empty">${h(t("device.nessunoConFiltri"))}</div></div>`;
    return;
  }
  box.innerHTML = `<div class="card" style="padding:0"><table><thead><tr>
      <th></th><th>${h(t("device.colDispositivo"))}</th><th>IP</th><th>MAC</th><th>${h(t("device.colTipoOs"))}</th>
      <th>${h(t("comune.porte"))}</th><th>${h(t("comune.sorgenti"))}</th><th>${h(t("comune.stato"))}</th><th></th>
    </tr></thead><tbody>${list.map(d => {
      const aperta = d.key === devFilter.detailKey;
      return `<tr class="dev-riga${aperta ? " aperta" : ""}"${d.hidden ? ' style="opacity:.5"' : ""}>
        <td class="apri"><button class="expand${aperta ? " open" : ""}" title="Dettagli"
          data-act="detail" data-key="${h(d.key)}">▸</button></td>
        <td><a href="#" class="link" data-act="detail" data-key="${h(d.key)}">${h(d.name)}</a>
            ${safeHref(d.url) ? ` <a href="${h(safeHref(d.url))}" target="_blank" rel="noopener" title="${h(t("device.apriPannello"))}">↗</a>` : ""}
            ${d.hidden ? `<span class="tag">nascosto</span>` : ""}
            ${d.vendor ? `<div class="muted" style="font-size:11px">${h(d.vendor)}</div>` : ""}</td>
        <td class="mono">${(d.ips || []).map(h).join("<br>") || "—"}</td>
        <td class="mono muted">${h(d.mac || "—")}</td>
        <td>${h(d.type)}${d.os ? `<div class="muted" style="font-size:11px">${h(d.os)}</div>` : ""}</td>
        <td>${portChips(d.open_ports) || "—"}</td>
        <td>${(d.discovered_by || []).map(x => `<span class="tag teal">${h(x)}</span>`).join("")}</td>
        <td class="nowrap"><span class="status-dot ${d.online ? "s-on" : "s-off"}"></span>${d.online
          ? `online${d.latency_ms ? ` <span class="muted">${h(d.latency_ms)}ms</span>` : ""}` : "offline"}</td>
        <td class="nowrap right">
          <button class="iconbtn" title="${h(t("device.analizzaNeiTools"))}" data-act="probe" data-key="${h(d.key)}">⌖</button>
          <button class="iconbtn" title="${h(t("comune.modifica"))}" data-act="edit" data-key="${h(d.key)}">✎</button>
          ${d.hidden
            ? `<button class="iconbtn" title="${h(t("device.mostraDiNuovo"))}" data-act="unhide" data-key="${h(d.key)}">↺</button>`
            : `<button class="iconbtn" title="${h(t("comune.nascondi"))}" data-act="hide" data-key="${h(d.key)}">⊘</button>`}
          <button class="iconbtn" title="${h(t("comune.rimuovi"))}" data-act="delete" data-key="${h(d.key)}">✕</button>
        </td>
      </tr>${aperta ? `<tr class="dev-scheda"><td colspan="9">${deviceCard(d)}</td></tr>` : ""}`;
    }).join("")}</tbody></table></div>`;
  box.querySelectorAll("[data-act]").forEach(b =>
    b.onclick = (e) => { e.preventDefault(); deviceAction(b.dataset.act, b.dataset.key); });
}

/* Scheda del dispositivo: compare sotto la riga, non al posto dell'elenco.
   Accanto ai campi ci sono le interfacce di rete dell'host, una card per
   ciascuna — il dato arriva dai facts SSH, quindi c'e' solo per gli host
   configurati; per tutti gli altri lo si dice invece di lasciare il vuoto. */
function deviceCard(d) {
  const row = (k, v) => v ? `<div class="kv"><span class="muted">${k}</span><span class="mono">${v}</span></div>` : "";
  return `<div class="dev-card">
    <div class="detail-grid">
      ${row(t("comune.hostname"), h(d.hostname))}
      ${row(t("comune.nome"), h(d.name))}
      ${row(t("comune.tipo"), h(d.type))}
      ${row("MAC", h(d.mac))}
      ${row("IP", (d.ips || []).map(h).join(", "))}
      ${row(t("comune.subnet"), h(d.subnet))}
      ${row("OS", h(d.os))}
      ${row(t("comune.vendor"), h(d.vendor))}
      ${row(t("device.porteAperte"), (d.open_ports || []).join(", "))}
      ${row(t("comune.sorgenti"), (d.discovered_by || []).join(", "))}
      ${row(t("comune.servizi"), (d.services || []).map(h).join(", "))}
      ${row(t("comune.stato"), d.online ? `online${d.latency_ms ? " · " + d.latency_ms + " ms" : ""}` : "offline")}
      ${row(t(d.online ? "device.accesoDa" : "device.spentoDa"), daQuando(statiStorici.devices[d.key], d.online))}
      ${row(t("device.ultimoVisto"), d.last_seen ? ago(d.last_seen) : "mai")}
      ${row("URL", d.url ? (safeHref(d.url) ? `<a href="${h(safeHref(d.url))}" target="_blank" rel="noopener">${h(d.url)}</a>` : h(d.url)) : "")}
      ${d.notes ? `<div class="kv" style="grid-column:1/-1"><span class="muted">${h(t("comune.note"))}</span><span>${h(d.notes)}</span></div>` : ""}
    </div>
    ${ifaceSection(d)}
  </div>`;
}

function ifaceSection(d) {
  const ifaces = d.interfaces || [];
  const titolo = `<div class="iface-testa">Interfacce di rete${
    ifaces.length ? ` <span class="muted">${ifaces.length} · da <code>ip -j addr show</code> via SSH</span>` : ""}</div>`;
  if (!ifaces.length)
    return `${titolo}<div class="muted" style="font-size:12px">Nessuna interfaccia rilevata:
      si leggono via SSH, quindi ci sono solo per gli host configurati in
      <span class="link" data-vai="settings" style="cursor:pointer">${h(t("impostazioni.vaiDiscovery"))}</span>.</div>`;
  return `${titolo}<div class="iface-grid">${ifaces.map(ifaceCard).join("")}</div>`;
}

function ifaceCard(i) {
  const su = i.state === "up" || i.state === "unknown";
  const meta = [];
  const v4 = (i.addresses || []).filter(a => a.family === "inet");
  const v6 = (i.addresses || []).filter(a => a.family === "inet6");
  const riga = (k, v) => v ? `<div><span class="lbl">${k}</span><span class="val">${v}</span></div>` : "";
  meta.push(riga("IPv4", v4.map(a => h(`${a.ip}/${a.prefix}`)).join(", ")));
  meta.push(riga("IPv6", v6.map(a => h(`${a.ip}/${a.prefix}`)).join(", ")));
  meta.push(riga("MAC", h(i.mac)));
  meta.push(riga("MTU", h(i.mtu)));
  meta.push(riga("master", h(i.master)));
  return `<div class="iface-card${su ? "" : " giu"}">
    <div class="iface-testa-card">
      <span class="iface-nome" title="${h(i.name)}">${h(i.name)}</span>
      <span class="chip ${su ? "ok" : "ignoto"}">${h(i.state || "?")}</span>
    </div>
    <div class="iface-meta">${meta.join("")}</div>
  </div>`;
}

/* Ultima transizione nota di device e servizi, dallo storico su disco.
   Una sola richiesta per tutta la pagina invece di una per riga; i timestamp
   possono precedere qualunque finestra, che e' proprio il caso interessante
   ("giu' da tre giorni"). Vuoto finche' lo storico non risponde: in quel caso
   la UI semplicemente non mostra il "da quando", non mostra un'ora sbagliata. */
const statiStorici = { devices: {}, services: {}, chiesto: 0 };

/* Ritorna true solo quando ha davvero caricato dati nuovi.
   Il valore serve a chi ridisegna la pagina dopo la chiamata: ridisegnare
   comunque significherebbe rientrare in questa funzione, e sulle pagine senza
   refresh parziale (Servizi) il rimbalzo non finirebbe mai. */
async function loadStatiStorici() {
  // Non piu' di una volta al minuto: cambia solo quando qualcosa va su o giu'.
  if (Date.now() - statiStorici.chiesto < 60000) return false;
  statiStorici.chiesto = Date.now();
  // auth:false: e' un arricchimento di sfondo ("giu' da 3g" accanto alle
  // righe), non un'azione dell'utente: non deve far comparire un login.
  const r = await api("/api/history/states", { auth: false });
  if (!r.ok) return false;                   // riprovera' fra un minuto
  const d = r.data;
  statiStorici.devices = {};
  (d.devices || []).forEach(x => { statiStorici.devices[x.key] = x; });
  statiStorici.services = {};
  (d.services || []).forEach(x => {
    statiStorici.services[`${x.kind}\u0000${x.host || ""}\u0000${x.name}`] = x;
  });
  return true;
}

/* "da 3g" a partire da un istante in millisecondi, solo se lo stato storico
   combacia con quello attuale: se non combaciano lo storico e' indietro di un
   ciclo e dire "offline da due giorni" per un device appena riacceso sarebbe
   peggio che non dire niente. */
function daQuando(stato, atteso) {
  if (!stato || stato.since == null) return "";
  const suGiu = stato.online !== undefined ? stato.online : stato.ok;
  if (suGiu !== atteso) return "";
  return ago(Math.floor(stato.since / 1000));
}

/* Azioni dispositivo: aggiornamento ottimistico locale + chiamata API. */
function deviceMatch(key) { return (state.snap.devices || []).find(d => d.key === key); }

async function deviceAction(act, key) {
  const d = deviceMatch(key);
  if (act === "detail") {
    devFilter.detailKey = devFilter.detailKey === key ? "" : key;
    return renderDeviceList();
  }
  if (act === "edit") return openEditModal(d, key);
  // "Analizza": porta il device nei tools di rete gia' compilato.
  if (act === "probe") return toolsSetTarget(d && d.ips && d.ips[0] ? d.ips[0] : key);
  if (act === "hide") {
    if (!confirm(`Nascondere "${d ? d.name : key}"? Resta escluso anche se la discovery lo ritrova (reversibile da "mostra nascosti").`)) return;
    if (d) d.hidden = true; aggiornaVistaDevice();
    const r = await api(`/api/devices/${encodeURIComponent(key)}/hide`, { method: "POST" });
    // Senza il rientro la riga resterebbe nascosta nella tabella pur essendo
    // ancora visibile nel catalogo: la pagina direbbe il falso fino al reload.
    if (!r.ok) { if (d) d.hidden = false; aggiornaVistaDevice(); toastErrore(r.error); }
  }
  if (act === "unhide") {
    if (d) d.hidden = false; aggiornaVistaDevice();
    const r = await api(`/api/devices/${encodeURIComponent(key)}/unhide`, { method: "POST" });
    if (!r.ok) { if (d) d.hidden = true; aggiornaVistaDevice(); toastErrore(r.error); }
  }
  if (act === "delete") {
    if (!confirm(`Rimuovere "${d ? d.name : key}" dal catalogo? Se viene riscoperto dalla discovery restera' comunque nascosto.`)) return;
    const prima = state.snap.devices || [];
    state.snap.devices = prima.filter(x => x.key !== key);   // update ottimistico
    aggiornaVistaDevice();
    const r = await api(`/api/devices/${encodeURIComponent(key)}`, { method: "DELETE" });
    // Il device torna in elenco se la rimozione non e' passata: sparire e basta
    // faceva credere fatta un'operazione che il backend aveva rifiutato.
    if (!r.ok) { state.snap.devices = prima; aggiornaVistaDevice(); toastErrore(r.error); }
  }
}

function openEditModal(d, key) {
  d = d || { name: "", type: "unknown", os: "", notes: "", url: "" };
  const types = ["unknown", "router", "desktop", "laptop", "server", "vm", "mobile", "cloud", "ap", "printer"];
  const ov = document.createElement("div");
  ov.className = "modal-ov";
  ov.innerHTML = `<div class="modal">
    <h3>${h(t("device.modificaTitolo"))} <span class="muted mono">${h(key)}</span></h3>
    <label>${h(t("device.indirizzoIp"))}<input type="text" id="m-ip" class="mono" value="${h((d.ips || [])[0] || "")}" placeholder="${h(esempioIP())}"></label>
    <label>MAC<input type="text" id="m-mac" class="mono" value="${h(d.mac || "")}" placeholder="(facoltativo)"></label>
    <label>${h(t("comune.nome"))}<input type="text" id="m-name" value="${h(d.name || "")}"></label>
    <label>${h(t("comune.tipo"))}<select id="m-type">${types.map(t => `<option ${t === d.type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>${h(t("comune.sistemaOperativo"))}<input type="text" id="m-os" value="${h(d.os || "")}"></label>
    <label>URL pannello<input type="text" id="m-url" value="${h(d.url || "")}" placeholder="http://..."></label>
    <label>${h(t("comune.note"))}<input type="text" id="m-notes" value="${h(d.notes || "")}"></label>
    <div id="m-msg" class="cfg-note err" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn" id="m-cancel">${h(t("comune.annulla"))}</button>
      <button class="btn" id="m-save" style="border-color:var(--teal);color:var(--teal)">${h(t("comune.salva"))}</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  $("#m-cancel", ov).onclick = close;
  $("#m-save", ov).onclick = async () => {
    const body = {
      ip: $("#m-ip", ov).value.trim(), mac: $("#m-mac", ov).value.trim(),
      name: $("#m-name", ov).value, type: $("#m-type", ov).value,
      os: $("#m-os", ov).value, url: $("#m-url", ov).value, notes: $("#m-notes", ov).value,
    };
    const r = await api(`/api/devices/${encodeURIComponent(key)}`, { method: "PUT", body });
    // Indirizzo non valido o gia' di un altro device: il modale resta aperto
    // con i valori digitati, cosi' si corregge senza riscrivere tutto.
    if (!r.ok) {
      const m = $("#m-msg", ov);
      m.textContent = r.error.messaggio;
      m.style.display = "";
      return;
    }
    close();
    // Niente update ottimistico: cambiare IP cambia l'identita' del device,
    // quindi la riga corretta arriva dal refresh che il backend ha gia'
    // pubblicato sul WebSocket dopo aver salvato il catalogo.
  };
}

function openAddModal() {
  const types = ["unknown", "router", "desktop", "laptop", "server", "vm", "mobile", "cloud", "ap", "printer"];
  const ov = document.createElement("div");
  ov.className = "modal-ov";
  ov.innerHTML = `<div class="modal">
    <h3>${h(t("device.aggiungiTitolo"))}</h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">${h(t("device.serveMacOIp"))}</div>
    <label>MAC<input type="text" id="a-mac" placeholder="AA:BB:CC:DD:EE:FF"></label>
    <label>IP<input type="text" id="a-ip" placeholder="${h(esempioIP())}"></label>
    <label>${h(t("comune.nome"))}<input type="text" id="a-name"></label>
    <label>${h(t("comune.tipo"))}<select id="a-type">${types.map(t => `<option>${t}</option>`).join("")}</select></label>
    <label>${h(t("comune.sistemaOperativo"))}<input type="text" id="a-os"></label>
    <label>URL pannello<input type="text" id="a-url" placeholder="http://..."></label>
    <label>${h(t("comune.note"))}<input type="text" id="a-notes"></label>
    <div id="a-msg" class="cfg-note err" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn" id="a-cancel">${h(t("comune.annulla"))}</button>
      <button class="btn" id="a-save" style="border-color:var(--teal);color:var(--teal)">${h(t("comune.aggiungi"))}</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  $("#a-cancel", ov).onclick = close;
  $("#a-save", ov).onclick = async () => {
    const body = {
      mac: $("#a-mac", ov).value.trim(), ip: $("#a-ip", ov).value.trim(),
      name: $("#a-name", ov).value, type: $("#a-type", ov).value,
      os: $("#a-os", ov).value, url: $("#a-url", ov).value, notes: $("#a-notes", ov).value,
    };
    if (!body.mac && !body.ip) { const m = $("#a-msg", ov); m.textContent = t("device.inserisciMacOIp"); m.style.display = ""; return; }
    const r = await api("/api/devices/", { method: "POST", body });
    if (!r.ok) {
      const m = $("#a-msg", ov);
      m.textContent = r.error.messaggio;
      m.style.display = "";
      return;
    }
    close();
    // La scansione che segue e' un di piu': se non parte l'aggiunta e' comunque
    // riuscita, ma senza avviso il device sembrerebbe non essersi popolato.
    const sc = await api("/api/devices/scan", { method: "POST" });
    if (!sc.ok && sc.error.kind !== "limite")
      toast(t("msg.scanNonPartita") + sc.error.messaggio,
            { level: "warn", chiave: "scan-dopo-aggiunta" });
  };
}

/* ── Servizi ──────────────────────────────────────────────────────── */
/* Barra dei sotto-tab della sezione monitoraggio. Riusa la classe .seg dei
   filtri: stesso aspetto dei segmenti gia' presenti altrove. */
function monitorTabs(active) {
  return `<div class="subtabs-bar">
    <div class="seg subtabs" id="mon-tabs">
      ${[["services", "comune.servizi"], ["resources", "comune.risorse"]].map(([r, lbl]) =>
        `<button data-tab="${r}"${active === r ? ' class="active"' : ""}>${lbl}</button>`).join("")}
    </div>
    <span class="muted mono" id="mon-eta" title="tempo al prossimo aggiornamento automatico"></span>
    <button class="btn" id="mon-refresh">↻ Aggiorna ora</button>
  </div>`;
}
function bindMonitorTabs(view) {
  const bar = $("#mon-tabs", view);
  if (!bar) return;
  bar.querySelectorAll("button").forEach(b => b.onclick = () => go(b.dataset.tab));
  const btn = $("#mon-refresh", view);
  if (btn) btn.onclick = () => monitorRefreshNow();
  syncMonitorButton();
  startMonitorEta();
}

/* ── Countdown del monitoraggio ───────────────────────────────────── */
/* Il ciclo del collector e' lato backend: la UI non lo indovina, legge gli
   istanti pubblicati nello snapshot (`collector`, `resources.ts`) e li conta
   alla rovescia in locale, un tick al secondo. */
let monEta = null;          // handle dell'intervallo, null = fermo
let monBusy = false;        // aggiornamento manuale in corso
// Esito dell'ultimo aggiornamento manuale fallito (cooldown, backend occupato):
// resta al posto del countdown per qualche secondo, altrimenti il tick
// successivo lo cancellerebbe prima che si riesca a leggerlo.
let monMsg = { text: "", until: 0 };

/* Il pulsante vive dentro una pagina che si ridisegna ad ogni update: dopo
   ogni render va rimesso nello stato in cui era (come syncScanButton). */
function syncMonitorButton() {
  const b = $("#mon-refresh");
  if (!b) return;
  b.disabled = monBusy;
  b.textContent = monBusy ? "aggiorno…" : "↻ Aggiorna ora";
}

function monitorNextTs() {
  const col = state.snap.collector || {};
  if (col.slow_running) return "running";
  const next = col.next_slow_ts || 0;
  if (state.route !== "resources") return next;
  // Le risorse hanno un intervallo proprio, ma si raccolgono dentro il ciclo
  // lento: il primo istante utile e' il piu' lontano dei due.
  const r = state.snap.resources || {};
  if (r.enabled === false) return 0;
  return r.ts ? Math.max(r.ts + (r.interval || 0), next) : next;
}

function renderMonitorEta() {
  const el = $("#mon-eta");
  if (!el) return stopMonitorEta();       // pagina cambiata: niente da aggiornare
  if (monBusy) { el.textContent = "aggiornamento in corso…"; return; }
  if (monMsg.text && Date.now() < monMsg.until) { el.textContent = monMsg.text; return; }
  const target = monitorNextTs();
  if (target === "running") { el.textContent = "raccolta in corso…"; return; }
  if (!target) { el.textContent = ""; return; }
  const left = target - Math.floor(Date.now() / 1000);
  // "~": la raccolta lenta puo' slittare se la precedente e' ancora in corso.
  el.textContent = left > 0 ? `prossimo aggiornamento fra ~${left}s` : "a momenti…";
}

function startMonitorEta() {
  renderMonitorEta();
  if (!monEta) monEta = setInterval(renderMonitorEta, 1000);
}
function stopMonitorEta() {
  if (monEta) { clearInterval(monEta); monEta = null; }
}

async function monitorRefreshNow() {
  monBusy = true;
  monMsg = { text: "", until: 0 };
  syncMonitorButton();
  renderMonitorEta();
  const r = await api("/api/services/refresh", { method: "POST" });
  monBusy = false;
  // 429 cooldown, 409 raccolta gia' in corso, oppure backend irraggiungibile:
  // ora i tre casi si leggono distinti, con il tempo d'attesa quando c'e'.
  if (!r.ok) monMsg = { text: r.error.messaggio, until: Date.now() + 6000 };
  // La pagina puo' essersi gia' ridisegnata con i dati arrivati sul WebSocket:
  // si cerca il pulsante attuale, non quello su cui si e' cliccato.
  syncMonitorButton();
  renderMonitorEta();
}

/* "giu' da 3g" accanto a un servizio non ok. Vuoto se il servizio sta bene o
   se lo storico non sa ancora nulla di lui. */
function giuDa(kind, host, name) {
  const q = daQuando(statiStorici.services[`${kind}\u0000${host || ""}\u0000${name}`], false);
  return q ? `<span class="tag">${h(t("servizi.giuDa", { q }))}</span>` : "";
}

/* Le quattro tabelle stanno in funzioni a se': la pagina si aggiorna per parti
   (vedi refreshServices) invece di riscriversi intera ogni dieci secondi. */
function svcKpi(s) {
  const svc = s.services || {}, sum = svc.summary || {};
  const systemd = svc.systemd || [], health = svc.healthchecks || [];
  // I servizi dell'host, di qualunque sistema, stanno in un riquadro solo: la
  // griglia e' a quattro e un quinto riquadro romperebbe la riga.
  const suHost = systemd.concat(svc.windows_services || []);
  const dk = svc.docker?.summary || {};
  return `
    ${kpi(`${sum.ok ?? "–"}<span class="unit">/${sum.total ?? "–"}</span>`, "servizi ok",
      sum.total == null ? "" : (sum.down ? "orange" : "green"),
      { dot: sum.total == null ? "s-off" : (sum.down ? "s-down" : "s-on"), vai: "services",
        ctx: sum.total == null ? t("stato.inAttesa") : (sum.down ? conta(sum.down, "conta.nonRisponde") : t("stato.tuttiAttivi")) })}
    ${kpi(`${dk.running ?? "–"}<span class="unit">/${dk.total ?? "–"}</span>`, "container running", "",
      { ctx: dk.total == null ? "" : conta(dk.stopped || 0, "conta.fermo") })}
    ${kpi(`${suHost.filter(u => u.ok).length}<span class="unit">/${suHost.length}</span>`,
      "servizi host attivi", "",
      { ctx: conta(suHost.filter(u => u.critical).length, "conta.critico") })}
    ${kpi(`${health.filter(c => c.ok).length}<span class="unit">/${health.length}</span>`,
      "healthcheck up", health.some(c => !c.ok) ? "orange" : "",
      { ctx: health.length ? "" : t("servizi.nessunoConfigurato") })}`;
}

function svcDocker(s) {
  const svc = s.services || {};
  const containers = svc.docker?.containers || [];
  const host = `<div class="muted" style="padding:8px 12px;font-size:12px">host: ${
    (svc.docker?.hosts || []).map(x => x.reachable === false
      ? `<span class="err" title="${h(x.error || "")}">${h(x.name)} (non risponde)</span>`
      : h(x.name)).join(" · ") || "—"}</div>`;
  if (!containers.length) return `<div class="empty">${h(t("servizi.nessunContainer"))}</div>${host}`;
  return `<table><thead><tr>
      <th>${h(t("servizi.colContainer"))}</th><th>${h(t("comune.host"))}</th><th>${h(t("comune.stato"))}</th><th>CPU</th><th>RAM</th><th>${h(t("comune.porte"))}</th><th></th></tr></thead><tbody>
      ${containers.map(c => `<tr>
        <td>${safeHref(c.url) ? `<a href="${h(safeHref(c.url))}" target="_blank" rel="noopener">${h(c.label)}</a>` : h(c.label)}
          ${c.pinned ? `<span class="tag teal">pin</span>` : ""}
          ${c.dashboard ? `<span class="tag">dashboard</span>` : ""}
          <div class="muted" style="font-size:11px">${h(c.image)}</div></td>
        <td><span class="tag purple">${h(c.host)}</span></td>
        <td class="nowrap"><span class="status-dot ${c.running ? "s-on" : "s-off"}"></span>${h(c.status)}
          ${c.running ? "" : giuDa("docker", c.host, c.name)}</td>
        <td class="mono">${c.running ? (c.cpu_percent ?? 0) + "%" : "—"}</td>
        <td class="mono">${c.running ? fmtMB(c.mem_usage_mb) : "—"}</td>
        <td class="mono muted" style="font-size:11px">${(c.ports || []).map(h).join("<br>") || "—"}</td>
        <td class="right nowrap">${c.pinned
          ? `<button class="iconbtn" title="${h(t("servizi.modificaPin"))}" data-svc-edit="${h(c.name)}" data-kind="docker">✎</button><button class="iconbtn" title="${h(t("servizi.rimuoviPin"))}" data-svc-del="${h(c.name)}" data-kind="docker">✕</button>`
          : `<button class="iconbtn" title="${h(t("servizi.dammiNomeUrl"))}" data-svc-pin="${h(c.name)}">+</button>`}</td>
      </tr>`).join("")}</tbody></table>${host}`;
}

function svcSystemd(s) {
  const systemd = (s.services || {}).systemd || [];
  if (!systemd.length) return `<div class="empty">${h(t("servizi.nessunaUnit"))}</div>`;
  return `<table><tbody>${systemd.map(u => `<tr>
    <td><span class="status-dot ${u.ok ? "s-on" : (u.critical ? "s-down" : "s-off")}"></span>${h(u.label)}
      ${u.host ? `<span class="tag purple">${h(u.host)}</span>` : ""}
      ${u.critical ? `<span class="tag">critico</span>` : ""}
      ${u.dashboard ? `<span class="tag">dashboard</span>` : ""}
      ${u.ok ? "" : giuDa("systemd", u.host, u.name)}</td>
    <td class="right">${chipStato(u)}</td>
    <td class="right nowrap"><button class="iconbtn" title="${h(t("comune.modifica"))}" data-svc-edit="${h(u.name)}" data-kind="systemd">✎</button><button class="iconbtn" title="${h(t("servizi.smettiMonitorare"))}" data-svc-del="${h(u.name)}" data-kind="systemd">✕</button></td>
  </tr>`).join("")}</tbody></table>`;
}

function svcWindows(s) {
  const win = (s.services || {}).windows_services || [];
  if (!win.length) return `<div class="empty">${h(t("servizi.nessunWindows"))}</div>`;
  return `<table><tbody>${win.map(w => `<tr>
    <td><span class="status-dot ${w.ok ? "s-on" : (w.available ? (w.critical ? "s-down" : "s-off") : "s-off")}"></span>${h(w.label)}
      <span class="tag purple">${h(w.host)}</span>
      ${w.critical ? `<span class="tag">critico</span>` : ""}
      ${w.dashboard ? `<span class="tag">dashboard</span>` : ""}
      ${w.ok ? "" : giuDa("windows_service", w.host, w.name)}
      <div class="muted mono" style="font-size:11px">${h(w.name)}${
        w.start_type ? ` · avvio ${h(w.start_type)}` : ""}</div></td>
    <td class="right">${chipStato(w)}</td>
    <td class="right nowrap"><button class="iconbtn" title="${h(t("comune.modifica"))}" data-svc-edit="${h(w.name)}" data-kind="windows_service">✎</button><button class="iconbtn" title="${h(t("servizi.smettiMonitorare"))}" data-svc-del="${h(w.name)}" data-kind="windows_service">✕</button></td>
  </tr>`).join("")}</tbody></table>`;
}

function svcHealth(s) {
  const health = (s.services || {}).healthchecks || [];
  if (!health.length) return `<div class="empty">${h(t("servizi.nessunHealthcheck"))}</div>`;
  return `<table><tbody>${health.map(c => `<tr>
    <td><span class="status-dot ${c.ok ? "s-on" : "s-down"}"></span>${h(c.name)}
      <span class="tag">${h(c.type)}</span>
      ${c.dashboard ? `<span class="tag">dashboard</span>` : ""}
      ${c.ok ? "" : giuDa("healthcheck", "", c.name)}
      <div class="muted mono" style="font-size:11px">${h(c.target)}</div></td>
    <td class="right">${chipStato(c)}</td>
    <td class="right nowrap"><button class="iconbtn" title="${h(t("comune.modifica"))}" data-svc-edit="${h(c.name)}" data-kind="http">✎</button><button class="iconbtn" title="${h(t("servizi.smettiMonitorare"))}" data-svc-del="${h(c.name)}" data-kind="http">✕</button></td>
  </tr>`).join("")}</tbody></table>`;
}

/* Un solo gestore per i pulsanti delle tre tabelle: si riscrivono ad ogni
   update, e riagganciarli in tre punti significherebbe dimenticarsene in uno. */
function bindServiceActions(view) {
  view.querySelectorAll("[data-svc-del]").forEach(b =>
    b.onclick = () => removeService(b.dataset.kind, b.dataset.svcDel));
  view.querySelectorAll("[data-svc-edit]").forEach(b =>
    b.onclick = () => serviceEdit(b.dataset.kind, b.dataset.svcEdit));
  view.querySelectorAll("[data-svc-pin]").forEach(b =>
    b.onclick = () => openServiceModal({ method: "docker", name: b.dataset.svcPin, nuovo: true }));
}

/* Aggiornamento parziale: la pagina restava aperta a riscriversi intera ogni
   dieci secondi, perdendo la selezione del testo mentre la si leggeva. */
function refreshServices(view, s) {
  if (!$("#svc-kpi")) return false;
  $("#svc-kpi").innerHTML = svcKpi(s);
  $("#svc-docker").innerHTML = svcDocker(s);
  $("#svc-systemd").innerHTML = svcSystemd(s);
  $("#svc-windows").innerHTML = svcWindows(s);
  $("#svc-health").innerHTML = svcHealth(s);
  aggiornaNoteSorgente(["docker", "services"]);
  aggiornaBoxAlert();
  bindServiceActions(view);
  syncMonitorButton();
  return true;
}

function pageServices(view, s) {
  view.innerHTML = `
    ${monitorTabs("services")}
    ${alertBox("services")}
    ${notaSorgente("docker")}
    ${notaSorgente("services")}
    <div class="controls">
      <button class="btn" id="svc-add" style="border-color:var(--teal);color:var(--teal)">+ Monitora servizio</button>
      <span class="muted">${h(t("servizi.scegliMetodo"))}</span>
    </div>
    <div class="grid cols-4" style="margin-bottom:14px" id="svc-kpi">${svcKpi(s)}</div>
    <div class="grid cols-2-1">
      ${card(t("servizi.containerDocker"), `<div id="svc-docker">${svcDocker(s)}</div>`,
        `<span class="muted">${badgeSorgente("docker")} ${etaSorgente("docker")}</span>`)}
      <div>
        ${card(t("servizi.systemdHost"), `<div id="svc-systemd">${svcSystemd(s)}</div>`)}
        <div style="height:14px"></div>
        ${card(t("servizi.serviziWindows"), `<div id="svc-windows">${svcWindows(s)}</div>`)}
        <div style="height:14px"></div>
        ${card(t("servizi.healthcheck"), `<div id="svc-health">${svcHealth(s)}</div>`)}
      </div>
    </div>`;
  bindMonitorTabs(view);
  loadStatiStorici().then(nuovi => {
    if (nuovi && state.route === "services") renderIfLive();
  });
  $("#svc-add").onclick = () => openServiceModal();
  bindServiceActions(view);
}

/* Un'entry scritta prima che la spunta esistesse non ha il campo: qui si
   ricostruisce il valore che il backend le sta gia' applicando, altrimenti la
   modale mostrerebbe una casella vuota per un servizio che invece si vede in
   dashboard, e riaprire-e-salvare lo farebbe sparire senza averlo chiesto.
   La regola e' la stessa di systemd_monitor._in_dashboard e di _docker_section. */
function inDashboard(kind, e) {
  if (e.dashboard !== undefined) return !!e.dashboard;
  if (kind === "systemd") return !!e.critical;
  return kind === "docker";
}

/* Modifica servizio: recupera l'entry salvata dal catalogo e apre il modale
   pre-compilato. Il metodo (systemd/docker/http-type) resta bloccato. */
async function serviceEdit(kind, ident) {
  const rc = await api("/api/services/config");
  // Prima si usciva in silenzio: il clic su "modifica" non apriva nulla e
  // sembrava un pulsante rotto.
  if (!rc.ok) return void toastErrore(rc.error, { label: t("azione.riprova"), onclick: () => serviceEdit(kind, ident) });
  const cfg = rc.data;
  let ex = null;
  if (kind === "systemd") {
    const e = (cfg.systemd || []).find(x => x.unit === ident);
    if (e) ex = { method: "systemd", unit: e.unit, label: e.label, critical: e.critical, host: e.host,
                  dashboard: inDashboard("systemd", e) };
  } else if (kind === "windows_service") {
    const e = (cfg.windows || []).find(x => x.name === ident);
    if (e) ex = { method: "windows_service", name: e.name, label: e.label,
                  critical: e.critical, host: e.host,
                  dashboard: inDashboard("windows_service", e) };
  } else if (kind === "docker") {
    const e = (cfg.docker || []).find(x => x.name === ident);
    if (e) ex = { method: "docker", name: e.name, label: e.label, url: e.url,
                  dashboard: inDashboard("docker", e) };
  } else {
    const e = (cfg.http || []).find(x => x.name === ident);
    if (e) ex = { method: e.type || "http", name: e.name, url: e.url, host: e.host, port: e.port, expect_status: e.expect_status,
                  dashboard: inDashboard("http", e) };
  }
  if (ex) openServiceModal(ex);
}

/* Chiede al backend di ricalcolare subito la vista servizi: gli STATI (up/down)
   arrivano sul WebSocket in un paio di secondi invece che al prossimo giro
   lento (60s). Che la riga cancellata sparisca non dipende piu' da qui: lo
   snapshot lo pota il backend nella DELETE stessa.

   L'esito si guarda lo stesso. Il cooldown e' `max_hits=1` su 10 secondi ed e'
   **condiviso** con "↻ Aggiorna ora" e con il salvataggio di un servizio:
   due azioni ravvicinate danno 429, e dirlo e' meglio che lasciar credere che
   gli stati siano freschi quando hanno fino a un minuto. */
async function chiediRicalcoloServizi() {
  const r = await api("/api/services/refresh", { method: "POST", retry: false });
  if (!r.ok) toast(t("msg.statiAlProssimoGiro"),
                   { level: "info" });
}

/* Toglie subito il servizio dallo snapshot in memoria. Il catalogo su disco lo
   ha gia' riscritto il backend, ma `services` si ricalcola solo nel giro lento:
   senza questo la riga restava in pagina fino a un minuto dopo la cancellazione
   e il pulsante ✕ sembrava rotto (segnalato dal proprietario il 2026-09-03). */
function scordaServizio(kind, ident) {
  const svc = state.snap.services;
  if (!svc) return;
  if (kind === "docker") {
    // Il container resta, e' scoperto da solo: sparisce il pin, cioe'
    // etichetta, URL e la scelta di mostrarlo in dashboard. Conteggi invariati.
    const c = (svc.docker?.containers || []).find(x => x.name === ident);
    if (c) { c.pinned = false; c.dashboard = false; c.url = ""; c.label = c.name; }
    return;
  }
  const lista = kind === "systemd" ? svc.systemd
    : kind === "windows_service" ? svc.windows_services
    : svc.healthchecks;
  const i = (lista || []).findIndex(x => x.name === ident);
  if (i < 0) return;
  const era = !!lista[i].ok;
  lista.splice(i, 1);
  const sum = svc.summary;
  if (!sum) return;
  if (sum.total) sum.total -= 1;
  if (era && sum.ok) sum.ok -= 1;
  if (!era && sum.down) sum.down -= 1;
}

async function removeService(kind, ident) {
  if (!confirm(`Smettere di monitorare "${ident}"?`)) return;
  const r = await api(`/api/services/config/${encodeURIComponent(kind)}/${encodeURIComponent(ident)}`,
    { method: "DELETE" });
  // 404 = nel catalogo non c'era (un container mai pinnato, o una voce che sta
  // solo nel file di esempio). Prima il backend rispondeva 200 con `count: 0` e
  // la pagina dichiarava riuscita una cancellazione che non era avvenuta.
  if (!r.ok) return void toastErrore(r.error);
  scordaServizio(kind, ident);
  renderIfLive();
  toast(kind === "docker"
    ? `"${ident}" non e' piu' nel catalogo: il container resta, senza nome ne' URL.`
    : `"${ident}" non e' piu' monitorato.`, { level: "ok" });
  await chiediRicalcoloServizi();
}

/* Modale servizio: aggiungi o (se `ex` passato) modifica. Il metodo scelto
   determina i campi; in modifica il metodo e' bloccato. */
function openServiceModal(ex) {
  // `nuovo` = si sta pinnando un container gia' scoperto: il metodo resta
  // bloccato su docker e il nome e' quello vero, ma non e' una modifica.
  const editing = !!ex;
  ex = ex || {};
  const hosts = knownHosts();
  const ov = document.createElement("div");
  ov.className = "modal-ov";
  ov.innerHTML = `<div class="modal">
    <h3>${h(ex.nuovo ? t("servizi.aggiungiCatalogo") : editing ? t("servizi.modificaServizio") : t("servizi.monitoraServizio"))}</h3>
    <label>${h(t("servizi.metodo"))}<select id="sv-method" ${editing ? "disabled" : ""}>
      <option value="systemd">systemd (unit dell'host)</option>
      <option value="windows_service">${h(t("servizi.windowsSsh"))}</option>
      <option value="http">HTTP (URL)</option>
      <option value="tcp">TCP (host:porta)</option>
      <option value="ping">ping (host raggiungibile)</option>
      <option value="docker">${h(t("servizi.dockerPin"))}</option>
    </select></label>
    <div id="sv-fields"></div>
    <div id="sv-msg" class="cfg-note err" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn" id="sv-cancel">${h(t("comune.annulla"))}</button>
      <button class="btn" id="sv-save" style="border-color:var(--teal);color:var(--teal)">${h(editing && !ex.nuovo ? t("comune.salva") : t("comune.aggiungi"))}</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  $("#sv-cancel", ov).onclick = close;

  /* `obbligatorio` non e' un vezzo: per systemd l'host vuoto significa "la
     macchina che ospita LANMng", per un servizio Windows quella macchina e'
     Linux e il servizio non ci puo' essere. */
  const hostField = (val, obbligatorio) => `
    <label>${h(t("servizi.hostSsh"))} <span class="muted">${obbligatorio
      ? t("servizi.hostObbligatorio")
      : "opzionale, default host locale"}</span>
      <input type="text" id="sv-host2" list="sv-hostlist" value="${h(val || "")}" placeholder="IP/hostname">
      <datalist id="sv-hostlist">${hosts.map(x => `<option value="${h(x)}">`).join("")}</datalist></label>`;
  /* Vale per tutti e cinque i metodi: la dashboard mostra quello che il
     proprietario ha scelto, non quello che il codice indovina. Spuntata di
     default su un servizio nuovo; in modifica `serviceEdit` ha gia' risolto
     il valore effettivo, default storici compresi. */
  const dashField = `
      <label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox"
        id="sv-dash" ${ex.dashboard !== false ? "checked" : ""}> mostra in dashboard</label>`;
  const fieldsFor = (m) => {
    if (m === "systemd") return `
      <label>${h(t("servizi.unit"))}<input type="text" id="sv-unit" value="${h(ex.unit || "")}" placeholder="es. nginx.service"></label>
      <label>${h(t("comune.etichetta"))}<input type="text" id="sv-label" value="${h(ex.label || "")}" placeholder="nome leggibile"></label>
      ${hostField(ex.host)}
      <label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="sv-critical" ${ex.critical ? "checked" : ""}> critico (alert se giù)</label>
      ${dashField}`;
    if (m === "windows_service") return `
      <label>${h(t("servizi.servizio"))}<input type="text" id="sv-name" value="${h(ex.name || "")}" placeholder="nome breve, es. Spooler"></label>
      <label>${h(t("comune.etichetta"))}<input type="text" id="sv-label" value="${h(ex.label || "")}" placeholder="nome leggibile"></label>
      ${hostField(ex.host, true)}
      <label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="sv-critical" ${ex.critical ? "checked" : ""}> critico (alert se giù)</label>
      ${dashField}`;
    if (m === "http") return `
      <label>${h(t("comune.nome"))}<input type="text" id="sv-name" value="${h(ex.name || "")}"></label>
      <label>URL<input type="text" id="sv-url" value="${h(ex.url || "")}" placeholder="http://host:porta/path"></label>
      <label>${h(t("servizi.statusAttesi"))}<input type="text" id="sv-expect" value="${h((ex.expect_status || []).join(","))}" placeholder="200,204 (vuoto = 200)"></label>
      ${dashField}`;
    if (m === "tcp") return `
      <label>${h(t("comune.nome"))}<input type="text" id="sv-name" value="${h(ex.name || "")}"></label>
      <label>${h(t("comune.host"))}<input type="text" id="sv-host" value="${h(ex.host || "")}" placeholder="IP o hostname (anche di rete)"></label>
      <label>${h(t("comune.porta"))}<input type="text" id="sv-port" value="${h(ex.port || "")}" placeholder="es. 5432"></label>
      ${dashField}`;
    if (m === "ping") return `
      <label>${h(t("comune.nome"))}<input type="text" id="sv-name" value="${h(ex.name || "")}"></label>
      <label>${h(t("comune.host"))}<input type="text" id="sv-host" value="${h(ex.host || "")}" placeholder="IP o hostname (anche di rete)"></label>
      ${dashField}`;
    return `
      <label>${h(t("servizi.nomeContainer"))}<input type="text" id="sv-name" value="${h(ex.name || "")}" placeholder="esatto come in docker ps"></label>
      <label>${h(t("comune.etichetta"))}<input type="text" id="sv-label" value="${h(ex.label || "")}"></label>
      <label>URL pannello<input type="text" id="sv-url" value="${h(ex.url || "")}" placeholder="http://..."></label>
      ${dashField}`;
  };
  const sel = $("#sv-method", ov);
  if (editing && ex.method) sel.value = ex.method;
  const render = () => { $("#sv-fields", ov).innerHTML = fieldsFor(sel.value); };
  sel.onchange = render; render();

  $("#sv-save", ov).onclick = async () => {
    const m = sel.value;
    const val = (id) => { const el = $("#" + id, ov); return el ? el.value.trim() : ""; };
    const body = { kind: ["docker", "systemd", "windows_service"].includes(m) ? m : "http", type: m,
                   dashboard: $("#sv-dash", ov).checked };
    if (m === "systemd") { body.unit = val("sv-unit"); body.label = val("sv-label"); body.critical = $("#sv-critical", ov).checked; const hh = val("sv-host2"); if (hh) body.host = hh; }
    else if (m === "windows_service") { body.name = val("sv-name"); body.label = val("sv-label"); body.critical = $("#sv-critical", ov).checked; body.host = val("sv-host2"); }
    else if (m === "docker") { body.name = val("sv-name"); body.label = val("sv-label"); body.url = val("sv-url"); }
    else { body.name = val("sv-name"); }
    if (m === "http") { body.url = val("sv-url"); const ex = val("sv-expect"); if (ex) body.expect_status = ex.split(",").map(x => parseInt(x.trim())).filter(Number.isFinite); }
    if (m === "tcp") { body.host = val("sv-host"); body.port = parseInt(val("sv-port")) || 0; }
    if (m === "ping") { body.host = val("sv-host"); }
    const r = await api("/api/services/config", { method: "POST", body });
    if (r.ok) {
      close();
      // `renderRoute()` e basta ridisegnava dallo snapshot, che si ricalcola
      // solo nel giro lento: il servizio appena aggiunto non compariva per un
      // minuto e nulla diceva che il salvataggio era andato a buon fine.
      toast(`"${body.name || body.unit}" e' ora monitorato: compare fra pochi secondi.`,
            { level: "ok" });
      await chiediRicalcoloServizi();
      return;
    }
    const msg = $("#sv-msg", ov);
    msg.textContent = r.error.messaggio;
    msg.style.display = "";
  };
}

/* ── WireGuard ────────────────────────────────────────────────────── */
/* ── Monitoraggio · Risorse degli host ────────────────────────────── */
/* Le risorse si raccolgono con un intervallo proprio (60s), mentre gli update
   WebSocket arrivano ogni 10s: si ridisegna solo quando il timestamp cambia,
   altrimenti la pagina lampeggerebbe per riscrivere gli stessi numeri. */
let resState = { ts: -1 };

/* Periodo scelto nella pagina Risorse. Uno solo per la pagina: i grafici degli
   host si confrontano a colpo d'occhio e finestre diverse lo impedirebbero.
   Le schede sono le stesse di Stats (PERIODI): "live" e' la serie che viaggia
   nello snapshot — `host_metrics.history_points` punti, di serie 60 a 60s,
   cioe' circa un'ora — mentre 1h/24h/7d arrivano dallo storico su disco. */
let resPeriod = "live";
/* Storico per host gia' scaricato: {ip: [punti]}, valido per `period`.
   `chiesto` dice quando: senza scadenza i grafici lunghi restavano fermi
   all'istante in cui si era aperta la pagina. */
const resHist = { period: null, per_host: {}, stato: "", chiesto: 0 };

async function loadResourcesHistory(r) {
  if (resPeriod === "live") return;
  const chiesto = resPeriod;
  const host = (r.hosts || []).map(x => x.host).filter(Boolean);
  if (!host.length) return;
  // Gia' in cache per questo periodo, per questi host e presa da meno di un
  // minuto: gli update arrivano ogni 10s e riscaricare ogni volta sarebbe una
  // richiesta per host ad ogni ciclo, per dati che cambiano al massimo al minuto.
  const fresca = resHist.period === chiesto
    && host.every(ip => resHist.per_host[ip])
    && Date.now() - resHist.chiesto < 60000;
  if (fresca) return;
  const primoCaricamento = resHist.period !== chiesto;
  resHist.chiesto = Date.now();
  if (primoCaricamento) {
    resHist.stato = "caricamento…";
    aggiornaNotaRisorse();
  }
  const esiti = await Promise.all(host.map(async ip => {
    const q = `/api/history/resources?host=${encodeURIComponent(ip)}` +
              `&period=${encodeURIComponent(chiesto)}`;
    // auth:false: N host in parallelo con la sessione scaduta aprirebbero il
    // pannello mentre si guarda un grafico. Lo stato lo dice la nota in pagina.
    const risp = await api(q, { auth: false });
    if (!risp.ok) return { ip, errore: risp.error };
    return { ip, punti: risp.data.points || [] };
  }));
  if (chiesto !== resPeriod) return;        // periodo gia' cambiato: esito scaduto
  const rotti = esiti.filter(e => e.errore);
  // Si sovrascrive solo chi ha risposto: prima la mappa veniva azzerata, quindi
  // un host che falliva un rinfresco perdeva anche i punti che gia' si avevano
  // e il suo grafico si svuotava sotto gli occhi.
  if (primoCaricamento) resHist.per_host = {};
  esiti.filter(e => !e.errore).forEach(e => { resHist.per_host[e.ip] = e.punti; });
  if (!primoCaricamento || rotti.length < esiti.length) resHist.period = chiesto;
  else resHist.period = null;
  resHist.stato = !rotti.length ? ""
    : rotti.some(e => e.errore.kind === "sessione") ? t("servizi.sessioneScaduta")
    : `${rotti[0].errore.messaggio} (${rotti.length} host su ${esiti.length})`;
  aggiornaNotaRisorse();
  drawResourceCharts(state.snap.resources || {});
}

function aggiornaNotaRisorse() {
  const n = $("#res-nota");
  if (n) n.textContent = resHist.stato;
  // Quanto copre davvero la finestra mostrata, presa dal primo host che ha
  // dei punti: sono tutti sulla stessa cadenza.
  const a = $("#res-arco");
  if (!a) return;
  const primo = ((state.snap.resources || {}).hosts || [])
    .map(resourceSeries).find(ser => (ser || []).length > 1);
  const arco = arcoSerie(primo || []);
  a.textContent = arco ? `copre ${arco}` : "";
}

/* Serie da disegnare per un host: viva sull'ora corrente, storica sui periodi
   lunghi. Finche' lo storico non e' arrivato si torna una serie vuota, che il
   grafico rende come "in attesa di dati" invece che come uno zero. */
function resourceSeries(x) {
  if (resPeriod === "live") return x.series || [];
  return (resHist.period === resPeriod && resHist.per_host[x.host]) || [];
}

function refreshResources(view, s) {
  const box = $("#res-hosts");
  if (!box) return false;
  // Fuori dal confronto sul ts: la sorgente puo' cadere senza che arrivi un
  // nuovo timestamp, ed e' proprio quello il caso da segnalare.
  aggiornaNoteSorgente(["resources"]);
  // Idem per la nota di testa: entrando nella pagina prima della prima lettura
  // restava "Prima raccolta in corso…" anche dopo, con le schede piene di dati
  // sotto, perche' il refresh parziale riscriveva solo l'elenco degli host.
  const testa = $("#res-testa");
  if (testa) testa.innerHTML = resourcesNote(s.resources || {});
  const ts = (s.resources || {}).ts || 0;
  if (ts === resState.ts) return true;
  resState.ts = ts;
  box.innerHTML = resourceCards(s.resources || {});
  drawResourceCharts(s.resources || {});
  aggiornaNotaRisorse();
  // Aprendo la pagina prima della prima raccolta non c'era ancora nessun host
  // da chiedere allo storico: senza questo i grafici lunghi resterebbero vuoti
  // finche' non si ritocca il selettore.
  loadResourcesHistory(s.resources || {});
  return true;
}

function pageResources(view, s) {
  const r = s.resources || {};
  resState.ts = r.ts || 0;
  view.innerHTML = `
    ${monitorTabs("resources")}
    ${alertBox("resources")}
    ${notaSorgente("resources")}
    <div id="res-testa">${resourcesNote(r)}</div>
    <div class="subtabs-bar">
      <div class="seg" id="res-period">
        ${PERIODI.map(([v, lbl]) =>
          `<button data-v="${v}"${resPeriod === v ? ' class="active"' : ""}>${lbl}</button>`).join("")}
      </div>
      <span class="muted" id="res-arco"></span>
      <span class="muted" id="res-nota" style="margin-left:auto"></span>
    </div>
    <div id="res-hosts">${resourceCards(r)}</div>`;
  bindMonitorTabs(view);
  $("#res-period").querySelectorAll("button").forEach(b => b.onclick = () => {
    if (resPeriod === b.dataset.v) return;
    $("#res-period").querySelectorAll("button")
      .forEach(x => x.classList.toggle("active", x === b));
    resPeriod = b.dataset.v;
    resHist.stato = "";
    drawResourceCharts(state.snap.resources || {});
    aggiornaNotaRisorse();
    loadResourcesHistory(state.snap.resources || {});
  });
  aggiornaNotaRisorse();
  drawResourceCharts(r);
  loadResourcesHistory(r);
}

/* Nota di testa: dice da dove arrivano i dati e quanti host rispondono.
   Se la raccolta e' spenta o non ha host, lo si dichiara invece di mostrare
   una pagina vuota che sembra un guasto. */
function resourcesNote(r) {
  if (r.enabled === false)
    return `<div class="cfg-note">Raccolta delle risorse disattivata
      (<code>host_metrics.enabled: false</code> nelle Impostazioni).</div>`;
  if (r.warning)
    return `<div class="cfg-note err">${h(r.warning)}</div>`;
  if (!r.ts) return `<div class="cfg-note">${h(t("risorse.primaRaccolta"))}</div>`;
  // "almeno": la raccolta si aggancia al ciclo lento, che con scan e Docker in
  // corso puo' arrivare piu' tardi. Ogni scheda dichiara la finestra davvero
  // usata, quindi qui non si promette una cadenza che non e' garantita.
  return `<div class="muted" style="margin-bottom:12px;font-size:12px">
    ${r.reachable ?? 0} host su ${r.total ?? 0} raggiungibili via SSH · lettura almeno ogni
    ${h(r.interval || "?")}s · le percentuali di CPU sono medie sulla finestra indicata
    in ogni scheda, non campioni istantanei.</div>`;
}

/* Gli host accesi vanno in cima: sono gli unici di cui c'e' qualcosa da
   guardare, e con qualche macchina spenta in mezzo le schede vive finivano
   sparse per la pagina. `slice()` perche' l'elenco arriva dallo snapshot, che
   e' condiviso con alert e storico e non va riordinato sotto i piedi a nessuno;
   `sort` e' stabile, quindi a parita' di stato l'ordine resta quello della
   configurazione invece di rimescolarsi ad ogni ciclo. */
function hostsOrdinati(r) {
  return (r.hosts || []).slice()
    .sort((a, b) => (b.reachable ? 1 : 0) - (a.reachable ? 1 : 0));
}

function resourceCards(r) {
  const hosts = hostsOrdinati(r);
  if (!hosts.length) return `<div class="empty">${h(t("risorse.nessunHost"))}</div>`;
  return `<div class="grid cols-2">${hosts.map(hostCard).join("")}</div>`;
}

/* Soglie di colore condivise da tutte le barre: sopra il 90% e' un problema,
   sopra il 70% merita un'occhiata. */
function barClass(pct) { return pct >= 90 ? "red" : pct >= 70 ? "orange" : "green"; }

function meter(pct, label, detail = "") {
  const val = pct == null ? "—" : `${pct}%`;
  return `<div style="margin-bottom:9px">
    <div style="display:flex;gap:8px;font-size:12px">
      <span class="muted">${h(label)}</span>
      <span class="mono" style="margin-left:auto">${h(val)}</span>
      ${detail ? `<span class="muted mono">${h(detail)}</span>` : ""}</div>
    <div class="bar" style="margin-top:3px"><i class="${pct == null ? "" : barClass(pct)}"
      style="width:${pct == null ? 0 : Math.min(100, pct)}%"></i></div></div>`;
}

function hostCard(x, idx) {
  const cpu = x.cpu || {}, mem = x.memory || {}, swap = x.swap || {};
  const temps = x.temperatures || [];
  const tmax = temps.length ? Math.max(...temps.map(t => t.celsius)) : null;
  // Il sistema si dichiara solo quando non e' quello di casa: su una LAN di
  // macchine Linux una targhetta su ognuna sarebbe rumore, su quella diversa e'
  // l'informazione che spiega perche' manca il load average.
  const sistema = x.os === "windows" ? `<span class="tag">Windows</span> ` : "";
  const right = x.reachable
    ? `${sistema}<span class="muted mono">${h(x.host)} · up ${h(x.uptime_human || "—")}</span>`
    : `${sistema}<span class="tag err" style="border-color:var(--bd-red)">${h(t("comune.nonRaggiungibile"))}</span>`;

  if (!x.reachable) {
    const last = lastKnown(x.series);
    return card(x.name, `
      <div class="cfg-note err">${h(x.error || t("risorse.hostNonRaggiungibile"))}</div>
      ${last ? `<div class="muted" style="margin-top:10px;font-size:12px">Ultimo stato noto
          (${h(ago(Math.round(last.t / 1000)))}): CPU ${h(fmtPct(last.cpu))} ·
          RAM ${h(fmtPct(last.mem))}${last.temp != null ? " · " + h(last.temp) + " °C" : ""}.</div>`
        : `<div class="muted" style="margin-top:10px;font-size:12px">Nessuna lettura riuscita
          finora.</div>`}`, right);
  }

  const cores = (cpu.per_core || []);
  return card(x.name, `
    <div class="detail-grid" style="margin-bottom:12px">
      <div>
        ${meter(cpu.percent, "CPU", cpu.window_seconds ? `media ${cpu.window_seconds}s` : "prima lettura")}
        ${meter(mem.used_pct, "RAM", `${fmtBytes(mem.used)} / ${fmtBytes(mem.total)}`)}
        ${meter(swap.used_pct, t("risorse.swap"), swap.total ? `${fmtBytes(swap.used)} / ${fmtBytes(swap.total)}` : "assente")}
      </div>
      <div>
        <div class="kv"><span class="muted">${h(t("host.load"))}</span><span class="mono">${
          (x.load || []).map(v => v.toFixed(2)).join("  ") || "—"}</span></div>
        <div class="kv"><span class="muted">CPU</span><span class="mono" style="text-align:right">${
          h(cpu.model || "—")}<br>${h(cpu.cores || "?")} core</span></div>
        <div class="kv"><span class="muted">${h(t("host.temperatura"))}</span><span class="mono">${
          tmax == null ? "—" : h(tmax) + " °C"}</span></div>
        <div class="kv"><span class="muted">${h(t("host.uptime"))}</span><span class="mono">${h(x.uptime_human || "—")}</span></div>
      </div>
    </div>
    ${cores.length ? `<div class="cores">${cores.map((c, i) =>
      `<div class="core" title="core ${i}: ${c == null ? "—" : c + "%"}">
        <span class="mono faint">c${i}</span>
        <div class="bar"><i class="${c == null ? "" : barClass(c)}"
          style="width:${c == null ? 0 : Math.min(100, c)}%"></i></div>
        <span class="mono muted">${c == null ? "—" : c}</span></div>`).join("")}</div>` : ""}
    <canvas class="chart" data-res-chart="${idx}" data-chart-key="res:${h(x.host || idx)}"></canvas>
    <div style="margin:6px 0 14px">
      <span class="legend-line"><i style="background:var(--teal)"></i>CPU</span>
      <span class="legend-line"><i style="background:var(--purple)"></i>RAM</span>
      <span class="legend-line"><i style="background:var(--orange)"></i>temperatura</span></div>
    ${diskTable(x)}
    ${tempTable(temps)}
    ${procTable(x.processes || [])}`, right);
}

/* Ultimo punto della storia con almeno un valore: serve a dire cosa si sapeva
   dell'host prima che smettesse di rispondere. */
function lastKnown(series) {
  for (let i = (series || []).length - 1; i >= 0; i--)
    if (series[i].cpu != null || series[i].mem != null) return series[i];
  return null;
}
function fmtPct(v) { return v == null ? "—" : v + "%"; }
function fmtRateBytes(bps) { return bps == null ? "—" : fmtBytes(bps) + "/s"; }

function diskTable(x) {
  const disks = x.disks || [];
  if (!disks.length) return "";
  const io = {};
  (x.disk_io || []).forEach(d => io[d.device] = d);
  // L'I/O e' per dispositivo fisico, lo spazio per filesystem: sono due elenchi
  // diversi e accostarli per nome darebbe accoppiamenti sbagliati (LVM, RAID).
  const ioRows = (x.disk_io || []).filter(d => d.read_bps != null || d.write_bps != null);
  return `<table><thead><tr><th>${h(t("risorse.disco"))}</th><th>Uso</th><th>${h(t("risorse.libero"))}</th></tr></thead><tbody>
    ${disks.map(d => `<tr>
      <td class="mono">${h(d.mount)}<div class="muted" style="font-size:11px">${h(d.device)}
        ${d.fstype ? "· " + h(d.fstype) : ""}</div></td>
      <td style="min-width:120px">${meter(d.used_pct, "", fmtBytes(d.used) + " / " + fmtBytes(d.total))}</td>
      <td class="mono">${fmtBytes(d.free)}</td></tr>`).join("")}
    ${ioRows.map(d => `<tr><td class="mono muted">${h(d.device)} <span class="tag">I/O</span></td>
      <td class="mono muted" colspan="2">lettura ${h(fmtRateBytes(d.read_bps))} ·
        scrittura ${h(fmtRateBytes(d.write_bps))}</td></tr>`).join("")}
  </tbody></table>`;
}

function tempTable(temps) {
  if (!temps.length) return "";
  return `<div style="margin-top:10px">${temps.map(t =>
    `<span class="tag">${h(t.label)} ${h(t.celsius)} °C</span>`).join("")}</div>`;
}

function procTable(procs) {
  if (!procs.length) return "";
  return `<table style="margin-top:10px"><thead><tr>
      <th>${h(t("risorse.processo"))}</th><th>${h(t("risorse.utente"))}</th><th class="right">CPU</th><th class="right">RAM</th></tr></thead>
    <tbody>${procs.map(p => `<tr>
      <td class="mono">${h(p.command)} <span class="muted">${h(p.pid)}</span></td>
      <td class="mono muted">${h(p.user)}</td>
      <td class="mono right">${h(p.cpu_percent)}%</td>
      <td class="mono right">${fmtBytes(p.rss)}</td></tr>`).join("")}</tbody></table>`;
}

/* Un grafico per host: CPU e RAM in percentuale a sinistra, temperatura in gradi
   a destra. Entrambi gli assi hanno fondo scala fisso a 100 — le schede di host
   diversi si confrontano a occhio, e 100 °C e' la soglia di throttling delle
   CPU, quindi la linea sta dove significa qualcosa invece di appoggiarsi al
   bordo perche' il massimo della serie e' 49. */
function drawResourceCharts(r) {
  // Lo stesso ordine delle schede, non quello dello snapshot: i canvas si
  // agganciano per indice, e le due liste devono essere la stessa lista.
  hostsOrdinati(r).forEach((x, i) => {
    const canvas = document.querySelector(`canvas[data-res-chart="${i}"]`);
    if (!canvas) return;
    drawLineChart(canvas, resourceSeries(x), [
      { get: p => p.cpu, color: "teal", label: "CPU" },
      { get: p => p.mem, color: "purple", label: "RAM" },
      { get: p => p.temp, color: "orange", axis: "right", label: "temp" },
    ], { left: "pct", leftTop: 100, right: "temp", rightTop: 100 });
  });
}

function pageWireGuard(view, s) {
  view.innerHTML = `
    ${alertBox("wireguard")}
    ${notaSorgente("wireguard")}
    <div id="wg-corpo">${wgCorpo(s)}</div>`;
}

/* Aggiornamento parziale: la pagina non ha controlli da preservare, ma
   l'avviso di sorgente sta fuori dal corpo e va aggiornato lo stesso —
   altrimenti resta quello di quando si e' entrati. */
function refreshWireGuard(view, s) {
  const box = $("#wg-corpo");
  if (!box) return false;
  aggiornaNoteSorgente(["wireguard"]);
  box.innerHTML = wgCorpo(s);
  return true;
}

/* Nome del peer. Quando nel catalogo (`wireguard.peer_names`) non c'e' un nome,
   il backend ripiega sulla chiave pubblica tagliata: una riga di base64 non
   dice niente a chi guarda, ed e' proprio il caso del peer principale qui.
   Si preferisce l'indirizzo che il peer occupa dentro la VPN, che almeno e'
   un indirizzo vero; la chiave resta leggibile sotto. */
function nomePeer(p) {
  const ripiego = ((p.public_key || "").slice(0, 16)) + "…";
  if (p.name && p.name !== ripiego) return p.name;
  const host = (p.endpoint || "").split(":")[0];
  // Il relay e' dichiarato in configurazione: se il peer punta li', dirlo e'
  // piu' utile che ripetere la rotta o l'indirizzo.
  const relay = ((state.snap.wireguard || {}).relay || "");
  if (host && host === relay) return `relay ${host}`;
  const ip = (p.allowed_ips || [])[0] || "";
  if (/\/32$/.test(ip)) return ip.replace(/\/32$/, "");
  return host || ip || ripiego;
}

function peerSenzaNome(p) {
  return !p.name || p.name === ((p.public_key || "").slice(0, 16)) + "…";
}

/* Stato del peer in italiano, e con la distinzione che serve: "mai collegato"
   e "fermo da sette ore" erano tutti e due `idle`, stessa parola e stesso
   colore per due situazioni diverse. */
function statoPeer(p) {
  if (p.status === "active") return { cls: "ok", testo: "attivo" };
  // Grigio, non rosso: un peer che ora non e' collegato non e' un guasto — si
  // collega quando serve (e' il motivo per cui l'alert sui peer mai connessi
  // nasce spento, vedi services/alerts.py). Il colore direbbe il falso; il
  // testo dice il fatto.
  if (!p.last_handshake) return { cls: "ignoto", testo: t("wg.maiCollegato") };
  return { cls: "ignoto", testo: `fermo da ${durata(p.last_handshake)}` };
}

function wgCorpo(s) {
  const wg = s.wireguard || {};
  const ifaces = wg.interfaces || [];
  const peers = ifaces.flatMap(i => i.peers || []);
  const attivi = wg.active_peers ?? peers.filter(p => p.status === "active").length;
  const totali = wg.total_peers ?? peers.length;
  const sorgente = (state.snap.sources || {}).wireguard;
  // Finche' il primo giro non e' arrivato non si dichiara nulla: uno "0/0"
  // spento sembra una VPN senza peer invece di un dato che non c'e' ancora.
  const attesa = !sorgente;
  const giu = totali - attivi;
  return `
    <div class="grid cols-3" style="margin-bottom:14px">
      ${kpi(attesa ? "–" : `${attivi}<span class="unit">/${totali}</span>`, "peer attivi", "", {
        // Giallo solo quando non e' collegato NESSUNO: con qualche peer fermo
        // e il tunnel su, un pallino d'allarme accuserebbe un guasto che non
        // c'e' (i peer si collegano quando servono).
        dot: attesa || !totali ? "s-off" : attivi ? "s-on" : "s-warn",
        ctx: attesa ? t("wg.attesaPrimoGiro")
           : !totali ? t("wg.nessunPeerConfigurato")
           : giu ? `${conta(giu, "conta.peerFermo")}` : t("stato.tuttiCollegati") })}
      ${kpi(attesa ? "–" : ifaces.length, "interfacce", "", {
        ctx: ifaces.map(i => i.name).join(", ") || "nessuna" })}
      ${kpi(h(wg.relay || "—"), "relay", "", {
        ctx: wg.relay ? "endpoint dichiarato in configurazione" : t("wg.nonConfigurato") })}
    </div>
    <div class="muted" style="margin-bottom:12px;font-size:12px">
      WireGuard gira sul <b>router</b>${(state.snap.meta || {}).router_name
        ? ` (${h(routerName())})` : ""}, non su questo host: lo stato
      arriva da li' via SSH ${etaSorgente("wireguard")}. RX e TX sono i totali scambiati
      da quando l'interfaccia e' stata caricata, non la velocita' di adesso.
    </div>
    ${ifaces.map(wgCard).join("") || wgVuoto(sorgente)}
    ${peers.length && !attivi && !attesa ? `<div class="cfg-note">Nessun peer collegato in
      questo momento. Se il tunnel dovrebbe essere sempre su, e' il caso di guardare il
      router.</div>` : ""}
    ${peers.some(peerSenzaNome) ? `<div class="cfg-note">Qualche peer si presenta con la
      sua chiave pubblica: i nomi si danno in <b>${h(t("comune.impostazioni"))}</b>, voce
      <code>wireguard.peer_names</code> (chiave pubblica → nome).</div>` : ""}`;
}

/* Perche' non c'e' niente da mostrare: "nessuna interfaccia" era la stessa
   frase sia con il router muto sia con il router che risponde e non ha
   WireGuard acceso. */
function wgVuoto(sorgente) {
  if (!sorgente) return `<div class="empty">${h(t("wg.attesaRaccolta"))}</div>`;
  if (!sorgente.ok) return `<div class="empty">Nessun dato: il router non risponde
    (il dettaglio e' nell'avviso qui sopra).</div>`;
  return `<div class="empty">${h(t("wg.nessunaInterfaccia"))}</div>`;
}

function wgCard(i) {
  const peers = i.peers || [];
  return card(`Interfaccia ${h(i.name)}`, `
    <div class="muted mono" style="margin-bottom:10px">listen :${h(i.listen_port)} ·
      ${i.active_peers}/${i.total_peers} attivi</div>
    ${!peers.length ? `<div class="empty">${h(t("wg.nessunPeerInterfaccia"))}</div>` : `
    <table><thead><tr><th>${h(t("wg.peer"))}</th><th>${h(t("wg.endpoint"))}</th><th>${h(t("wg.allowedIps"))}</th>
      <th>${h(t("wg.ultimoHandshake"))}</th><th>RX / TX totali</th><th></th></tr></thead>
    <tbody>${peers.map(p => {
      const st = statoPeer(p);
      return `<tr>
        <td class="mono">${h(nomePeer(p))}
          ${peerSenzaNome(p) ? `<div class="muted mono" style="font-size:11px"
            title="${h(p.public_key || "")}">chiave ${h((p.public_key || "").slice(0, 12))}…</div>` : ""}</td>
        <td class="mono muted">${h(p.endpoint || "—")}</td>
        <td class="mono muted">${(p.allowed_ips || []).map(h).join(", ")}</td>
        <td class="mono">${h(p.last_handshake ? ago(p.last_handshake) : "mai")}</td>
        <td class="mono muted">${fmtMB(p.rx_mb)} / ${fmtMB(p.tx_mb)}</td>
        <td><span class="chip ${st.cls}">${h(st.testo)}</span></td>
      </tr>`;
    }).join("")}</tbody></table>`}`);
}

/* ── Stats ────────────────────────────────────────────────────────── */
/* Celle RX/TX di una interfaccia: i byte appartengono al dispositivo, quindi si
   scrivono su una riga sola. Le altre reti sullo stesso dispositivo rimandano a
   quella, invece di ripetere lo stesso numero e sembrare misure indipendenti. */
function trafficCells(i) {
  if (i.counters_own === false)
    return `<td class="mono muted" colspan="2">contati su ${h(i.ifname)}</td>`;
  return `<td class="mono">${fmtMB(i.rx_mb)}</td><td class="mono">${fmtMB(i.tx_mb)}</td>`;
}

/* Periodo scelto nella pagina Stats. In una variabile di modulo come devFilter:
   deve sopravvivere ai re-render provocati dagli update WebSocket. */
let statsPeriod = "live";
/* Cache dello storico scaricato: `period` dice a quale periodo si riferisce e
   `chiesto` quando e' stato preso, cosi' un update live non la fa riscaricare
   ma dopo un minuto si', altrimenti resterebbe ferma per sempre. */
const statsHist = { period: null, points: [], stato: "", chiesto: 0 };

/* "live" e' la finestra viva in memoria: 120 punti all'intervallo rapido, cioe'
   una VENTINA DI MINUTI, non un'ora. Prima questa stessa serie era etichettata
   "1 ora" e la pagina diceva il falso; l'ora vera adesso arriva dallo storico,
   che ha i punti grezzi delle ultime 24 ore. */
const PERIODI = [["live", "live"], ["1h", "1 ora"], ["24h", "24 ore"], ["7d", "7 giorni"]];

function statsSeries(s) {
  if (statsPeriod === "live") return s.traffic_series || [];
  return statsHist.period === statsPeriod ? statsHist.points : [];
}

/* Quanto arco copre davvero la serie mostrata, derivato dai dati. */
function arcoSerie(ser) {
  if ((ser || []).length < 2) return "";
  const min = Math.round(((ser[ser.length - 1].t || 0) - (ser[0].t || 0)) / 60000);
  if (min < 1) return t("tempo.menoDiUnMinuto");
  if (min < 90) return conta(min, "tempo.minuto");
  const ore = Math.round(min / 60);
  return ore < 48 ? conta(ore, "tempo.ora") : conta(Math.round(ore / 24), "tempo.giorno");
}

async function loadStatsHistory() {
  if (statsPeriod === "live") return;
  // Stesso periodo gia' in cache e preso da meno di un minuto: va bene cosi'.
  // Senza la scadenza il grafico restava fermo all'istante in cui si era
  // aperta la pagina, mentre la barra in alto continuava a dire "agg. 3s fa".
  if (statsHist.period === statsPeriod && Date.now() - statsHist.chiesto < 60000) return;
  const primoCaricamento = statsHist.period !== statsPeriod;
  if (primoCaricamento) {
    statsHist.stato = "caricamento…";
    drawStatsCharts(state.snap);
  }
  const chiesto = statsPeriod;
  statsHist.chiesto = Date.now();
  // auth:false: cambiare periodo su un grafico non deve aprire un pannello di
  // login sopra la pagina.
  const r = await api(`/api/history/traffic?period=${encodeURIComponent(chiesto)}`, { auth: false });
  if (chiesto !== statsPeriod) return;          // l'utente ha gia' cambiato idea
  if (!r.ok) {
    statsHist.stato = r.error.kind === "sessione"
      ? t("servizi.sessioneScaduta")
      : r.error.messaggio;
    // Il rinfresco periodico puo' fallire: in quel caso si tengono i punti che
    // gia' si avevano invece di svuotare il grafico sotto gli occhi.
    if (primoCaricamento) { statsHist.period = null; statsHist.points = []; }
  } else {
    statsHist.period = chiesto;
    statsHist.points = r.data.points || [];
    statsHist.stato = statsHist.points.some(p => p.rx_bps != null)
      ? "" : t("stats.nessunArchivio");
  }
  drawStatsCharts(state.snap);
}

/* Riepilogo del periodo mostrato, calcolato sulla serie che si sta guardando:
   i numeri sotto il grafico devono parlare dello stesso grafico. */
function statsRiassunto(ser) {
  const soloValori = (campo) => (ser || []).map(p => p[campo]).filter(v => v != null);
  const rx = soloValori("rx_bps"), tx = soloValori("tx_bps"), lat = soloValori("latency");
  const media = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const probe = (state.snap.meta || {}).internet_probe;
  const scambiati = volumeStimato(ser, "rx_bps") + volumeStimato(ser, "tx_bps");
  return `
    ${kpi(rx.length ? h(fmtRate(Math.max(...rx))) : "—", "picco RX", "", { dot: "" })}
    ${kpi(tx.length ? h(fmtRate(Math.max(...tx))) : "—", "picco TX", "")}
    ${kpi(lat.length ? `${Math.round(media(lat))}<span class="unit"> ms</span>` : "—",
      "latenza media", "", { ctx: probe ? `verso ${probe}` : "" })}
    ${kpi(scambiati ? h(fmtBytes(scambiati)) : "—", t("stats.scambiatiNelPeriodo"), "",
      { ctx: "stimato dalle velocita'" })}`;
}

/* Volume dalle velocita': i punti sono bit/s, quindi si integra sull'intervallo
   fra un punto e il successivo. E' una stima — i contatori esatti li ha solo il
   router, e nello storico non ci sono — e la pagina lo dichiara. */
function volumeStimato(ser, campo) {
  let byte = 0;
  for (let i = 1; i < (ser || []).length; i++) {
    const v = ser[i][campo];
    const dt = ((ser[i].t || 0) - (ser[i - 1].t || 0)) / 1000;
    if (v == null || !(dt > 0)) continue;
    byte += v * dt / 8;
  }
  return byte;
}

function statsInterfacce(s) {
  const ifs = s.interfaces || [];
  if (!ifs.length) return `<div class="empty">In attesa dati router…</div>`;
  return `<table><thead><tr><th>${h(t("comune.interfaccia"))}</th><th>IP</th><th>${h(t("comune.stato"))}</th><th>RX totali</th><th>TX totali</th></tr></thead>
    <tbody>${ifs.map(i => `<tr>
      <td class="mono">${h(i.name)} <span class="muted">${h(i.ifname)}</span>
        ${(i.shared_with || []).length ? `<div class="muted" style="font-size:11px">
          stesso dispositivo di ${(i.shared_with || []).map(h).join(", ")}</div>` : ""}</td>
      <td class="mono">${(i.ip4 || []).map(h).join(", ") || "—"}</td>
      <td><span class="status-dot ${i.up ? "s-on" : "s-off"}"></span>${i.up ? "up" : "down"}</td>
      ${trafficCells(i)}
    </tr>`).join("")}</tbody></table>
    ${ifs.some(i => (i.shared_with || []).length) ? `<div class="muted"
      style="font-size:12px;padding:8px 12px">${h(t("stats.contatoriDel"))} <b>dispositivo</b> di rete, non
      della singola rete logica: dove piu' reti condividono lo stesso dispositivo il traffico non
      e' separabile e viene attribuito una volta sola. Per lo stesso motivo la somma delle reti non
      corrisponde alla WAN: il traffico fra subnet passa dal bridge senza mai uscire.</div>` : ""}`;
}

/* Ridisegna solo i canvas e le etichette: rifare l'intera pagina azzererebbe
   il selettore ad ogni update live. */
function drawStatsCharts(s) {
  const ser = statsSeries(s);
  const canvas = $("#st-traffic");
  if (!canvas) return false;
  const now = lastTrafficPoint(ser);
  const nota = $("#st-nota");
  if (nota) nota.textContent = statsHist.stato;
  const arco = $("#st-arco");
  if (arco) arco.textContent = arcoSerie(ser) ? `copre ${arcoSerie(ser)}` : "";
  const riass = $("#st-kpi");
  if (riass) riass.innerHTML = statsRiassunto(ser);
  const rx = $("#st-rx"), tx = $("#st-tx"), lat = $("#st-latnow");
  if (rx) rx.textContent = fmtRate(now.rx_bps);
  if (tx) tx.textContent = fmtRate(now.tx_bps);
  if (lat) lat.textContent = now.latency ? Math.round(now.latency) + " ms" : "—";
  const testaIf = $("#st-if-testa");
  if (testaIf) testaIf.innerHTML = `${badgeSorgente("interfaces")} ${etaSorgente("interfaces")}`;
  // La tabella e' fuori dai canvas ma dentro il refresh parziale: senza questa
  // riga restava quella del momento in cui si era entrati nella pagina, e
  // arrivandoci prima del primo snapshot restava vuota per sempre.
  const tabIf = $("#st-if");
  if (tabIf) tabIf.innerHTML = statsInterfacce(s);
  drawLineChart(canvas, ser, [
    { get: p => p.rx_bps, color: "teal", label: "RX" },
    { get: p => p.tx_bps, color: "green", label: "TX" }],
    { left: "rate" });
  drawLineChart($("#st-lat"), ser,
    [{ get: p => p.latency, color: "orange", label: "latenza" }], { left: "ms" });
  return true;
}

/* Sugli update live si ridisegna soltanto: i controlli restano in piedi e il
   periodo scelto non viene sovrascritto. `loadStatsHistory` ha la sua scadenza,
   quindi chiamarla qui non produce una richiesta per ciclo. */
function refreshStats(view, s) {
  if (!drawStatsCharts(s)) return false;
  aggiornaNoteSorgente(["traffic"]);
  loadStatsHistory();
  return true;
}

function pageStats(view, s) {
  view.innerHTML = `
    ${alertBox("stats")}
    ${notaSorgente("traffic")}
    <div class="subtabs-bar">
      <div class="seg" id="st-period">
        ${PERIODI.map(([v, lbl]) =>
          `<button data-v="${v}"${statsPeriod === v ? ' class="active"' : ""}>${lbl}</button>`).join("")}
      </div>
      <span class="muted" id="st-arco"></span>
      <span class="muted" id="st-nota" style="margin-left:auto"></span>
    </div>
    <div class="grid cols-4" style="margin-bottom:14px" id="st-kpi">${statsRiassunto(statsSeries(s))}</div>
    <div class="grid cols-2-1" style="margin-bottom:14px">
      ${card(t("stats.velocitaWan"), `<canvas class="chart tall" id="st-traffic"></canvas>
        <div class="rates"><span class="rate"><i style="background:var(--teal)"></i>↓ <span id="st-rx">—</span></span>
        <span class="rate"><i style="background:var(--green)"></i>↑ <span id="st-tx">—</span></span></div>`,
        `<span class="muted">${h(t("stats.bitMedia"))}</span>`)}
      ${card(t("stats.latenzaInternet"), `<canvas class="chart tall" id="st-lat"></canvas>
        <div class="muted" style="font-size:12px;margin-top:8px">Un buco nella linea
        significa che il bersaglio non ha risposto: non e' latenza zero.</div>`,
        `<span class="muted" id="st-latnow">—</span>`)}
    </div>
    ${card(t("stats.interfacceRouter"), `<div id="st-if">${statsInterfacce(s)}</div>`,
      `<span id="st-if-testa"></span>`)}`;
  $("#st-period").querySelectorAll("button").forEach(b => b.onclick = () => {
    if (statsPeriod === b.dataset.v) return;
    $("#st-period").querySelectorAll("button")
      .forEach(x => x.classList.toggle("active", x === b));
    statsPeriod = b.dataset.v;
    statsHist.stato = "";
    drawStatsCharts(state.snap);
    loadStatsHistory();
  });
  drawStatsCharts(s);
  loadStatsHistory();
}

/* ===================================================================
   PAGINE SENSIBILI (tools, terminale)
   Il backend pretende una sessione vera anche quando l'auth globale e'
   spenta: dare una shell SSH o l'esecuzione di comandi a chiunque sia in
   LAN non e' come mostrargli lo stato dei dispositivi. Qui si mostra un
   login *dentro* la pagina, senza buttare fuori dal resto della dashboard.
   =================================================================== */

async function sessionGate(view, onReady) {
  const esito = await api("/api/auth/status", { auth: false, retry: false });
  const stato = statoGate(esito);
  if (stato === "ok") return onReady();

  // Con l'API giu' si diceva "Password admin mancante": si accusava il
  // proprietario di non aver fatto una cosa che invece aveva fatto.
  if (stato === "offline") {
    view.innerHTML = card(t("sicurezza.accessoNonVerificabile"), noteErrore(esito.error, "gate-retry"));
    const b = $("#gate-retry");
    if (b) b.onclick = () => sessionGate(view, onReady);
    return;
  }
  if (stato === "senza-password") {
    view.innerHTML = card(t("sicurezza.passwordMancante"),
      `<div class="empty">${h(t("sicurezza.impostaPrima"))}
         <a class="link" href="#/settings">${h(t("comune.impostazioni"))}</a>${h(t("sicurezza.senzaToolsChiusi"))}</div>`);
    return;
  }
  view.innerHTML = card(t("sicurezza.accessoRichiesto"),
    `<div id="gate-form"></div>
     <div class="muted" style="margin-top:10px">${h(t("sicurezza.pagineEsegueComandi"))}</div>`);
  // Stesso form del pannello della sessione scaduta: un solo punto che fa
  // login in tutta la SPA, in due contenitori diversi.
  if (await formLogin($("#gate-form"))) renderRoute();
}

/* ── Tools di rete ────────────────────────────────────────────────── */
let toolsState = { catalog: null, tool: "ping", target: "", options: {}, out: "", running: false };

function toolsSetTarget(target) { toolsState.target = target; go("tools"); }

function pageTools(view) {
  // Ritorna la promessa del gate: `go()` non l'aspetta, ma chi la chiama a
  // mano (i test, il "riprova") deve poter sapere quando la pagina e' pronta.
  return sessionGate(view, async () => {
    // Il catalogo si rilegge ad ogni ingresso: dopo un aggiornamento del
    // backend restava quello di prima, e gli strumenti nuovi comparivano solo
    // ricaricando la pagina a mano. Se la richiesta fallisce ma un elenco ce
    // l'abbiamo gia', si continua a lavorare con quello.
    const r = await api("/api/tools/");
    if (r.ok) toolsState.catalog = r.data.tools;
    else if (!toolsState.catalog) {
      view.innerHTML = card(t("nav.tools"), noteErrore(r.error, "tl-retry"));
      const b = $("#tl-retry");
      if (b) b.onclick = () => pageTools(view);
      return;
    }
    renderTools(view);
  });
}

function renderTools(view) {
  const cat = toolsState.catalog;
  const cur = cat.find(t => t.id === toolsState.tool) || cat[0];
  view.innerHTML = `
    ${card(t("tools.titolo"), `
      <div class="controls" style="margin-bottom:0">
        <select id="tl-tool">${cat.map(t =>
          `<option value="${h(t.id)}"${t.id === cur.id ? " selected" : ""}>${h(t.label)}</option>`).join("")}</select>
        ${cur.no_target ? "" : `<input type="text" id="tl-target"
          placeholder="${h(segnaposto(cur))}" value="${h(toolsState.target)}"
          style="min-width:220px">`}
        ${optionInputs(cur)}
        <button class="btn" id="tl-run"${toolsState.running ? " disabled" : ""}
          style="border-color:var(--teal);color:var(--teal)">${h(toolsState.running ? t("tools.inCorso") : t("comune.esegui"))}</button>
        <span class="muted">${h(cur.help)}</span>
      </div>`)}
    ${card(t("comune.output"), `<div class="logbox" id="tl-out">
      ${toolsState.running
        ? `<div class="l muted">esecuzione in corso… <span id="tl-crono"></span></div>` : ""}
      <div class="l${toolsState.out ? "" : " muted"}" id="tl-testo">${h(toolsState.out
        || (toolsState.running ? "" : t("tools.scegliStrumento")))}</div>
      </div>`)}`;

  $("#tl-tool").onchange = (e) => { toolsState.tool = e.target.value; toolsState.options = {}; renderTools(view); };
  $("#tl-run").onclick = () => runTool(view, cur);
  // La misura di velocita' non ha un bersaglio: l'indirizzo sta in
  // configurazione, e un campo vuoto da riempire sarebbe una domanda senza
  // risposta giusta.
  const campo = $("#tl-target");
  if (campo) {
    campo.oninput = (e) => { toolsState.target = e.target.value; };
    t.onkeydown = (e) => { if (e.key === "Enter") runTool(view, cur); };
  }
}

/* Cosa ci si aspetta nel campo bersaglio: un URL per HTTP, un IP della LAN per
   l'ARP ping, un dominio per il whois. Un segnaposto solo per tutti diceva
   "host, IP o subnet" anche dove non era vero. */
function segnaposto(tool) {
  if (tool.id === "http") return "https://esempio.it/pagina";
  if (tool.id === "whois") return "dominio o IP pubblico…";
  if (tool.id === "arping") return "IP sulla stessa rete…";
  return "host, IP o subnet…";
}

function optionInputs(tool) {
  return Object.entries(tool.options || {}).map(([key, o]) => {
    const val = toolsState.options[key] ?? o.default;
    if (o.type === "bool")
      return `<label class="muted" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" class="tl-opt" data-k="${h(key)}" data-t="bool"${val ? " checked" : ""}>
        ${h(o.label || key)}</label>`;
    if (o.type === "choice")
      return `<select class="tl-opt" data-k="${h(key)}" data-t="str">${o.values.map(v =>
        `<option${v === val ? " selected" : ""}>${h(v)}</option>`).join("")}</select>`;
    if (o.type === "text")
      return `<input type="text" class="tl-opt" data-k="${h(key)}" data-t="str"
        value="${h(val ?? "")}" placeholder="${h(o.label || key)}" title="${h(o.label || key)}"
        style="width:150px">`;
    return `<input type="number" class="tl-opt" data-k="${h(key)}" data-t="int" value="${h(val)}"
      min="${h(o.min ?? 1)}" max="${h(o.max ?? 999)}" style="width:80px" title="${h(key)}">`;
  }).join("");
}

/* Cronometro dell'esecuzione. Un nmap puo' durare tre minuti: "in corso…"
   senza un tempo che scorre e senza un orizzonte sembra un blocco, e si
   ricarica la pagina proprio mentre il lavoro sta girando. */
let toolsCrono = 0;

function avviaCronometro(tool) {
  const inizio = Date.now();
  fermaCronometro();
  const scrivi = () => {
    const el = $("#tl-crono");
    if (!el) return fermaCronometro();      // pagina cambiata: niente da aggiornare
    const s = Math.round((Date.now() - inizio) / 1000);
    el.textContent = `${s}s${tool.max_seconds ? ` · al massimo ${tool.max_seconds}s` : ""}`;
  };
  scrivi();
  toolsCrono = setInterval(scrivi, 1000);
}

function fermaCronometro() {
  if (toolsCrono) { clearInterval(toolsCrono); toolsCrono = 0; }
}

/* Com'e' andata, in una parola. `exit_code` da solo e' un numero che vuol dire
   qualcosa solo a chi conosce il tool: un arping senza risposte usciva "1" e
   sembrava un errore del programma invece che un IP spento. */
function esitoTool(d) {
  if (d.timed_out) return t("tools.interrottoTempo");
  if (d.exit_code === 0) return "riuscito";
  // Senza codice d'uscita il comando non e' arrivato in fondo: "uscita ?" non
  // lo diceva, e sembrava un errore del programma.
  if (d.exit_code === null || d.exit_code === undefined)
    return d.truncated ? "interrotto: output troppo lungo" : t("tools.interrottoPrima");
  return `nessun risultato (uscita ${d.exit_code})`;
}

/* Corpo di /api/tools/stream: una riga JSON per evento. Se il browser non
   espone un flusso leggibile, o il corpo e' gia' arrivato tutto, si legge in
   una volta sola — stesso risultato, non si vede solo scorrere. */
async function* righeEventi(r) {
  if (!r.body || typeof r.body.getReader !== "function") {
    for (const riga of (await r.text()).split("\n")) if (riga.trim()) yield riga;
    return;
  }
  const lettore = r.body.getReader();
  const dec = new TextDecoder();
  let resto = "";
  for (;;) {
    const { done, value } = await lettore.read();
    if (done) break;
    resto += dec.decode(value, { stream: true });
    const righe = resto.split("\n");
    resto = righe.pop();               // l'ultima puo' essere a meta'
    for (const riga of righe) if (riga.trim()) yield riga;
  }
  if (resto.trim()) yield resto;
}

/* Scrive l'output mentre arriva, senza rifare la pagina: un render intero ad
   ogni pezzo azzererebbe il cronometro e la posizione dello scorrimento. */
function scriviParziale(comando, testo) {
  const el = $("#tl-testo");
  if (!el) return;
  el.className = "l";
  el.textContent = `$ ${comando}\n\n${testo}`;
  const box = $("#tl-out");
  if (box && typeof box.scrollHeight === "number") box.scrollTop = box.scrollHeight;
}

/* Esecuzione in diretta: l'output esce mentre il comando gira. Prima un
   traceroute lungo o un nmap da tre minuti mostravano una schermata vuota
   fino all'ultimo secondo, e sembrava che non stesse succedendo niente.

   Ritorna la stessa forma di api(): { ok, data } oppure { ok:false, error }. */
async function eseguiInDiretta(tool, corpo) {
  const ac = new AbortController();
  // Rete di sicurezza: il tetto vero lo applica il backend (e chiude il
  // flusso), questo serve solo se la connessione resta aperta senza dire piu'
  // nulla. Sta sopra al massimo dichiarato dal tool, non sotto.
  const guardia = setTimeout(() => ac.abort(), ((tool.max_seconds || 60) + 30) * 1000);
  const inizio = Date.now();
  let r;
  try {
    r = await fetch("/api/tools/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
      signal: ac.signal,
    });
  } catch (e) {
    clearTimeout(guardia);
    return apiErrore(e && e.name === "AbortError" ? "timeout" : "rete", 0, "");
  }
  if (!r.ok) {
    clearTimeout(guardia);
    // Sessione scaduta: la ripresa col login inline la sa fare solo api(), che
    // pero' non legge un flusso. Si rifa' la stessa esecuzione dalla rotta
    // one-shot: si perde lo scorrere dell'output, non il risultato.
    if (r.status === 401) return null;
    let testo = "";
    try { testo = await r.text(); } catch { /* connessione caduta a meta' corpo */ }
    let dati = null;
    try { dati = JSON.parse(testo); } catch { dati = null; }
    const detail = dati && dati.detail !== undefined ? String(dati.detail) : "";
    const dopo = r.headers && r.headers.get ? Number(r.headers.get("Retry-After")) : 0;
    return apiErrore(apiClassifica(r.status, detail), r.status, detail,
      { retryAfter: dopo || 0, data: dati || {} });
  }

  let comando = "", testo = "", fine = null;
  try {
    for await (const riga of righeEventi(r)) {
      let ev;
      try { ev = JSON.parse(riga); } catch { continue; }   // riga tagliata: si salta
      if (ev.type === "start") { comando = ev.command || ""; scriviParziale(comando, ""); }
      else if (ev.type === "out") { testo += ev.text || ""; scriviParziale(comando, testo); }
      else if (ev.type === "end") fine = ev;
    }
  } catch (e) {
    // Il flusso si e' rotto a meta': l'output gia' arrivato resta, ma non si
    // spaccia per completo.
    fine = null;
    testo += e && e.name === "AbortError"
      ? "\n… nessuna risposta entro il tempo massimo …"
      : "\n… collegamento interrotto …";
  } finally {
    clearTimeout(guardia);
  }

  return { ok: true, status: 200, data: {
    command: comando, output: testo,
    exit_code: fine ? fine.exit_code : null,
    timed_out: !!(fine && fine.timed_out),
    truncated: !!(fine && fine.truncated),
    duration_ms: fine ? fine.duration_ms : Date.now() - inizio,
  } };
}

async function runTool(view, tool) {
  // Doppio avvio: il pulsante si disabilita, ma l'Invio nel campo bersaglio no.
  if (toolsState.running) return;
  // Bersaglio mancante: si dice qui invece di spendere una richiesta. Il
  // backend la rifiuterebbe comunque, ma quel rifiuto consumava uno dei dieci
  // colpi al minuto, e dieci clic a vuoto bloccavano gli strumenti.
  if (!tool.no_target && !(toolsState.target || "").trim()) {
    toolsState.out = `${tool.label} ha bisogno di un bersaglio: ${segnaposto(tool)}`;
    return renderTools(view);
  }
  const opts = {};
  document.querySelectorAll(".tl-opt").forEach(el => {
    opts[el.dataset.k] = el.dataset.t === "bool" ? el.checked
      : el.dataset.t === "int" ? Number(el.value) : el.value;
  });
  toolsState.options = opts;
  toolsState.running = true;
  toolsState.out = "";
  renderTools(view);
  avviaCronometro(tool);

  // Senza bersaglio per i tool che non ne hanno: mandare l'ultimo host
  // digitato per un altro strumento sarebbe un parametro inventato.
  const corpo = { tool: tool.id, target: tool.no_target ? "" : toolsState.target, options: opts };
  // Quali strumenti escono man mano lo dichiara il catalogo del backend: un
  // secondo elenco scritto qui prima o poi divergerebbe da quello vero.
  let r = tool.stream ? await eseguiInDiretta(tool, corpo) : null;
  // `null` = sessione scaduta durante lo streaming: si ripassa da api(), che
  // sa rifare il login senza perdere la richiesta.
  if (!r) r = await api("/api/tools/run", { method: "POST", body: corpo });
  toolsState.running = false;
  fermaCronometro();

  if (!r.ok) {
    // Il 401 lo riprende gia' api() col login inline; qui resta il 503, cioe'
    // la password admin mai impostata, che il gate sa spiegare.
    if (r.error.kind === "servizio") return pageTools(view);
    toolsState.out = r.error.messaggio;
    return renderTools(view);
  }
  const data = r.data;
  toolsState.out = `$ ${data.command}\n\n${data.output}`.trimEnd()
    + `\n\n— ${esitoTool(data)} · ${data.duration_ms} ms`;
  renderTools(view);
}

/* ── Host di LANMng (interfacce e rotte) ──────────────────────────── */
/* Su un server con Docker le interfacce vere sono due e le virtuali una
   dozzina (bridge e veth dei container, senza indirizzo e con nomi casuali):
   di serie si mostrano solo quelle vere, dicendo quante se ne stanno
   nascondendo. La pagina si legge a richiesta, quindi dichiara anche di quando
   e' il dato. */
let hostState = { dati: null, virtuali: false, quando: 0, caricando: false };

function pageHost(view) {
  if (!hostState.dati) view.innerHTML = card(t("nav.host"), `<div class="empty">${h(t("host.letturaInCorso"))}</div>`);
  return caricaHost(view);
}

async function caricaHost(view) {
  if (hostState.caricando) return;
  hostState.caricando = true;
  const b = $("#hs-refresh");
  if (b) { b.disabled = true; b.textContent = "lettura…"; }
  const r = await api("/api/host/network");
  hostState.caricando = false;
  if (!r.ok) {
    view.innerHTML = card(t("nav.host"), noteErrore(r.error, "hs-retry"));
    const rb = $("#hs-retry");
    if (rb) rb.onclick = () => pageHost(view);
    return;
  }
  hostState.dati = r.data;
  hostState.quando = Date.now();
  renderHost(view, r.data);
}

function renderHost(view, d) {
  const tutte = d.interfaces || [];
  const mostrate = hostState.virtuali ? tutte : tutte.filter(i => !i.virtual);
  const nascoste = tutte.length - mostrate.length;
  const rotte = d.routes || [];
  // Fra piu' rotte di default vince quella con la metrica piu' bassa: dirlo
  // evita di leggere due default come "due connessioni attive insieme".
  const metricaAttiva = Math.min(...rotte.filter(r => r.default)
    .map(r => (r.metric == null ? 0 : r.metric)), Infinity);

  view.innerHTML = `
    ${alertBox("host")}
    <div class="muted" style="margin-bottom:12px;font-size:12px">
      Le interfacce di <b>${h(d.hostname || t("host.questoHost"))}</b>, la macchina che ospita LANMng —
      non quelle del router, che stanno in WAN. Lettura fatta con
      ${h(d.source === "ip" ? "iproute2" : "nmap (ripiego)")} ${h(quandoLetto())}.
    </div>
    ${card(t("host.interfacce"), `${mostrate.length ? `<table><thead><tr>
      <th>${h(t("comune.interfaccia"))}</th><th>${h(t("comune.stato"))}</th><th>${h(t("host.indirizzi"))}</th><th>MAC</th><th>${h(t("comune.velocita"))}</th>
      <th>MTU</th><th>RX / TX</th><th>${h(t("comune.errori"))}</th><th>${h(t("comune.subnet"))}</th></tr></thead>
      <tbody>${mostrate.map(hostIfRow).join("")}</tbody></table>`
      : `<div class="empty">${h(t("host.nessunaInterfaccia"))}</div>`}
      ${nascoste ? `<label class="muted" style="display:flex;align-items:center;gap:6px;
        margin-top:10px;cursor:pointer"><input type="checkbox" id="hs-virt"${
        hostState.virtuali ? " checked" : ""}> mostra anche le
        ${conta(nascoste, "conta.ifaceVirtuale")} (bridge e veth dei
        container)</label>` : ""}`,
      `<button class="btn" id="hs-refresh">&#8635; Aggiorna</button>`)}
    ${rotte.length ? card(t("host.rotte"), `<table><thead><tr>
      <th>${h(t("host.destinazione"))}</th><th>${h(t("host.gateway"))}</th><th>${h(t("comune.interfaccia"))}</th><th>${h(t("host.metrica"))}</th><th></th></tr></thead>
      <tbody>${rotte.map(r => {
        const attiva = r.default && (r.metric == null ? 0 : r.metric) === metricaAttiva;
        return `<tr${r.default ? ' style="font-weight:600"' : ""}>
        <td class="mono">${h(r.dest || "—")}</td><td class="mono">${h(r.gateway || "—")}</td>
        <td class="mono">${h(r.dev)}</td><td class="mono muted">${h(r.metric ?? "—")}</td>
        <td>${!r.default ? "" : attiva
          ? `<span class="chip ok">in uso</span>`
          : `<span class="chip ignoto" title="${h(t("host.metricaPiuAlta"))}">${h(t("host.diRiserva"))}</span>`}</td>
      </tr>`; }).join("")}</tbody></table>`) : ""}`;

  $("#hs-refresh").onclick = () => caricaHost(view);
  const v = $("#hs-virt");
  if (v) v.onchange = (e) => { hostState.virtuali = e.target.checked; renderHost(view, d); };
}

function quandoLetto() {
  if (!hostState.quando) return "";
  const q = new Date(hostState.quando);
  const due = (n) => String(n).padStart(2, "0");
  return `${t("host.alle")} ${due(q.getHours())}:${due(q.getMinutes())}:${due(q.getSeconds())}`;
}

/* Errori e scarti sono contatori dall'avvio della macchina: nove errori su
   quaranta milioni di pacchetti non sono un guasto, e colorarli di rosso
   faceva gridare al lupo. Si colora solo quando il tasso e' sensibile. */
function erroriIf(st) {
  const rxErr = st.rx_errors || 0, txErr = st.tx_errors || 0;
  const pacchetti = (st.rx_packets || 0) + (st.tx_packets || 0);
  const totale = rxErr + txErr;
  const tasso = pacchetti ? totale / pacchetti : 0;
  const grave = totale > 0 && tasso > 0.0001;      // piu' di uno su diecimila
  const scartati = (st.rx_dropped || 0) + (st.tx_dropped || 0);
  const titolo = pacchetti
    ? `${totale} errori su ${pacchetti.toLocaleString(I18N.locale())} pacchetti`
      + (scartati ? ` · ${scartati} scartati` : "")
      + " (contatori dall'avvio)"
    : "contatori dall'avvio";
  return `<td class="mono ${grave ? "err" : "muted"}" title="${h(titolo)}">${
    h(rxErr)} / ${h(txErr)}</td>`;
}

function hostIfRow(i) {
  const st = i.stats || {};
  return `<tr${i.virtual ? ' style="opacity:.55"' : ""}>
    <td class="mono">${h(i.name)} ${i.wireless ? '<span class="tag">wifi</span>' : ""}
        ${i.virtual ? '<span class="tag">virtuale</span>' : ""}</td>
    <td><span class="status-dot ${i.state === "up" ? "s-on" : "s-off"}"></span>${h(i.state || "?")}</td>
    <td class="mono">${(i.addresses || []).filter(a => a.family === "inet")
        .map(a => h(`${a.ip}/${a.prefix}`)).join("<br>") || "—"}</td>
    <td class="mono muted">${h(i.mac || "—")}</td>
    <td class="mono">${i.speed_mbps ? h(i.speed_mbps) + " Mb/s" : "—"}</td>
    <td class="mono">${h(i.mtu ?? "—")}</td>
    <td class="mono">${fmtBytes(st.rx_bytes)} / ${fmtBytes(st.tx_bytes)}</td>
    ${erroriIf(st)}
    <td>${(i.subnets || []).map(s => `<span class="tag">${h(s)}</span>`).join(" ")}</td></tr>`;
}

/* ── Terminale SSH ────────────────────────────────────────────────── */
const term = { ws: null, emu: null, host: "", status: "chiuso", ro: null };

function stopTerminal() {
  if (term.ro) { term.ro.disconnect(); term.ro = null; }
  if (term.ws) { try { term.ws.close(); } catch { /* gia' chiuso */ } term.ws = null; }
  if (term.emu) { term.emu.dispose(); term.emu = null; }
  term.status = "chiuso";
}

function pageTerminal(view) {
  sessionGate(view, async () => {
    const r = await api("/api/terminal/hosts");
    if (!r.ok) {
      view.innerHTML = card(t("term.titolo"), noteErrore(r.error, "tm-retry"));
      const b = $("#tm-retry");
      if (b) b.onclick = () => pageTerminal(view);
      return;
    }
    const info = r.data;
    if (!info.enabled) {
      view.innerHTML = card(t("term.titolo"), `<div class="empty">${h(t("term.disattivato"))}
        (<span class="mono">terminal.enabled</span>).</div>`);
      return;
    }
    term.host = term.host || (info.hosts[0] || {}).host || "";
    renderTerminal(view, info);
  });
}

/* Da dove arriva ogni host dell'elenco: senza questa spiegazione la lista
   sembra decisa da qualcun altro. Solo gli "ssh" si gestiscono da qui. */
const TERM_SOURCE = {
  router: { label: "router", hint: "dalla sezione router della configurazione" },
  systemd: { label: t("term.hostDiLanmng"), hint: "dalla sezione systemd della configurazione" },
  ssh: { label: t("term.aggiuntoDaTe"), hint: "elenco host SSH, gestibile qui sotto" },
};

/* Il terminale legge i tasti da un <div> focalizzabile (vedi terminal.js):
   su telefono e tablet toccarlo non fa comparire la tastiera virtuale, quindi
   la sessione si apre e mostra l'output ma non si puo' digitare. Meglio dirlo
   in pagina che lasciar credere a un guasto: serve una tastiera vera. */
function terminalTouchNotice() {
  // matchMedia va chiamata su window: staccarla in una variabile e invocarla
  // da sola solleva "Illegal invocation" e farebbe saltare tutta la pagina.
  if (!window.matchMedia || !window.matchMedia("(pointer: coarse)").matches) return "";
  return `<div class="cfg-note err" style="margin:0 0 12px">
    <b>${h(t("term.daTelefono"))}</b> Il terminale riceve i tasti
    da una tastiera vera (anche Bluetooth): con la sola tastiera a schermo la
    connessione si apre e mostra l'output, ma resta muta. Per lavorare davvero
    su un host usa un computer, oppure un client SSH del telefono.</div>`;
}

function renderTerminal(view, info) {
  const hosts = info.hosts || [];
  view.innerHTML = `
    ${terminalTouchNotice()}
    <div class="controls">
      <select id="tm-host"${hosts.length ? "" : " disabled"}>${hosts.map(hh =>
        `<option value="${h(hh.host)}"${hh.host === term.host ? " selected" : ""}>
           ${h(hh.label)} — ${h(hh.user)}@${h(hh.host)}</option>`).join("")
        || `<option>${h(t("term.nessunHost"))}</option>`}</select>
      <button class="btn" id="tm-conn"${hosts.length ? "" : " disabled"}
        style="border-color:var(--teal);color:var(--teal)">${h(t("comune.connetti"))}</button>
      <button class="btn" id="tm-close"${term.ws ? "" : " disabled"}>${h(t("comune.chiudi"))}</button>
      <span class="pill" id="tm-state"><span class="dot"></span> ${h(term.status)}</span>
      <span class="muted" style="margin-left:auto">chiusura automatica dopo
        ${Math.round((info.idle_timeout || 900) / 60)} min di inattivita'</span>
    </div>
    <div class="card" style="padding:0"><div id="tm-box"></div></div>
    <div class="muted" style="font-size:12px;margin-top:8px">
      ${t("term.aiuto")}
    </div>
    ${card(t("term.hostRaggiungibili"), `
      <table><thead><tr><th>${h(t("comune.host"))}</th><th>${h(t("comune.utente"))}</th><th>${h(t("comune.origine"))}</th><th></th></tr></thead>
        <tbody>${hosts.map(hh => `<tr>
          <td class="mono">${h(hh.host)}</td>
          <td class="mono muted">${h(hh.user)}</td>
          <td><span class="tag">${h((TERM_SOURCE[hh.source] || {}).label || hh.source)}</span>
              <span class="muted" style="font-size:11px">${h((TERM_SOURCE[hh.source] || {}).hint || "")}</span></td>
          <td class="right">${hh.editable
            ? `<button class="iconbtn" title="${h(t("term.rimuoviDallElenco"))}" data-rm="${h(hh.host)}">✕</button>`
            : `<span class="muted" style="font-size:11px">${h(t("term.daImpostazioni"))}</span>`}</td>
        </tr>`).join("") || `<tr><td colspan="4" class="muted">${h(t("term.nessunHostPunto"))}</td></tr>`}</tbody></table>
      <form class="controls" id="tm-add" style="margin:10px 0 0">
        <input type="text" id="tm-new-host" placeholder="IP o nome host…" style="min-width:170px">
        <input type="text" id="tm-new-user" placeholder="utente (vuoto = predefinito)" style="min-width:170px">
        <select id="tm-new-key" class="mono" style="min-width:210px">
          <option value="">chiave predefinita${info.default_key ? ` (${h(info.default_key)})` : ""}</option>
          ${(info.keys || []).map(k => `<option value="${h(k)}">${h(k)}</option>`).join("")}
        </select>
        <button class="btn" type="submit" style="border-color:var(--teal);color:var(--teal)">+ Aggiungi host</button>
        <span class="muted" id="tm-add-msg"></span>
      </form>
      <div class="muted" style="font-size:12px;margin-top:8px">
        La chiave si sceglie fra quelle presenti nella cartella montata nel container: per
        usarne una nuova, copiala li' (di norma <span class="mono">/app/ssh/</span>, cioe'
        <span class="mono">~/lanmng/ssh/</span> sul server) e ricarica la pagina. Va poi
        autorizzata sull'host di destinazione. Le modifiche valgono subito, senza riavviare.</div>`)}`;

  $("#tm-host").onchange = (e) => { term.host = e.target.value; };
  $("#tm-conn").onclick = () => connectTerminal();
  $("#tm-close").onclick = () => { stopTerminal(); renderTerminal(view, info); };
  $("#tm-add").onsubmit = (e) => { e.preventDefault(); addTerminalHost(view, info); };
  view.querySelectorAll("[data-rm]").forEach(b =>
    b.onclick = () => removeTerminalHost(view, info, b.dataset.rm));
  if (term.emu) $("#tm-box").appendChild(term.emu.el);
}

async function addTerminalHost(view, info) {
  const msg = $("#tm-add-msg");
  const body = {
    host: $("#tm-new-host").value.trim(),
    user: $("#tm-new-user").value.trim(),
    key: $("#tm-new-key").value.trim(),
  };
  if (!body.host) { msg.textContent = t("term.indicaIpOHost"); return; }
  msg.textContent = "salvataggio…";
  const r = await api("/api/terminal/hosts", { method: "POST", body });
  if (!r.ok) { msg.textContent = r.error.messaggio; return; }
  term.host = body.host;
  renderTerminal(view, { ...info, hosts: r.data.hosts });
}

async function removeTerminalHost(view, info, host) {
  if (!confirm(`Togliere ${host} dagli host raggiungibili dal terminale?`)) return;
  const r = await api(`/api/terminal/hosts/${encodeURIComponent(host)}`, { method: "DELETE" });
  if (!r.ok) return void toastErrore(r.error);
  if (term.host === host) term.host = (r.data.hosts[0] || {}).host || "";
  renderTerminal(view, { ...info, hosts: r.data.hosts });
}

/* Perche' la connessione si e' chiusa prima ancora di aprirsi.

   Il rifiuto avviene durante l'handshake (origine non consentita, sessione
   assente): il server chiude prima di accettare, quindi il browser riceve un
   1006 muto — niente codice, niente motivo. La pagina diceva solo "chiuso", e
   una sessione scaduta era indistinguibile da un terminale spento in
   configurazione. Si chiede allo stato di autenticazione, che il motivo ce l'ha. */
async function spiegaChiusuraTerminale(emu) {
  setTermStatus("chiuso", "down");
  const st = await api("/api/auth/status", { auth: false, retry: false, timeout: 4000 });
  const perche = !st.ok
    ? t("term.backendNonRisponde")
    : !st.data.session
      ? t("term.sessioneScaduta")
      : t("term.rifiutata");
  if (emu) emu.write(`\r\n\x1b[31mConnessione non aperta: ${perche}\x1b[0m\r\n`);
  setTermStatus(!st.ok ? t("term.backendNonRaggiungibile")
    : !st.data.session ? "sessione scaduta" : "connessione rifiutata", "down");
}

function setTermStatus(status, cls = "") {
  term.status = status;
  const el = $("#tm-state");
  if (el) { el.className = "pill " + cls; el.innerHTML = `<span class="dot"></span> ${h(status)}`; }
}

function connectTerminal() {
  stopTerminal();
  const box = $("#tm-box"); if (!box) return;
  // Se terminal.js non e' stato caricato (cache rotta, file non servito), il
  // clic finiva in un ReferenceError e non succedeva nulla: nessun messaggio,
  // nessuna spiegazione.
  if (typeof Terminal !== "function") {
    box.innerHTML = `<div class="cfg-note err" style="margin:12px">Il modulo del terminale
      (<span class="mono">terminal.js</span>${h(t("term.ricaricaPagina"))}</div>`;
    setTermStatus(t("term.nonCaricato"), "down");
    return;
  }
  const emu = new Terminal(box, { scrollback: 1500 });
  term.emu = emu;

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/terminal`);
  term.ws = ws;
  setTermStatus("connessione…");

  ws.onopen = () => {
    emu.fit();
    ws.send(JSON.stringify({ type: "open", host: term.host, cols: emu.cols, rows: emu.rows }));
  };
  // Se il server ha gia' detto qualcosa (aperta, errore, chiusa), la chiusura
  // che segue non deve cancellare quella spiegazione con un generico "chiuso".
  let spiegato = false;
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "data") emu.write(msg.data);
    else if (msg.type === "ready") {
      spiegato = true;
      setTermStatus(`connesso a ${msg.user}@${msg.host}`, "ok");
      emu.focus();
    } else if (msg.type === "error") {
      spiegato = true;
      emu.write(`\r\n\x1b[31m${msg.detail}\x1b[0m\r\n`);
      setTermStatus("errore", "down");
    } else if (msg.type === "closed") {
      spiegato = true;
      emu.write(`\r\n\x1b[33m${msg.reason}\x1b[0m\r\n`);
      setTermStatus("chiuso", "down");
    }
  };
  ws.onclose = () => { if (!spiegato) spiegaChiusuraTerminale(emu); };
  // `onerror` non porta con se' un motivo: la spiegazione la cerca onclose,
  // che arriva sempre subito dopo.
  ws.onerror = () => {};

  emu.onData(d => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "data", data: d })); });

  // Il terminale segue la finestra: ricalcola righe/colonne e avvisa il server.
  term.ro = new ResizeObserver(() => {
    const size = emu.fit();
    if (size && ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", ...size }));
  });
  term.ro.observe(emu.el);
  const chiudi = $("#tm-close");
  if (chiudi) chiudi.disabled = false;      // ora c'e' qualcosa da chiudere
  emu.focus();
}

/* ── WAN ──────────────────────────────────────────────────────────── */
function pageWan(view, s) {
  view.innerHTML = `
    ${alertBox("wan")}
    ${notaSorgente("interfaces")}
    <div id="wan-corpo">${wanCorpo(s)}</div>
    ${card(t("wan.provaConnessione"), `
      <div class="muted" style="font-size:12px;margin-bottom:10px">Il ping parte dal
        <b>router</b>, non da questo host: risponde alla domanda "esce la connessione di
        casa?" invece che "questo container vede internet?".</div>
      <div class="controls" style="margin-bottom:0">
        <input type="text" id="wan-ping-host" style="min-width:220px"
          placeholder="${h(sondaInternet())} (predefinito)">
        <button class="btn" id="wan-ping">${h(t("wan.pingDalRouter"))}</button>
        <span class="muted" id="wan-ping-esito"></span>
      </div>`)}
    ${card(t("wan.firewallRouter"), `
      <div class="muted" style="font-size:12px;margin-bottom:10px">Le regole in vigore adesso
        sul router (nftables). Si caricano su richiesta: sono migliaia di righe e leggerle ad
        ogni visita costerebbe una connessione SSH per niente.</div>
      <button class="btn" id="wan-fw">${h(t("wan.caricaRegole"))}</button>
      <div id="wan-fw-nota"></div>
      <pre class="logbox" id="wan-fw-box" hidden
           style="max-height:360px;white-space:pre;overflow:auto"></pre>`)}`;
  bindWan();
}

/* Aggiornamento parziale: le due card in fondo hanno un campo di testo e un
   riquadro caricato a mano, che un render pieno ogni dieci secondi
   cancellerebbe sotto le dita. */
function refreshWan(view, s) {
  const box = $("#wan-corpo");
  if (!box) return false;
  aggiornaNoteSorgente(["interfaces"]);
  box.innerHTML = wanCorpo(s);
  return true;
}

function sondaInternet() { return (state.snap.meta || {}).internet_probe || "8.8.8.8"; }

/* Indirizzo privato (RFC1918 / CGNAT): dietro c'e' un altro router che fa NAT,
   quindi quello mostrato NON e' l'indirizzo con cui si esce su internet. Vale
   la pena dirlo: qui l'uplink e' un tethering 4G dietro un altro apparato. */
function ipPrivato(ip) {
  const n = String(ip || "").split(".").map(Number);
  if (n.length !== 4 || n.some(x => !Number.isFinite(x))) return false;
  return n[0] === 10 || (n[0] === 172 && n[1] >= 16 && n[1] <= 31)
      || (n[0] === 192 && n[1] === 168) || (n[0] === 100 && n[1] >= 64 && n[1] <= 127);
}

/* Ultimo punto con una latenza vera e da quanto: `latency` e' null quando il
   probe non ha risposto (vedi _measure_latency nel collector), e quel buco e'
   proprio il caso da raccontare. */
function ultimaLatenza(serie) {
  const s = serie || [];
  for (let i = s.length - 1; i >= 0; i--)
    if (s[i].latency != null) return { ms: s[i].latency, t: s[i].t };
  return null;
}

function wanCorpo(s) {
  const ifaces = s.interfaces || [];
  const nome = s.wan_interface;
  const wan = ifaces.filter(i => i.name === nome);
  const serie = s.traffic_series || [];
  const ultimo = serie.length ? serie[serie.length - 1] : null;
  const rate = lastTrafficPoint(serie);
  const scorsa = ultimaLatenza(serie);
  const su = wan.some(i => i.up);
  const internetOk = !!(ultimo && ultimo.latency != null);
  const privati = wan.flatMap(i => i.ip4 || []).filter(ipPrivato);
  return `
    <div class="grid cols-3" style="margin-bottom:14px">
      ${kpi(h(nome || "—"), "uplink", "", {
        dot: !nome ? "s-off" : su ? "s-on" : "s-down",
        ctx: !nome ? t("wan.nonRiconosciuto")
           : wan.map(i => `${i.ifname || "?"} · ${(i.ip4 || []).join(", ") || "senza IP"}`).join(" · ")
             || t("wan.nessunDettaglio") })}
      ${kpi(!ultimo ? "–" : internetOk ? `${Math.round(ultimo.latency)}<span class="unit">ms</span>`
                                       : t("servizi.nonRisponde"), "internet", "", {
        dot: !ultimo ? "s-off" : internetOk ? "s-on" : "s-down",
        ctx: !ultimo ? t("wan.attesaPrimaMisura")
           : internetOk ? `ping verso ${sondaInternet()}`
           : scorsa ? `verso ${sondaInternet()} · ultima risposta ${ago(Math.round(scorsa.t / 1000))}`
                    : `verso ${sondaInternet()} · mai una risposta` })}
      ${kpi(rate.rx_bps == null ? "–"
            : `<span class="mono">↓ ${fmtRate(rate.rx_bps)}<br>↑ ${fmtRate(rate.tx_bps)}</span>`,
            "adesso", "", {
        ctx: wan.length ? `totali ${fmtMB(wan[0].rx_mb)} in · ${fmtMB(wan[0].tx_mb)} out`
                        : t("wan.nessunContatore") })}
    </div>
    ${card(t("wan.connessioneWan"), wan.length ? `
      <table><thead><tr><th>${h(t("comune.interfaccia"))}</th><th>IP</th><th>${h(t("comune.stato"))}</th>
        <th>RX totali</th><th>TX totali</th></tr></thead>
      <tbody>${wan.map(i => `<tr>
        <td class="mono">${h(i.name)} <span class="muted">${h(i.ifname)}</span></td>
        <td class="mono">${(i.ip4 || []).map(h).join(", ") || "—"}</td>
        <td><span class="status-dot ${i.up ? "s-on" : "s-off"}"></span>${i.up ? "connessa" : "giù"}</td>
        <td class="mono">${fmtMB(i.rx_mb)}</td><td class="mono">${fmtMB(i.tx_mb)}</td>
      </tr>`).join("")}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:10px">
        I totali sono contatori dall'ultimo riavvio del router. ${etaSorgente("interfaces")}</div>
      ${privati.length ? `<div class="cfg-note">L'uplink ha un indirizzo privato
        (${privati.map(h).join(", ")}): non e' l'indirizzo con cui esci su internet, c'e'
        un altro apparato davanti che fa NAT.</div>` : ""}`
      : wanVuoto(s, ifaces))}`;
}

/* Perche' non c'e' una WAN da mostrare. Prima era sempre "Nessuna interfaccia
   WAN", anche quando il router rispondeva benissimo e il problema era solo che
   l'uplink non era stato riconosciuto. */
function wanVuoto(s, ifaces) {
  const sorgente = (state.snap.sources || {}).interfaces;
  if (!ifaces.length) {
    if (!sorgente) return `<div class="empty">${h(t("wg.attesaRaccolta"))}</div>`;
    return `<div class="empty">${h(t("wan.nessunaInterfacciaRouter"))}${
      sorgente.ok ? "" : h(t("wan.nonRispondeDettaglio"))}.</div>`;
  }
  const candidati = (state.snap.meta || {}).wan_candidates || [];
  const su = ifaces.filter(i => i.up && (i.ip4 || []).length);
  return `<div class="cfg-note err">L'uplink non e' stato riconosciuto fra le
    ${ifaces.length} interfacce del router. Si cercano i nomi
    ${candidati.length ? candidati.map(c => `<code>${h(c)}</code>`).join(", ")
                       : "elencati in <code>router.wan_candidates</code>"},
    oppure quello scritto in <code>router.wan_interface</code>.</div>
    ${su.length ? `<div class="muted" style="font-size:12px;margin-top:8px">Interfacce su e con
      un indirizzo, se una di queste e' l'uplink: ${su.map(i =>
        `<code>${h(i.name)}</code> (${(i.ip4 || []).map(h).join(", ")})`).join(", ")}.</div>` : ""}`;
}

function bindWan() {
  const b = $("#wan-ping");
  if (b) b.onclick = async () => {
    const esito = $("#wan-ping-esito");
    const host = ($("#wan-ping-host").value || "").trim();
    b.disabled = true;
    esito.textContent = "ping in corso…";
    const r = await api(`/api/wan/ping?count=4${host ? `&host=${encodeURIComponent(host)}` : ""}`);
    b.disabled = false;
    if (!r.ok) { esito.textContent = r.error.messaggio; return; }
    // latency_ms null = nessuna misura: dirlo, invece di stampare uno zero che
    // sembrerebbe una risposta istantanea.
    esito.textContent = r.data.online
      ? `${r.data.host} risponde${r.data.latency_ms ? ` in ${Math.round(r.data.latency_ms)} ms` : ""}`
      : `${r.data.host} non risponde`;
  };
  const f = $("#wan-fw");
  if (f) f.onclick = async () => {
    const box = $("#wan-fw-box"), nota = $("#wan-fw-nota");
    f.disabled = true;
    nota.innerHTML = `<div class="muted" style="margin-top:8px">lettura dal router…</div>`;
    const r = await api("/api/wan/firewall");
    f.disabled = false;
    if (!r.ok) { nota.innerHTML = noteErrore(r.error); return; }
    const regole = r.data.rules || "";
    nota.innerHTML = regole ? "" : `<div class="muted" style="margin-top:8px">Il router non ha
      restituito regole.</div>`;
    box.textContent = regole;
    box.hidden = !regole;
  };
}

/* ── Logs ─────────────────────────────────────────────────────────── */
/* Quattro sorgenti (syslog del router, backend, registro di audit, journalctl
   sugli host) e un solo modo di mostrarle: il backend le normalizza tutte alla
   stessa forma, quindi qui c'e' un render solo.

   La pagina resta `live: false`: un `logread` via SSH ad ogni giro del
   collector costerebbe una connessione al minuto per dei dati che si guardano
   di rado, e un re-render ad ogni update azzererebbe i filtri. */
let logStato = { righe: 0, quando: 0, sorgenti: [], livelli: {}, ultime: [],
                 sorgente: "router", warning: "" };
let logSorgenti = [];          // da /api/logs/sources: nessun nome di host nel codice
let logTail = null;            // AbortController dello stream, quando si sta seguendo

const LIVELLI_LOG = [
  { id: "error", cls: "red", uno: "errore", molti: "errori" },
  { id: "warn", cls: "orange", uno: "warning", molti: "warning" },
  { id: "info", cls: "teal", uno: "info", molti: "info" },
  { id: "debug", cls: "", uno: "debug", molti: "debug" },
];

async function pageLogs(view) {
  view.innerHTML = `
    ${alertBox("logs")}
    <div class="controls">
      <select id="lsource" title="${h(t("logs.daQualeRegistro"))}"></select>
      <select id="llevel"><option value="">tutti i livelli</option>
        <option value="error">errori</option><option value="warn">warning</option>
        <option value="info">info</option><option value="debug">debug</option></select>
      <input type="text" id="lfilter" style="min-width:180px"
        placeholder="${h(t("logs.cercaNelLog"))}">
      <input type="text" id="lexclude" style="min-width:160px"
        placeholder="${h(t("logs.nascondi"))}">
      <select id="lperiod"><option value="">${h(t("logs.daSempre"))}</option>
        <option value="15m">ultimi 15 min</option><option value="1h">ultima ora</option>
        <option value="6h">ultime 6 ore</option><option value="24h">ultime 24 ore</option>
        <option value="7d">ultimi 7 giorni</option></select>
      <select id="llines">
        <option value="200">ultime 200</option>
        <option value="500" selected>ultime 500</option>
        <option value="2000">ultime 2000</option></select>
      <button class="btn" id="lrefresh">&#8635; Aggiorna</button>
      <button class="btn" id="lfollow">&#9654; Segui</button>
      <button class="btn" id="lexport">&#8681; Esporta</button>
      <span class="muted" id="lstato" style="margin-left:auto"></span>
    </div>
    <div id="llivelli"></div>
    <div id="lnota"></div>
    <div class="logbox" id="lbox"><div class="empty">caricamento…</div></div>`;
  bindLogs();
  await caricaSorgenti();
  // Atteso: `go()` non aspetta il render, ma chi chiama pageLogs (i test, e un
  // domani un pulsante "ricarica tutto") deve poter sapere quando c'e' il dato.
  await caricaLogs();
}

function bindLogs() {
  $("#lrefresh").onclick = caricaLogs;
  $("#llevel").onchange = caricaLogs;
  $("#llines").onchange = caricaLogs;
  $("#lperiod").onchange = caricaLogs;
  $("#lfilter").onchange = caricaLogs;
  $("#lexclude").onchange = caricaLogs;
  // Cambiare registro mentre si sta seguendo lascerebbe aperto lo stream
  // vecchio, che continuerebbe a versare righe dell'altra sorgente.
  $("#lsource").onchange = () => { stopLogTail(); return caricaLogs(); };
  $("#lfollow").onclick = toggleLogTail;
  $("#lexport").onclick = esportaLogs;
}

/* L'elenco delle sorgenti arriva dal backend: quali esistono dipende dalla
   configurazione (host SSH, audit acceso o spento) e nessun nome di macchina
   deve vivere qui dentro. */
async function caricaSorgenti() {
  const sel = $("#lsource");
  if (!sel) return;
  const r = await api("/api/logs/sources");
  logSorgenti = (r.ok && r.data && r.data.sources) ? r.data.sources : [];
  // Se l'elenco non arriva resta almeno il syslog: e' la sorgente che c'e'
  // sempre, e una pagina senza selettore sarebbe peggio di una con una voce.
  if (!logSorgenti.length) logSorgenti = [{ id: "router", label: t("logs.routerSyslog") }];
  const scelta = logSorgenti.some(s => s.id === logStato.sorgente)
    ? logStato.sorgente : logSorgenti[0].id;
  sel.innerHTML = logSorgenti.map(s =>
    `<option value="${h(s.id)}"${s.id === scelta ? " selected" : ""}>${h(s.label)}</option>`
  ).join("");
  sel.value = scelta;
}

function sorgenteScelta() {
  const sel = $("#lsource");
  return (sel && sel.value) || logStato.sorgente || "router";
}

function logQuery() {
  return new URLSearchParams({
    source: sorgenteScelta(),
    lines: $("#llines").value || "500",
    level: $("#llevel").value || "",
    period: ($("#lperiod") && $("#lperiod").value) || "",
    filter: termineLog(),
    exclude: ($("#lexclude").value || "").trim(),
  });
}

async function caricaLogs() {
  const box = $("#lbox");
  if (!box) return;
  const q = logQuery();
  box.innerHTML = `<div class="empty">caricamento…</div>`;
  $("#lstato").textContent = "";
  // I log del router arrivano via SSH: "Errore nel caricamento" non diceva
  // se era giu' il router, il backend o la sessione.
  const r = await api(`/api/logs/?${q}`);
  if (!r.ok) {
    box.innerHTML = noteErrore(r.error, "lg-retry");
    $("#lnota").innerHTML = "";
    $("#llivelli").innerHTML = "";
    const b = $("#lg-retry");
    if (b) b.onclick = caricaLogs;
    return;
  }
  const righe = r.data.lines || [];
  logStato = { righe: righe.length, quando: Date.now(), sorgenti: r.data.sources || [],
               livelli: r.data.levels || {}, ultime: righe,
               sorgente: r.data.source || sorgenteScelta(), warning: r.data.warning || "",
               filtri: r.data.filtri || 0 };
  box.innerHTML = righe.map(rigaLog).join("") || logVuoto();
  box.scrollTop = box.scrollHeight;
  aggiornaStatoLogs();
  $("#llivelli").innerHTML = contatoriLivelli();
  bindContatoriLivelli();
  const rumore = sorgenteRumorosa();
  $("#lnota").innerHTML = notaWarning() + notaFiltriLogs() + notaRumoreLogs(rumore);
  const b = $("#lg-nascondi");
  // Il nome viaggia nella chiusura, non in un attributo del bottone: e' un
  // dato che abbiamo gia' in mano, e riprenderlo dal DOM sarebbe un giro in piu'.
  // Ritorna la promessa della ricarica: gli altri controlli lo fanno gia'
  // (sono `= caricaLogs`), e serve a chi aspetta l'esito.
  if (b && rumore) b.onclick = () => {
    // Si aggiunge, non si sostituisce: i rumori sono spesso due (le connessioni
    // SSH del monitoraggio e il cron del modem), e nasconderne uno solo lascia
    // il log illeggibile lo stesso.
    const gia = ($("#lexclude").value || "").trim();
    $("#lexclude").value = gia ? `${gia}, ${rumore.src}` : rumore.src;
    return caricaLogs();
  };
}

/* Una riga. L'evidenziazione si applica al testo GIA' passato per `h()`:
   applicarla prima vorrebbe dire far escapare i tag appena inseriti (si
   vedrebbero scritti), applicarla al testo grezzo aprirebbe un'iniezione. */
function rigaLog(l) {
  const termine = termineLog();
  const src = l.src ? `<span class="src">${evidenzia(h(l.src), termine)}</span> ` : "";
  return `<div class="l ${h(l.level)}"><span class="t">${h(l.ts)}</span> ${src}${
    evidenzia(h(l.msg), termine)}</div>`;
}

function termineLog() {
  const el = $("#lfilter");
  return el ? (el.value || "").trim() : "";
}

/* Evidenzia `termine` dentro un testo GIA' escapato. Il termine va escapato a
   sua volta prima del confronto, altrimenti cercare "&" non troverebbe niente:
   nel testo e' gia' diventato "&amp;". */
function evidenzia(testoEscapato, termine) {
  const cercato = h(termine || "").trim();
  if (!cercato) return testoEscapato;
  const pezzi = [];
  let resto = testoEscapato, i;
  const basso = cercato.toLowerCase();
  while ((i = resto.toLowerCase().indexOf(basso)) !== -1) {
    pezzi.push(resto.slice(0, i), `<mark>${resto.slice(i, i + cercato.length)}</mark>`);
    resto = resto.slice(i + cercato.length);
  }
  pezzi.push(resto);
  return pezzi.join("");
}

/* Quanti errori e quanti warning, contati PRIMA del filtro per livello: senza,
   guardando gli info si leggerebbe sempre "0 errori". Sono cliccabili perche'
   il gesto naturale dopo aver letto "3 errori" e' volerli vedere. */
function contatoriLivelli() {
  const lv = logStato.livelli || {};
  const totale = LIVELLI_LOG.reduce((n, x) => n + (lv[x.id] || 0), 0);
  if (!totale) return "";
  const scelto = $("#llevel") ? ($("#llevel").value || "") : "";
  const chip = (x) => {
    const n = lv[x.id] || 0;
    return `<button class="tag ${x.cls}${scelto === x.id ? " attivo" : ""}"
      data-livello="${x.id}">${n} ${h(n === 1 ? x.uno : x.molti)}</button>`;
  };
  return `<div class="log-livelli">${LIVELLI_LOG.filter(x => lv[x.id]).map(chip).join("")}
    <button class="tag${scelto ? "" : " attivo"}" data-livello="">tutti</button></div>`;
}

function bindContatoriLivelli() {
  const box = $("#llivelli");
  if (!box || !box.querySelectorAll) return;
  box.querySelectorAll("[data-livello]").forEach(b => {
    b.onclick = () => { $("#llevel").value = b.dataset.livello; return caricaLogs(); };
  });
}

/* Una sorgente che non ha risposto dice il motivo, invece di lasciar credere
   che il log sia vuoto. Il caso vero e' journalctl senza permessi sull'host. */
function notaWarning() {
  return logStato.warning ? `<div class="cfg-note">${h(logStato.warning)}</div>` : "";
}

/* Le regole di scarto in `logs.exclude` tolgono righe prima che si arrivi qui.
   Vanno dichiarate: un filtro silenzioso e' il modo migliore per far cercare
   per mezz'ora una riga che dovrebbe esserci. Il numero basta — quali siano
   sta in Impostazioni, che e' anche l'unico posto dove si cambiano. */
function notaFiltriLogs() {
  const n = logStato.filtri || 0;
  if (!n) return "";
  return `<div class="cfg-note">${conta(n, "conta.regolaScarto")}
    su questa sorgente: le righe che vi corrispondono non vengono mostrate ne'
    archiviate (<code>logs.exclude</code>, si cambia in Impostazioni).</div>`;
}

/* Il vuoto ha due cause diverse: nessuna riga soddisfa i filtri, oppure il log
   e' davvero vuoto. Prima erano la stessa frase. */
function logVuoto() {
  const filtri = [$("#lfilter").value, $("#lexclude").value, $("#llevel").value,
                  $("#lperiod") && $("#lperiod").value].some(x => (x || "").trim());
  const voce = (logSorgenti.find(s => s.id === logStato.sorgente) || {}).label || t("logs.ilLog");
  return `<div class="empty">${filtri
    ? t("logs.nessunaRigaFiltri")
    : `${h(voce)}: nessuna riga.`}</div>`;
}

function aggiornaStatoLogs() {
  const el = $("#lstato");
  if (!el) return;
  const ora = new Date(logStato.quando);
  const due = (n) => String(n).padStart(2, "0");
  el.textContent = `${conta(logStato.righe, "conta.riga")} · ${
    logTail ? "in diretta dalle" : "letto alle"} ${
    due(ora.getHours())}:${due(ora.getMinutes())}:${due(ora.getSeconds())}`;
}

/* Chi sta riempiendo il log. Il monitoraggio si vede nel log che sta leggendo:
   ogni giro LANMng apre connessioni SSH al router e il demone le registra —
   sull'istanza erano 240 righe su 300, e cercare qualunque altra cosa non
   trovava niente. Invece di lasciarlo scoprire, la pagina lo dice e offre di
   nasconderlo; il nome del processo viene dai dati, non dal codice. */
function sorgenteRumorosa() {
  const top = (logStato.sorgenti || [])[0];
  const gia = ($("#lexclude").value || "").toLowerCase().split(",")
    .map(x => x.trim()).filter(Boolean);
  // Piu' della meta' delle righe: sotto quella soglia non e' un padrone del
  // log, e proporre di nascondere una sorgente su due sarebbe di troppo.
  if (!top || !logStato.righe || top.count * 2 <= logStato.righe) return null;
  if (gia.some(x => top.src.toLowerCase().includes(x))) return null;
  return top;
}

function notaRumoreLogs(top) {
  if (!top) return "";
  return `<div class="cfg-note">${h(top.count)} righe su ${h(logStato.righe)} vengono da
    <code>${h(top.src)}</code>. Se non e' quello che cerchi, nasconderlo lascia vedere il
    resto (piu' d'uno: separali con la virgola). <button class="btn" id="lg-nascondi" data-src="${h(top.src)}"
      style="margin-left:8px">Nascondi ${h(top.src)}</button></div>`;
}

/* ── Segui in tempo reale ──────────────────────────────────────────
   NDJSON, non il WebSocket: `/ws` e' un broadcast del collector senza
   sottoscrizioni per client, quindi passare di li' vorrebbe dire mandare a
   ogni dashboard aperta i log filtrati da qualcun altro. E' la stessa strada
   dei tool di rete, gia' verificata attraverso l'nginx di produzione. */
function toggleLogTail() {
  if (logTail) { stopLogTail(); return; }
  return startLogTail();
}

function stopLogTail() {
  if (!logTail) return;
  try { logTail.abort(); } catch (e) { /* gia' chiuso */ }
  logTail = null;
  const b = $("#lfollow");
  if (b) { b.innerHTML = "&#9654; Segui"; b.classList.remove("attivo"); }
  aggiornaStatoLogs();
}

async function startLogTail() {
  const b = $("#lfollow");
  const mio = new AbortController();
  logTail = mio;
  if (b) { b.innerHTML = "&#9632; Ferma"; b.classList.add("attivo"); }
  let r;
  try {
    r = await fetch(`/api/logs/stream?${logQuery()}`, { signal: mio.signal });
  } catch (e) {
    if (mio === logTail) stopLogTail();
    if (!(e && e.name === "AbortError")) toastErrore(apiErrore("rete", 0, "").error);
    return;
  }
  if (!r.ok) {
    stopLogTail();
    let testo = "";
    try { testo = await r.text(); } catch (e) { /* connessione caduta a meta' */ }
    let dati = null;
    try { dati = JSON.parse(testo); } catch (e) { dati = null; }
    const detail = dati && dati.detail !== undefined ? String(dati.detail) : "";
    // Sessione scaduta a meta': la ripresa col login inline la sa fare solo
    // api(), che pero' non legge un flusso. Si ricarica la pagina — da li'
    // l'utente rifa' il login una volta sola e puo' rimettersi a seguire.
    if (r.status === 401) return caricaLogs();
    toastErrore(apiErrore(apiClassifica(r.status, detail), r.status, detail).error);
    return;
  }
  try {
    for await (const linea of righeEventi(r)) {
      if (mio !== logTail) break;                   // fermato nel frattempo
      let ev;
      try { ev = JSON.parse(linea); } catch (e) { continue; }   // riga tagliata
      if (ev.type === "line") appendiRigaLog(ev.line);
      else if (ev.type === "warning")
        $("#lnota").innerHTML = `<div class="cfg-note">${h(ev.text)}</div>`;
      else if (ev.type === "end") break;
    }
  } catch (e) {
    if (mio === logTail && !(e && e.name === "AbortError"))
      toastErrore(apiErrore("rete", 0, "").error);
  }
  if (mio === logTail) stopLogTail();
}

/* Si accoda al DOM senza rifare il render: rifarlo ad ogni riga azzererebbe la
   posizione dello scorrimento e il campo in cui si sta scrivendo. E' la stessa
   lezione dei tool in diretta (0.1.72). */
function appendiRigaLog(l) {
  const box = $("#lbox");
  if (!box || !l) return;
  if (box.querySelector && box.querySelector(".empty")) box.innerHTML = "";
  box.insertAdjacentHTML("beforeend", rigaLog(l));
  logStato.righe += 1;
  logStato.quando = Date.now();
  logStato.ultime = (logStato.ultime || []).concat([l]);
  logStato.livelli[l.level] = (logStato.livelli[l.level] || 0) + 1;
  box.scrollTop = box.scrollHeight;
  aggiornaStatoLogs();
  $("#llivelli").innerHTML = contatoriLivelli();
  bindContatoriLivelli();
}

/* ── Esportazione ──────────────────────────────────────────────────
   Si esportano le righe che si stanno guardando, non una seconda
   interrogazione con altri filtri: allegare a una segnalazione un file diverso
   da quello che si aveva davanti e' peggio che non allegarlo. */
function esportaLogs() {
  const righe = logStato.ultime || [];
  if (!righe.length) { toast(t("msg.nienteDaEsportare"), { level: "warn" }); return; }
  const json = confirm("OK per il formato JSON (con livello, sorgente e orario),\n" +
                       t("chiedi.formatoExport"));
  const nome = `lanmng-${String(logStato.sorgente).replace(/[^a-z0-9]+/gi, "-")}-${
    new Date().toISOString().slice(0, 19).replace(/[:T-]/g, "")}`;
  if (json)
    scaricaFile(`${nome}.json`, JSON.stringify(righe, null, 2), "application/json");
  else
    scaricaFile(`${nome}.txt`,
                righe.map(l => l.raw || `${l.ts} ${l.src} ${l.msg}`).join("\n"), "text/plain");
}

/* Unico punto in cui la dashboard consegna un file al browser. L'URL va
   revocato: senza, ogni esportazione lascia in memoria una copia del log
   finche' non si ricarica la pagina. */
function scaricaFile(nome, contenuto, mime) {
  try {
    const url = URL.createObjectURL(new Blob([contenuto], { type: `${mime};charset=utf-8` }));
    const a = document.createElement("a");
    a.href = url;
    a.download = nome;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    return true;
  } catch (e) {
    toast(t("msg.downloadNegato"), { level: "warn" });
    return false;
  }
}

/* ── Impostazioni (editor config.yaml) ────────────────────────────── */
/* Ultimo YAML letto o salvato: serve a sapere se c'e' del lavoro non salvato
   nell'editor. Prima uscire dalla pagina buttava via le modifiche in silenzio. */
let cfgSalvato = null;

function cfgSporco() {
  const ed = $("#cfg-yaml");
  return !!ed && cfgSalvato !== null && ed.value !== cfgSalvato;
}

/* Mezza pagina chiede di riavviare il servizio e nessuno diceva come si fa:
   il proprietario non e' uno sviluppatore, e "riavvia" senza il come e' un
   ordine senza istruzioni. Niente percorsi scritti qui dentro: il comando e'
   quello di docker, la cartella la conosce chi ha installato lo stack. */
function notaRiavvio() {
  return `<div class="cfg-note" style="margin-top:10px">
    <div id="riavvio-azione" style="margin-bottom:8px"><span class="muted">…</span></div>
    <b>${h(t("impostazioni.ancheAMano2"))}</b>, ${t("impostazioni.riavvioAMano")}</div>`;
}

/* Il pulsante compare solo se c'e' qualcuno che riaccende il servizio: il
   backend non si riavvia da se', esce e basta, e a farlo ripartire e' la
   politica di riavvio del container. Dove non c'e', premerlo lascerebbe la
   dashboard spenta e irraggiungibile proprio dalla pagina che si stava usando:
   meglio dire perche' non si puo' che offrire un pulsante che spegne. */
async function caricaRiavvio() {
  const box = $("#riavvio-azione");
  if (!box) return;
  const r = await api("/api/config/restart");
  if (!r.ok) {
    box.innerHTML = `<span class="muted">Riavvio dalla dashboard: stato non
      leggibile — ${h(r.error.messaggio)}</span>`;
    return;
  }
  const st = r.data;
  if (!st.available) {
    box.innerHTML = `<span class="muted">Da qui non si puo' riavviare: ${h(st.reason)}.</span>`;
    return;
  }
  box.innerHTML = `<button class="btn" id="riavvio-ora"
      style="border-color:var(--orange);color:var(--orange)">${h(t("impostazioni.riavviaServizio"))}</button>
    <span class="muted" style="margin-left:8px">${st.reason
      ? h(st.reason)
      : `il container riparte da solo (<span class="mono">restart: ${h(st.policy)}</span>)`}</span>`;
  $("#riavvio-ora").onclick = () => riavviaServizio(st);
}

async function riavviaServizio(st) {
  // Chi preme non deve scoprire dopo che stava buttando via il lavoro
  // nell'editor: la configurazione non salvata non sopravvive al riavvio.
  if (cfgSporco() && !confirm(t("impostazioni.riavvioPerde"))) return;
  const conseguenze = t("impostazioni.sessioniSiChiudono");
  if (!confirm(st.policy
      ? `Riavviare il servizio adesso? ${conseguenze}`
      : `Riavviare il servizio adesso? ${conseguenze}\n\nAttenzione: ${st.reason}.`)) return;
  await conPulsante($("#riavvio-ora"), async () => {
    const r = await api("/api/config/restart", { method: "POST" });
    if (!r.ok) return toast(r.error.messaggio, { level: "err", chiave: "riavvio" });
    // Dura quanto basta a coprire il riavvio: un avviso che resta anche dopo
    // che tutto e' tornato a posto e' solo rumore.
    toast(t("msg.riavvioInCorso"), { level: "info", durata: 20000, chiave: "riavvio" });
  }, "riavvio…");
}

/* Un pulsante che sta zitto mentre lavora sembra rotto, e due clic salvano due
   volte: si disabilita e dice cosa sta facendo. */
async function conPulsante(btn, azione, etichetta = "salvataggio…") {
  if (!btn || btn.disabled) return;
  const testo = btn.textContent;
  btn.disabled = true;
  btn.textContent = etichetta;
  try {
    return await azione();
  } finally {
    btn.disabled = false;
    btn.textContent = testo;
  }
}

/* Selettore dei temi. I campioni non sono disegnati qui: ogni pulsante porta
   il suo `data-tema` e il CSS gli applica addosso le variabili di quel tema,
   cosi' la tavolozza vive in un posto solo (styles.css). */
function editorTemi() {
  const box = $("#ed-temi");
  if (!box) return;
  const attivo = temaAttivo();
  const campione = ["--bg", "--panel-2", "--teal", "--green", "--orange"]
    .map(v => `<i style="background:var(${v})"></i>`).join("");
  box.innerHTML = TEMI.map(tema => `
    <button class="tema-scelta${tema.id === attivo ? " attivo" : ""}" data-tema="${h(tema.id)}">
      <span class="tema-campione">${campione}</span>
      <span class="tema-riga"><span class="tema-nome">${h(t(tema.nome))}</span>${
        t.id === attivo ? `<span class="tema-uso">in uso</span>` : ""}</span>
      <span class="tema-nota">${h(t(tema.nota))}</span>
    </button>`).join("");
  box.querySelectorAll(".tema-scelta").forEach(b => {
    b.onclick = () => { applicaTema(b.dataset.tema); editorTemi(); };
  });
}

function editorLingua() {
  const box = $("#ed-lingua");
  if (!box) return;
  box.innerHTML = I18N.disponibili.map(l => `
    <button class="btn${l === I18N.lang ? " attivo" : ""}" data-lingua="${h(l)}"
      ${l === I18N.lang ? 'style="border-color:var(--teal);color:var(--teal)"' : ""}>
      ${h(t("lingua." + l))}</button>`).join("");
  box.querySelectorAll("[data-lingua]").forEach(b => {
    b.onclick = () => {
      I18N.setLang(b.dataset.lingua);
      // La cornice (menu, topbar) sta fuori dalla pagina: si ridisegna a parte,
      // altrimenti resterebbe nella lingua di prima fino a un ricaricamento.
      I18N.applicaStatico();
      go(state.route);
    };
  });
}

async function pageSettings(view) {
  view.innerHTML = `
    ${alertBox("settings")}
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.aspettoTema"))} <span class="right muted">${h(t("impostazioni.soloQuestoBrowser"))}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        Il tema si applica subito e resta ricordato in questo browser: dal telefono
        puoi usarne uno diverso che dal PC. Senza una scelta si segue il tema del
        sistema. Il terminale SSH resta scuro con qualunque tema, e i colori delle
        subnet sulla mappa restano i tuoi (si cambiano qui sotto, in Personalizzazione).
      </div>
      <div class="temi" id="ed-temi"></div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("lingua.etichetta"))} <span class="right muted">${h(t("lingua.nota"))}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">${h(t("lingua.spiegazione"))}</div>
      <div class="controls" style="margin-bottom:0" id="ed-lingua"></div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.passwordAdmin"))} <span class="right muted">${h(t("impostazioni.loginDashboard"))}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        Imposta o cambia la password di accesso (min 6 caratteri), salvata come hash bcrypt nei segreti.
        Il login e' attivo con <code>auth.method: basic</code> e <code>bypass_lan: false</code> in config.yaml
        (le modifiche richiedono il riavvio). Con altri valori la dashboard mostra l'avviso rosso in alto:
        le API rispondono a chiunque raggiunga questo indirizzo.
      </div>
      <div class="controls" style="margin-bottom:0">
        <input type="password" id="pw-new" placeholder="nuova password" autocomplete="new-password">
        <button class="btn" id="pw-save" style="border-color:var(--teal);color:var(--teal)">${h(t("impostazioni.salvaPassword"))}</button>
        <span id="pw-msg" class="muted"></span>
      </div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.segreti"))} <span class="right muted">${h(t("impostazioni.segretiNota"))}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        Password e hash NON stanno in config.yaml ma in <code>secrets.env</code> (priorita' sopra config.yaml).
        Lascia vuoto un campo per non cambiarlo. Richiede il <b>riavvio</b> del servizio
        (come si fa e' spiegato in fondo, sotto l'editor della configurazione).
      </div>
      <div id="sec-fields"><div class="muted">caricamento…</div></div>
      <div id="sec-msg"></div>
      <div class="controls" style="margin-top:12px;margin-bottom:0">
        <button class="btn" id="sec-save" style="border-color:var(--teal);color:var(--teal)">${h(t("impostazioni.salvaSegreti"))}</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.personalizzazione"))} <span class="right muted">richiede riavvio</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        Editor guidati per le sezioni piu' usate di <code>config.yaml</code>. Ogni salvataggio
        crea un backup e richiede il <b>${h(t("impostazioni.riavvioServizio"))}</b> per essere applicato.
      </div>
      <h4 style="margin:6px 0">${h(t("impostazioni.subnetMappa"))}</h4>
      <div id="ed-subnets" class="muted">…</div>
      <h4 style="margin:16px 0 6px">${h(t("impostazioni.peerWg"))}</h4>
      <div id="ed-wg" class="muted">…</div>
      <h4 style="margin:16px 0 6px">${h(t("impostazioni.discoverySsh"))}</h4>
      <div id="ed-disc" class="muted">…</div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.alertSilenziati"))} <span class="right muted">${h(t("impostazioni.nessunRiavvio"))}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        Avvisi che non devono piu' suonare, con il motivo per cui li hai zittiti.
        Soggetto vuoto = tutta la regola. Le modifiche valgono entro pochi secondi:
        il servizio rilegge questa sezione da solo.
      </div>
      <div id="ed-alerts" class="muted">…</div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>${h(t("impostazioni.configAvanzato"))} <span class="right muted mono" id="cfg-path"></span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        I segreti (password, hash) sono mascherati con <code>********</code>:
        lascia la maschera per non modificarli. Le modifiche vengono validate e
        salvate con backup automatico, ma richiedono il <b>${h(t("impostazioni.riavvioServizio"))}</b> per essere applicate.
      </div>
      <textarea id="cfg-yaml" class="config-editor" spellcheck="false">caricamento…</textarea>
      <div id="cfg-msg"></div>
      <div class="controls" style="margin-top:12px;margin-bottom:0">
        <button class="btn" id="cfg-save" style="border-color:var(--teal);color:var(--teal)">${h(t("comune.salva"))}</button>
        <button class="btn" id="cfg-reload">${h(t("azione.ricarica"))}</button>
        <span class="muted" id="cfg-dirty"></span>
      </div>
      ${notaRiavvio()}
    </div>
    ${card(t("impostazioni.backupConfig"), `<div id="cfg-backups" class="muted">…</div>`)}`;

  const msg = (html, cls) => { $("#cfg-msg").innerHTML = html ? `<div class="cfg-note ${cls || ""}">${html}</div>` : ""; };

  async function load() {
    msg("");
    const r = await api("/api/config/");
    // "Impossibile leggere la configurazione" non diceva se il servizio era
    // spento, la sessione scaduta o il file illeggibile.
    if (!r.ok) return msg(h(r.error.messaggio), "err");
    $("#cfg-yaml").value = r.data.yaml || "";
    cfgSalvato = $("#cfg-yaml").value;
    segnaModifiche();
    $("#cfg-path").textContent = r.data.path || "";
  }

  function segnaModifiche() {
    const el = $("#cfg-dirty");
    if (el) el.textContent = cfgSporco() ? t("impostazioni.modificheNonSalvate") : "";
  }
  async function loadBackups() {
    const box = $("#cfg-backups");
    const rb = await api("/api/config/backups");
    // Un elenco vuoto e un elenco non leggibile davano lo stesso trattino.
    if (!rb.ok) { box.innerHTML = noteErrore(rb.error, "cfg-bak-retry");
      $("#cfg-bak-retry").onclick = loadBackups; return; }
    {
      const d = rb.data;
      if (!d.backups || !d.backups.length) { box.innerHTML = `<div class="empty">${h(t("impostazioni.nessunBackup"))}</div>`; return; }
      box.innerHTML = `<table><tbody>${d.backups.map(b => `<tr>
        <td class="mono">${h(b.name)}</td>
        <td class="right muted">${new Date(b.mtime * 1000).toLocaleString()}</td>
        <td class="right"><button class="btn" data-bak="${h(b.name)}">${h(t("impostazioni.ripristina"))}</button></td>
      </tr>`).join("")}</tbody></table>`;
      box.querySelectorAll("button[data-bak]").forEach(btn => btn.onclick = async () => {
        if (!confirm(`Ripristinare ${btn.dataset.bak}? Lo stato attuale viene comunque salvato in un nuovo backup.`)) return;
        const r = await api(`/api/config/backups/${encodeURIComponent(btn.dataset.bak)}/restore`, { method: "POST" });
        if (r.ok) { msg(t("impostazioni.backupRipristinato"), "ok"); load(); loadBackups(); }
        else msg(h(r.error.messaggio), "err");
      });
    }
  }

  $("#cfg-save").onclick = () => conPulsante($("#cfg-save"), async () => {
    msg(t("impostazioni.salvataggio"));
    const testo = $("#cfg-yaml").value;
    const r = await api("/api/config/", { method: "PUT", body: { yaml: testo } });
    if (r.ok) {
      cfgSalvato = testo;
      segnaModifiche();
      msg(t("impostazioni.salvatoRiavvia", { riavvia: t("impostazioni.riavviaServizio") }), "ok");
      loadBackups();
    } else msg(t("impostazioni.nonSalvato") + h(r.error.messaggio), "err");
  });
  $("#cfg-reload").onclick = () => {
    if (cfgSporco() && !confirm(t("chiedi.ricaricaPerdeModifiche"))) return;
    return load();
  };
  $("#cfg-yaml").oninput = segnaModifiche;

  // ── Segreti ──────────────────────────────────────────────────────
  const secMsg = (html, cls) => { $("#sec-msg").innerHTML = html ? `<div class="cfg-note ${cls || ""}">${html}</div>` : ""; };
  async function loadSecrets() {
    const rs = await api("/api/config/secrets");
    if (!rs.ok) {
      $("#sec-fields").innerHTML = noteErrore(rs.error, "sec-retry");
      $("#sec-retry").onclick = loadSecrets;
      return;
    }
    {
      const d = rs.data;
      $("#sec-fields").innerHTML = `<table><tbody>${(d.secrets || []).map(s => `<tr>
        <td>${h(s.label)} <span class="tag ${s.set ? "green" : ""}">${h(s.set ? t("impostazioni.impostato") : t("impostazioni.nonImpostato"))}</span></td>
        <td><input type="password" class="sec-in" data-id="${h(s.id)}" autocomplete="new-password"
             placeholder="${s.set ? "•••••• (lascia vuoto per non cambiare)" : "inserisci valore"}"
             style="width:100%;background:var(--bg-2);border:1px solid var(--border-2);color:var(--text);border-radius:7px;padding:7px 10px"></td>
        ${s.set ? `<td class="right"><button class="iconbtn" title="${h(t("comune.rimuovi"))}" data-clear="${h(s.id)}">✕</button></td>` : "<td></td>"}
      </tr>`).join("")}</tbody></table>`;
      $("#sec-fields").querySelectorAll("button[data-clear]").forEach(b => b.onclick = async () => {
        if (!confirm(t("chiedi.rimuoviSegreto"))) return;
        await putSecrets({ [b.dataset.clear]: "__CLEAR__" });
      });
    }
  }
  async function putSecrets(values) {
    const r = await api("/api/config/secrets", { method: "PUT", body: { values } });
    if (r.ok) { secMsg(t("impostazioni.segretiSalvati", { riavvia: t("impostazioni.riavviaServizio") }), "ok"); loadSecrets(); }
    else secMsg(h(r.error.messaggio), "err");
  }
  $("#sec-save").onclick = () => conPulsante($("#sec-save"), async () => {
    const values = {};
    $("#sec-fields").querySelectorAll("input.sec-in").forEach(i => { if (i.value) values[i.dataset.id] = i.value; });
    if (!Object.keys(values).length) { secMsg(t("impostazioni.nessunCampo"), ""); return; }
    await putSecrets(values);
  });

  $("#pw-save").onclick = () => conPulsante($("#pw-save"), async () => {
    const p = $("#pw-new").value, m = $("#pw-msg");
    if (p.length < 6) { m.textContent = t("impostazioni.minimo6"); return; }
    // auth:false: se la sessione e' scaduta il pannello di login servirebbe la
    // password che si sta cambiando proprio qui.
    const r = await api("/api/auth/password", { method: "POST", body: { password: p }, auth: false });
    if (r.ok) { m.textContent = t("impostazioni.passwordSalvata"); $("#pw-new").value = ""; }
    else m.textContent = r.error.messaggio;
  });

  // ── Editor strutturati di sezioni config.yaml ────────────────────
  async function saveSection(name, value, setMsg) {
    const r = await api(`/api/config/section/${name}`, { method: "PUT", body: { value } });
    // Il backend dice se serve davvero un riavvio: alcune sezioni le rilegge da
    // solo, e chiedere un riavvio che non serve e' un modo per non farsi credere.
    if (r.ok) setMsg(r.data.restart_required === false
      ? t("impostazioni.giaAttivo") : t("impostazioni.salvatoRiavviaBreve"), "ok");
    else setMsg(r.error.messaggio, "err");
  }
  const rowMsg = (id, m, cls) => { const el = $("#" + id); if (el) { el.className = "muted " + (cls === "ok" ? "ok" : cls === "err" ? "err" : ""); el.textContent = m; } };

  async function loadSubnets() {
    const box = $("#ed-subnets"); if (!box) return;
    const rsubnets = await api("/api/config/section/subnets");
    if (!rsubnets.ok) { box.innerHTML = noteErrore(rsubnets.error); return; }
    let val = rsubnets.data.value || [];
    const render = () => {
      box.innerHTML = `<table><thead><tr><th>CIDR</th><th>${h(t("comune.etichetta"))}</th><th>${h(t("impostazioni.colore"))}</th><th>${h(t("impostazioni.scan"))}</th><th></th></tr></thead><tbody>${
        val.map((s, i) => `<tr>
          <td><input class="sn-in mono" data-i="${i}" data-k="cidr" value="${h(s.cidr || "")}" style="width:130px"></td>
          <td><input class="sn-in" data-i="${i}" data-k="label" value="${h(s.label || "")}" style="width:100px"></td>
          <td><input class="sn-col" data-i="${i}" type="color" value="${/^#[0-9a-fA-F]{6}$/.test(s.color || "") ? s.color : "#8b94a8"}"></td>
          <td style="text-align:center"><input type="checkbox" class="sn-ck" data-i="${i}" ${s.scan !== false ? "checked" : ""}></td>
          <td class="right"><button class="iconbtn" data-del="${i}">✕</button></td></tr>`).join("")
      }</tbody></table>
      <div class="controls" style="margin-top:8px;margin-bottom:0"><button class="btn" id="sn-add">+ subnet</button>
        <button class="btn" id="sn-save" style="border-color:var(--teal);color:var(--teal)">${h(t("impostazioni.salvaSubnet"))}</button><span id="sn-msg" class="muted"></span></div>`;
      box.querySelectorAll(".sn-in").forEach(inp => inp.oninput = () => { val[inp.dataset.i][inp.dataset.k] = inp.value; });
      box.querySelectorAll(".sn-col").forEach(inp => inp.oninput = () => { val[inp.dataset.i].color = inp.value; });
      box.querySelectorAll(".sn-ck").forEach(ck => ck.onchange = () => { val[ck.dataset.i].scan = ck.checked; });
      box.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { val.splice(+b.dataset.del, 1); render(); });
      $("#sn-add", box).onclick = () => { val.push({ cidr: "", label: "", color: "#8b94a8", scan: true }); render(); };
      $("#sn-save", box).onclick = () => saveSection("subnets", val, (m, c) => rowMsg("sn-msg", m, c));
    };
    render();
  }

  async function loadWg() {
    const box = $("#ed-wg"); if (!box) return;
    const rwg_peers = await api("/api/config/section/wg_peers");
    if (!rwg_peers.ok) { box.innerHTML = noteErrore(rwg_peers.error); return; }
    let obj = rwg_peers.data.value || {};
    let rows = Object.entries(obj || {}).map(([pub, name]) => ({ pub, name }));
    const render = () => {
      box.innerHTML = `<table><thead><tr><th>${h(t("impostazioni.chiavePubblica"))}</th><th>${h(t("comune.nome"))}</th><th></th></tr></thead><tbody>${
        rows.map((r, i) => `<tr>
          <td><input class="wg-in mono" data-i="${i}" data-k="pub" value="${h(r.pub)}" style="width:100%;min-width:220px"></td>
          <td><input class="wg-in" data-i="${i}" data-k="name" value="${h(r.name)}"></td>
          <td class="right"><button class="iconbtn" data-del="${i}">✕</button></td></tr>`).join("")
      }</tbody></table>
      <div class="controls" style="margin-top:8px;margin-bottom:0"><button class="btn" id="wg-add">+ peer</button>
        <button class="btn" id="wg-save" style="border-color:var(--teal);color:var(--teal)">${h(t("impostazioni.salvaPeer"))}</button><span id="wg-msg" class="muted"></span></div>`;
      box.querySelectorAll(".wg-in").forEach(inp => inp.oninput = () => { rows[inp.dataset.i][inp.dataset.k] = inp.value; });
      box.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { rows.splice(+b.dataset.del, 1); render(); });
      $("#wg-add", box).onclick = () => { rows.push({ pub: "", name: "" }); render(); };
      $("#wg-save", box).onclick = () => {
        const o = {}; rows.forEach(r => { if ((r.pub || "").trim()) o[r.pub.trim()] = r.name || ""; });
        saveSection("wg_peers", o, (m, c) => rowMsg("wg-msg", m, c));
      };
    };
    render();
  }

  async function loadDisc() {
    const box = $("#ed-disc"); if (!box) return;
    const rdiscovery_ssh = await api("/api/config/section/discovery_ssh");
    if (!rdiscovery_ssh.ok) { box.innerHTML = noteErrore(rdiscovery_ssh.error); return; }
    let d = rdiscovery_ssh.data.value || {};
    d.hosts = Array.isArray(d.hosts) ? d.hosts : [];
    const render = () => {
      box.innerHTML = `
        <label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="dsc-en" ${d.enabled ? "checked" : ""}> abilitata (raccoglie OS/servizi/docker dagli host via SSH)</label>
        <div class="controls" style="margin:8px 0">
          <input id="dsc-user" placeholder="${h(t("impostazioni.utenteSshDefault"))}" value="${h(d.default_user || "")}">
          <input id="dsc-key" class="mono" placeholder="${h(t("impostazioni.pathChiaveDefault"))}" value="${h(d.default_key || "")}" style="min-width:220px"></div>
        <table><thead><tr><th>${h(t("impostazioni.hostIp"))}</th><th>${h(t("comune.utente"))}</th><th>${h(t("impostazioni.chiaveOpz"))}</th><th>${h(t("impostazioni.sistema"))}</th><th></th></tr></thead><tbody>${
          d.hosts.map((hh, i) => `<tr>
            <td><input class="dsc-in mono" data-i="${i}" data-k="ip" value="${h(hh.ip || "")}"></td>
            <td><input class="dsc-in" data-i="${i}" data-k="user" value="${h(hh.user || "")}" placeholder="(default)"></td>
            <td><input class="dsc-in mono" data-i="${i}" data-k="key" value="${h(hh.key || "")}" placeholder="(default)"></td>
            <td><select class="dsc-in" data-i="${i}" data-k="os">${
              ["auto", "linux", "windows"].map(v =>
                `<option value="${v}"${(hh.os || "auto") === v ? " selected" : ""}>${v}</option>`).join("")
            }</select></td>
            <td class="right"><button class="iconbtn" data-del="${i}">✕</button></td></tr>`).join("")
        }</tbody></table>
        <div class="muted" style="font-size:12px">${h(t("impostazioni.sistemaCon"))} <span class="mono">auto</span> LANMng
          lo scopre da solo alla prima connessione. Serve dichiararlo solo se sbaglia: un host Windows
          risponde via SSH con <span class="mono">cmd.exe</span>, dove i comandi Linux non danno un
          errore ma una risposta senza senso.</div>
        <div class="controls" style="margin-top:8px;margin-bottom:0"><button class="btn" id="dsc-add">+ host</button>
          <button class="btn" id="dsc-save" style="border-color:var(--teal);color:var(--teal)">${h(t("impostazioni.salvaDiscovery"))}</button><span id="dsc-msg" class="muted"></span></div>`;
      // Anche `change`: un <select> non emette `input` su tutti i browser.
      box.querySelectorAll(".dsc-in").forEach(inp => {
        const aggiorna = () => { d.hosts[inp.dataset.i][inp.dataset.k] = inp.value; };
        inp.oninput = aggiorna; inp.onchange = aggiorna;
      });
      box.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { d.hosts.splice(+b.dataset.del, 1); render(); });
      $("#dsc-add", box).onclick = () => { d.hosts.push({ ip: "" }); render(); };
      $("#dsc-save", box).onclick = () => {
        d.enabled = $("#dsc-en", box).checked;
        d.default_user = $("#dsc-user", box).value.trim();
        d.default_key = $("#dsc-key", box).value.trim();
        // preserva eventuali campi extra (port/password mascherata) degli host esistenti
        const hosts = d.hosts.filter(x => (x.ip || "").trim()).map(x => {
          const o = { ...x, ip: x.ip.trim() };
          ["user", "key"].forEach(k => { if (!o[k] || !String(o[k]).trim()) delete o[k]; });
          // "auto" e' il default: non si scrive nel file, cosi' resta pulito.
          if (!o.os || o.os === "auto") delete o.os;
          return o;
        });
        saveSection("discovery_ssh", { ...d, hosts }, (m, c) => rowMsg("dsc-msg", m, c));
      };
    };
    render();
  }

  async function loadSilenced() {
    const box = $("#ed-alerts"); if (!box) return;
    const [rregole, rsilenced] = await Promise.all([
      api("/api/alerts/rules"), api("/api/config/section/alerts_silenced")]);
    if (!rregole.ok) { box.innerHTML = noteErrore(rregole.error); return; }
    if (!rsilenced.ok) { box.innerHTML = noteErrore(rsilenced.error); return; }
    const regole = rregole.data.rules || [];
    let val = Array.isArray(rsilenced.data.value) ? rsilenced.data.value : [];
    const opzioni = (sel) => regole.map(r =>
      `<option value="${h(r.rule)}" title="${h(r.descr)}"${r.rule === sel ? " selected" : ""}>${h(r.rule)}</option>`).join("");
    const render = () => {
      box.innerHTML = `${val.length ? `<table><thead><tr>
          <th>${h(t("alert.regola"))}</th><th>${h(t("alert.soggetto"))}</th><th>${h(t("alert.motivo"))}</th><th>${h(t("impostazioni.dal"))}</th><th></th></tr></thead><tbody>${
          val.map((x, i) => `<tr>
            <td><select class="al-in" data-i="${i}" data-k="rule">${opzioni(x.rule)}</select></td>
            <td><input class="al-in mono" data-i="${i}" data-k="subject" value="${h(x.subject || "")}" placeholder="(tutta la regola)"></td>
            <td><input class="al-in" data-i="${i}" data-k="reason" value="${h(x.reason || "")}" style="width:100%;min-width:180px"></td>
            <td class="muted nowrap">${x.since ? h(new Date(x.since * 1000).toLocaleDateString()) : "—"}</td>
            <td class="right"><button class="iconbtn" data-del="${i}">✕</button></td></tr>`).join("")
        }</tbody></table>` : `<div class="empty">${h(t("impostazioni.nessunSilenziato"))}</div>`}
        <div class="controls" style="margin-top:8px;margin-bottom:0"><button class="btn" id="al-add">+ silenziamento</button>
          <button class="btn" id="al-save" style="border-color:var(--teal);color:var(--teal)">${h(t("comune.salva"))}</button><span id="al-msg" class="muted"></span></div>`;
      box.querySelectorAll(".al-in").forEach(inp => inp.oninput = inp.onchange =
        () => { val[inp.dataset.i][inp.dataset.k] = inp.value; });
      box.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { val.splice(+b.dataset.del, 1); render(); });
      $("#al-add", box).onclick = () => {
        val.push({ rule: (regole[0] || {}).rule || "", subject: "", reason: "",
                   since: Math.floor(Date.now() / 1000) });
        render();
      };
      $("#al-save", box).onclick = () => saveSection("alerts_silenced", val,
        (m, c) => rowMsg("al-msg", m, c));
    };
    render();
  }

  editorTemi();
  editorLingua();
  load(); loadBackups(); loadSecrets(); loadSubnets(); loadWg(); loadDisc(); loadSilenced();
  caricaRiavvio();
}

/* ===================================================================
   GRAFICI (canvas)
   =================================================================== */
/* ── Colori del tema per canvas e SVG ─────────────────────────────
   Canvas e SVG non leggono le variabili CSS: i colori glieli deve passare il
   JavaScript. Si leggono da `:root` UNA volta per disegno (non per riga:
   getComputedStyle e' una lettura di stile, non una variabile) e si rileggono
   ad ogni ridisegno, cosi' cambiare tema cambia anche grafici e mappa. */
const PALETTE_DARK = {
  bg: "#0f1117", bg2: "#141823", panel: "#171b27", panel2: "#1c2130", border2: "#323a4f",
  text: "#dde3ee", muted: "#8b94a8", faint: "#5c6479",
  teal: "#4dd6e0", green: "#5bd97f", orange: "#e6a94d", red: "#e2685a",
  purple: "#9d8bf0", blue: "#5b9cf0",
  grid: "#222838", off: "#39405a", tip: "#171b27ee",
};
/* Nome usato nel disegno -> token CSS. I due elenchi hanno le stesse chiavi:
   PALETTE_DARK e' anche il ripiego quando un token manca. */
const TOKEN_TEMA = {
  bg: "--bg", bg2: "--bg-2", panel: "--panel", panel2: "--panel-2", border2: "--border-2",
  text: "--text", muted: "--muted", faint: "--faint",
  teal: "--teal", green: "--green", orange: "--orange", red: "--red",
  purple: "--purple", blue: "--blue",
  grid: "--grid", off: "--off", tip: "--tip",
};

function paletteTema() {
  const el = document.documentElement;
  // Fuori dal browser (i test girano in un contesto vm, senza layout) non
  // esiste getComputedStyle: si disegna con i valori del tema dark.
  if (!el || typeof getComputedStyle !== "function") return PALETTE_DARK;
  const css = getComputedStyle(el);
  const pal = {};
  for (const k in TOKEN_TEMA) {
    const v = (css.getPropertyValue(TOKEN_TEMA[k]) || "").trim();
    pal[k] = v || PALETTE_DARK[k];
  }
  return pal;
}

function _prep(canvas) {
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, ht = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = ht * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, ht);
  return { ctx, w, h: ht };
}
/* Scala di un asse: sceglie l'unita' in base al massimo della serie, cosi' le
   etichette restano numeri leggibili e l'unita' si scrive una volta sola. */
function axisScale(kind, max) {
  if (kind === "rate") {
    const u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"];
    let i = 0, div = 1;
    while (max / div >= 1000 && i < u.length - 1) { div *= 1000; i++; }
    return { unit: u[i], div };
  }
  if (kind === "ms") return { unit: "ms", div: 1 };
  if (kind === "pct") return { unit: "%", div: 1 };
  if (kind === "temp") return { unit: "°C", div: 1 };
  return { unit: "", div: 1 };
}
/* Massimo "tondo" (1/2/5 x 10^n): evita etichette tipo 3,7 / 7,4 / 11,1. */
function niceMax(v) {
  if (!(v > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / exp;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * exp;
}
function tickLabel(v, div) {
  const n = v / div;
  return n === 0 ? "0" : n < 10 ? n.toFixed(1) : String(Math.round(n));
}
function hhmm(ts) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
/* Etichetta dell'asse dei tempi, adatta alla larghezza della finestra: su
   sette giorni tre orari "14:30" non direbbero nulla, serve il giorno. */
function timeLabel(ts, spanMs) {
  const d = new Date(ts);
  const gg = `${d.getDate()}/${d.getMonth() + 1}`;
  if (spanMs >= 3 * 86400e3) return gg;                  // giorni: solo la data
  if (spanMs >= 86400e3) return `${gg} ${hhmm(ts)}`;      // oltre il giorno: data + ora
  return hhmm(ts);
}

/*
  Line chart con assi etichettati.
    lines: [{ get, color, axis }]      axis: "left" (default) | "right"
    opts:  { left: "rate"|"ms"|"pct"|"temp", right: idem,
             leftTop / rightTop: fondo scala fisso }
  Il fondo scala fisso serve alle percentuali: derivarlo dai dati farebbe
  sembrare un picco del 3% alto quanto uno del 90%, e due host diversi non si
  potrebbero piu' confrontare a colpo d'occhio.
  I valori null non vengono disegnati come zero: interrompono la linea (buco),
  cosi' un dato mancante non si confonde con "nessun traffico".
*/
function drawLineChart(canvas, series, lines, opts = {}) {
  // L'ultimo disegno resta appeso al canvas. Zoom e passaggio del mouse
  // ridisegnano lo stesso grafico, e i dati non li hanno sottomano: senza
  // questo servirebbe un secondo giro di render della pagina per un hover.
  if (canvas) {
    canvas._dati = { series, lines, opts };
    // Un canvas appena ricreato non sa di essere zoomato: la sua chiave si'.
    canvas._vista = vistaDi(canvas);
    bindGrafico(canvas);
  }
  const complete = series;
  series = finestraVisibile(canvas, series);
  const p = _prep(canvas); if (!p) return;
  const pal = paletteTema();
  // Le linee dichiarano un token del tema ("teal", "orange"...), non un colore:
  // in `canvas._dati` restano i token, cosi' il ridisegno dopo un cambio di
  // tema ripesca i colori nuovi invece di ridipingere quelli vecchi.
  lines = lines.map(L => ({ ...L, color: pal[L.color] || L.color }));
  const { ctx, w, h: ht } = p;
  const hasRight = lines.some(L => L.axis === "right");
  const padT = 16, padB = 18, padL = 46, padR = hasRight ? 46 : 10;
  const x0 = padL, x1 = w - padR, y0 = padT, y1 = ht - padB;
  const n = series.length;
  const font = "10px ui-monospace, monospace";

  const scaleFor = (side) => {
    const ls = lines.filter(L => (L.axis || "left") === side);
    let max = 0;
    ls.forEach(L => series.forEach(pt => {
      const v = L.get(pt); if (v != null) max = Math.max(max, v);
    }));
    const top = opts[side + "Top"] || niceMax(max);
    return { ...axisScale(opts[side] || "", top), top, lines: ls };
  };
  const left = scaleFor("left"), right = hasRight ? scaleFor("right") : null;

  // griglia (4 intervalli = 5 tacche)
  ctx.strokeStyle = pal.grid; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = y0 + (y1 - y0) * i / 4;
    ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
  }
  if (n < 2) {   // senza dati non si etichetta una scala inventata
    ctx.fillStyle = pal.faint; ctx.textAlign = "left";
    ctx.textBaseline = "middle"; ctx.font = "12px monospace";
    ctx.fillText(t("grafico.attesaDati"), x0 + 6, (y0 + y1) / 2);
    return;
  }

  // etichette degli assi
  ctx.font = font; ctx.textBaseline = "middle"; ctx.fillStyle = pal.faint;
  for (let i = 0; i <= 4; i++) {
    const y = y0 + (y1 - y0) * i / 4;
    ctx.textAlign = "right";
    ctx.fillText(tickLabel(left.top * (4 - i) / 4, left.div), x0 - 6, y);
    if (right) {
      ctx.textAlign = "left";
      ctx.fillText(tickLabel(right.top * (4 - i) / 4, right.div), x1 + 6, y);
    }
  }
  // unita' di misura, una volta sola in cima all'asse
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = pal.muted; ctx.textAlign = "left";
  ctx.fillText(left.unit, x0 - 2, y0 - 5);
  if (right) { ctx.textAlign = "right"; ctx.fillText(right.unit, x1 + 2, y0 - 5); }

  // asse dei tempi: inizio, meta', fine della finestra
  ctx.font = font; ctx.fillStyle = pal.faint; ctx.textBaseline = "top";
  const arco = (series[n - 1].t || 0) - (series[0].t || 0);
  [[0, "left"], [Math.floor((n - 1) / 2), "center"], [n - 1, "right"]].forEach(([i, align]) => {
    if (!series[i] || !series[i].t) return;
    ctx.textAlign = align;
    ctx.fillText(timeLabel(series[i].t, arco), x0 + (x1 - x0) * i / (n - 1), y1 + 5);
  });

  const xAt = (i) => x0 + (x1 - x0) * i / (n - 1);
  [left, right].filter(Boolean).forEach(ax => ax.lines.forEach(L => {
    const yAt = (v) => y1 - (y1 - y0) * v / ax.top;
    // Tratti continui: un buco nei dati spezza sia l'area sia la linea, cosi'
    // "nessun dato" resta distinguibile da "nessun traffico".
    const tratti = [];
    let corrente = null;
    series.forEach((pt, i) => {
      const v = L.get(pt);
      if (v == null) { corrente = null; return; }
      if (!corrente) tratti.push(corrente = []);
      corrente.push([xAt(i), yAt(v)]);
    });
    // Area sotto la linea, sfumata a zero verso il basso: due tratti da 2px su
    // fondo scuro si leggono solo fermandosi a guardarli, mentre una dashboard
    // si guarda di sfuggita. La sfumatura evita di annerire la griglia sotto.
    const grad = ctx.createLinearGradient(0, y0, 0, y1);
    grad.addColorStop(0, L.color + "40");
    grad.addColorStop(1, L.color + "00");
    ctx.fillStyle = grad;
    tratti.forEach(t => {
      if (t.length < 2) return;
      ctx.beginPath();
      ctx.moveTo(t[0][0], y1);
      t.forEach(([x, y]) => ctx.lineTo(x, y));
      ctx.lineTo(t[t.length - 1][0], y1);
      ctx.closePath();
      ctx.fill();
    });
    ctx.beginPath(); ctx.strokeStyle = L.color; ctx.lineWidth = 2;
    tratti.forEach(t => t.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))));
    ctx.stroke();
  }));

  disegnaPuntoLetto(ctx, canvas, { series, opts, x0, x1, y0, y1, left, right, xAt, pal });
  notaZoom(ctx, canvas, complete, series, { x1, y1, pal });
  if (canvas) aggiornaComandiGrafico(canvas);
}

/* ── Zoom, spostamento e lettura puntuale dei grafici ──────────────
   La finestra visibile si tiene in TIMESTAMP, non in indici: la serie cresce
   ad ogni ciclo di raccolta, e con gli indici l'intervallo che si sta
   guardando scivolerebbe indietro da solo mentre lo si guarda.
   Lo stato del puntatore (`_hover`, `_trascina`) vive sul canvas ed e' giusto
   che si azzeri con lui. La finestra zoomata no: sta in un registro indicizzato
   per chiave (vedi `vistaGrafici`), perche' i grafici aperti insieme sono piu'
   d'uno — sei host nella pagina Risorse — e perche' li' il canvas viene
   ricreato ad ogni raccolta. */

/* Meno di tre punti non e' un grafico: sotto questa soglia lo zoom si rifiuta
   invece di lasciare una schermata vuota che sembra un guasto. */
const ZOOM_MIN_PUNTI = 3;

/* La finestra zoomata NON puo' vivere sul nodo canvas. Nella pagina Risorse le
   schede si riscrivono ad ogni raccolta (`refreshResources`), il canvas viene
   ricreato e con lui sparirebbe lo zoom: misurato ~60 secondi di durata, senza
   che niente lo spieghi. Qui lo stato sta in un registro di modulo, indicizzato
   per una chiave che sopravvive al nodo.

   La chiave NON puo' essere l'indice della scheda: cambia quando cambia
   l'ordine degli host (gli accesi vanno in cima, 0.1.76) e lo zoom finirebbe
   sul grafico di un altro. Percio' `data-chart-key` porta l'host. */
const vistaGrafici = new Map();

function chiaveGrafico(canvas) {
  if (!canvas) return "";
  return canvas.id || (canvas.dataset && canvas.dataset.chartKey) || "";
}

function vistaDi(canvas) {
  const k = chiaveGrafico(canvas);
  // Senza chiave lo stato resta sul nodo: un grafico usa e getta continua a
  // funzionare, semplicemente non sopravvive alla propria ricreazione.
  return k ? (vistaGrafici.get(k) || null) : (canvas._vista || null);
}

function impostaVista(canvas, vista) {
  const k = chiaveGrafico(canvas);
  if (k) {
    if (vista) vistaGrafici.set(k, vista); else vistaGrafici.delete(k);
  }
  // Copia sul nodo: la usano il disegno e i test, ed e' comoda da ispezionare.
  canvas._vista = vista;
}

function arcoSerieMs(series) {
  const ts = series.map(p => p.t).filter(t => t != null);
  return ts.length >= 2 ? { t0: ts[0], t1: ts[ts.length - 1] } : null;
}

function finestraVisibile(canvas, series) {
  const v = canvas && vistaDi(canvas);
  if (!v || series.length < 2) return series;
  const dentro = series.filter(p => p.t == null || (p.t >= v.t0 && p.t <= v.t1));
  return dentro.length >= ZOOM_MIN_PUNTI ? dentro : series;
}

/* Valore con la sua unita', scelta sul valore stesso e non sul fondo scala:
   nel riquadro di lettura si vuole "12,4 Mbit/s", non "0,0" della scala in
   Gbit/s scelta per il picco della finestra. */
function fmtValoreAsse(kind, v) {
  if (v == null) return "—";
  if (kind === "rate") return fmtRate(v);
  if (kind === "ms") return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;
  if (kind === "pct") return `${v.toFixed(1)}%`;
  if (kind === "temp") return `${Math.round(v)} °C`;
  return String(Math.round(v * 100) / 100);
}

function orarioCompleto(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const due = (n) => String(n).padStart(2, "0");
  return `${due(d.getDate())}/${due(d.getMonth() + 1)} ${due(d.getHours())}:${
    due(d.getMinutes())}:${due(d.getSeconds())}`;
}

/* Il punto letto: riga verticale, un pallino per linea e il riquadro con
   l'orario esatto e i valori. Il pallino sta sul punto vero della serie, non
   sotto il cursore: leggere un valore interpolato che non esiste nei dati
   sarebbe peggio che non leggerlo affatto. */
function disegnaPuntoLetto(ctx, canvas, g) {
  const hx = canvas && canvas._hover;
  if (hx == null) return;
  const { series, opts, x0, x1, y0, y1, left, right, xAt, pal } = g;
  const n = series.length;
  if (n < 2) return;
  const f = Math.max(0, Math.min(1, (hx - x0) / (x1 - x0)));
  const i = Math.round(f * (n - 1));
  const pt = series[i];
  if (!pt) return;
  const px = xAt(i);

  ctx.strokeStyle = pal.border2; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(px, y0); ctx.lineTo(px, y1); ctx.stroke();

  const voci = [];
  [left, right].filter(Boolean).forEach(ax => {
    const kind = opts[ax === left ? "left" : "right"] || "";
    ax.lines.forEach(L => {
      const v = L.get(pt);
      voci.push({ label: L.label || "", color: L.color, testo: fmtValoreAsse(kind, v) });
      if (v == null) return;
      const py = y1 - (y1 - y0) * v / ax.top;
      ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = L.color; ctx.fill();
      ctx.strokeStyle = pal.bg; ctx.lineWidth = 1.5; ctx.stroke();
    });
  });

  riquadroLettura(ctx, { px, x0, x1, y0, y1, quando: orarioCompleto(pt.t), voci, pal });

}

function riquadroLettura(ctx, r) {
  const { px, x0, x1, y0, y1, quando, voci, pal } = r;
  ctx.font = "10px ui-monospace, monospace";
  const righe = voci.map(v => (v.label ? `${v.label}  ${v.testo}` : v.testo));
  const larghezza = Math.max(
    ctx.measureText(quando).width,
    ...righe.map(t => ctx.measureText(t).width + 12));
  const w = larghezza + 16, h = 18 + righe.length * 13;
  // Il riquadro sta dalla parte dove c'e' posto: appiccicato al bordo destro
  // uscirebbe dal canvas proprio quando si legge l'ultimo punto, che e' quello
  // che si guarda piu' spesso.
  const bx = px + 12 + w <= x1 ? px + 12 : px - 12 - w;
  const by = Math.max(y0, Math.min(y1 - h, y0 + 6));

  ctx.fillStyle = pal.tip;
  ctx.strokeStyle = pal.border2; ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(bx, by, w, h, 6); else ctx.rect(bx, by, w, h);
  ctx.fill(); ctx.stroke();

  ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillStyle = pal.muted;
  ctx.fillText(quando, bx + 8, by + 5);
  voci.forEach((v, k) => {
    const y = by + 20 + k * 13;
    ctx.fillStyle = v.color;
    ctx.fillRect(bx + 8, y + 2, 6, 6);
    ctx.fillStyle = pal.text;
    ctx.fillText(righe[k], bx + 18, y);
  });
}

/* Quando si e' zoomati va detto, con il modo di tornare indietro: un grafico
   che mostra venti minuti quando il periodo scelto ne dichiara sei ore, senza
   spiegazione, si legge come un dato mancante. */
function notaZoom(ctx, canvas, complete, visibili, g) {
  if (!canvas || !vistaDi(canvas) || visibili.length >= complete.length) return;
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "bottom";
  ctx.fillStyle = g.pal.orange;
  // In basso: in alto a destra ci sono i pulsanti, e il testo ci finirebbe
  // sotto proprio quando serve leggerlo.
  ctx.fillText(`zoom · ${visibili.length}/${complete.length} punti · trascina per spostarti`,
               g.x1, g.y1 - 3);
}

function ridisegnaGrafico(canvas) {
  const d = canvas && canvas._dati;
  if (d) drawLineChart(canvas, d.series, d.lines, d.opts);
}

function vistaCorrente(canvas) {
  const d = canvas._dati;
  return vistaDi(canvas) || (d && arcoSerieMs(d.series)) || null;
}

/* Zoom attorno a un punto: quello sotto il cursore resta fermo, cosi' si
   ingrandisce quello che si sta guardando invece del centro del grafico. */
function zoomGrafico(canvas, frazione, fattore) {
  const d = canvas._dati; if (!d) return;
  const tutto = arcoSerieMs(d.series); if (!tutto) return;
  const v = vistaCorrente(canvas); if (!v) return;
  const arco = v.t1 - v.t0;
  const perno = v.t0 + arco * Math.max(0, Math.min(1, frazione));
  let t0 = perno - (perno - v.t0) * fattore;
  let t1 = perno + (v.t1 - perno) * fattore;
  if (t1 - t0 >= tutto.t1 - tutto.t0) { impostaVista(canvas, null); ridisegnaGrafico(canvas); return; }
  // Dentro i dati che esistono: uno zoom che scorre nel vuoto mostrerebbe una
  // finestra vuota senza che nulla lo spieghi.
  if (t0 < tutto.t0) { t1 += tutto.t0 - t0; t0 = tutto.t0; }
  if (t1 > tutto.t1) { t0 -= t1 - tutto.t1; t1 = tutto.t1; }
  const prova = d.series.filter(p => p.t == null || (p.t >= t0 && p.t <= t1));
  if (prova.length < ZOOM_MIN_PUNTI) return;      // piu' stretto di cosi' non e' leggibile
  impostaVista(canvas, { t0: Math.max(t0, tutto.t0), t1: Math.min(t1, tutto.t1) });
  ridisegnaGrafico(canvas);
}

function spostaGrafico(canvas, frazione) {
  const d = canvas._dati; const v = vistaDi(canvas);
  if (!d || !v) return;
  const tutto = arcoSerieMs(d.series); if (!tutto) return;
  const arco = v.t1 - v.t0;
  let t0 = v.t0 + arco * frazione;
  let t1 = t0 + arco;
  if (t0 < tutto.t0) { t0 = tutto.t0; t1 = t0 + arco; }
  if (t1 > tutto.t1) { t1 = tutto.t1; t0 = t1 - arco; }
  impostaVista(canvas, { t0, t1 });
  ridisegnaGrafico(canvas);
}

function azzeraZoomGrafico(canvas) {
  impostaVista(canvas, null);
  ridisegnaGrafico(canvas);
}

/* Una volta sola per elemento: `drawLineChart` viene richiamata ad ogni ciclo
   di raccolta, e riagganciare i listener ogni dieci secondi ne accumulerebbe
   uno per giro finche' la pagina resta aperta (e' il difetto gia' corretto
   sulla mappa). */
/* I tre comandi si montano attorno al canvas invece che nei template: i grafici
   nascono in quattro punti diversi (dashboard, stats, latenza, una scheda per
   host nelle risorse) e un grafico aggiunto domani li avrebbe comunque. */
function montaComandiGrafico(canvas) {
  if (canvas._comandi || !canvas.parentNode || !document.createElement) return;
  const padre = canvas.parentNode;
  const wrap = document.createElement("div");
  wrap.className = "chart-wrap";
  padre.insertBefore(wrap, canvas);
  wrap.appendChild(canvas);

  const box = document.createElement("div");
  box.className = "chart-zoom";
  box.innerHTML =
    `<button class="btn" data-zoom="in" title="Ingrandisci">+</button>
     <button class="btn" data-zoom="out" title="Riduci">−</button>
     <button class="btn" data-zoom="reset" title="Torna a tutto l'intervallo">⤢</button>`;
  box.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-zoom]"); if (!b) return;
    if (b.dataset.zoom === "reset") return azzeraZoomGrafico(canvas);
    // Perno al centro: da un pulsante non c'e' un punto indicato, e il centro
    // e' quello che chi guarda si aspetta di veder restare fermo.
    zoomGrafico(canvas, 0.5, b.dataset.zoom === "in" ? 0.7 : 1 / 0.7);
  });
  wrap.appendChild(box);
  canvas._comandi = box;
}

/* Acceso quando c'e' qualcosa da azzerare, cosi' i comandi non tirano l'occhio
   su un grafico che sta gia' mostrando tutto. */
function aggiornaComandiGrafico(canvas) {
  if (canvas._comandi && canvas._comandi.classList)
    canvas._comandi.classList.toggle("attivo", !!vistaDi(canvas));
}

function bindGrafico(canvas) {
  montaComandiGrafico(canvas);
  if (canvas._zoomBound) return;
  canvas._zoomBound = true;
  const dentro = (ev) => {
    const r = canvas.getBoundingClientRect();
    const e = ev.touches && ev.touches.length ? ev.touches[0] : ev;
    return { x: e.clientX - r.left, larghezza: r.width || 1 };
  };

  canvas.addEventListener("mousemove", (ev) => {
    const p = dentro(ev);
    if (canvas._trascina != null) {
      spostaGrafico(canvas, (canvas._trascina - p.x) / p.larghezza);
      canvas._trascina = p.x;
      return;
    }
    canvas._hover = p.x;
    ridisegnaGrafico(canvas);
  });
  canvas.addEventListener("mouseleave", () => {
    canvas._hover = null; canvas._trascina = null;
    ridisegnaGrafico(canvas);
  });
  canvas.addEventListener("mousedown", (ev) => {
    if (!vistaDi(canvas)) return;           // senza zoom non c'e' dove spostarsi
    canvas._trascina = dentro(ev).x; ev.preventDefault();
  });
  canvas.addEventListener("mouseup", () => { canvas._trascina = null; });
  canvas.addEventListener("dblclick", () => azzeraZoomGrafico(canvas));
  canvas.addEventListener("wheel", (ev) => {
    // **Serve Ctrl (o Cmd)**. Nella pagina Risorse i grafici sono sei, impilati
    // in una pagina lunga: prendendosi la rotellina secca, scorrere la pagina
    // diventerebbe impossibile appena il cursore passa sopra un grafico. Con il
    // tasto premuto la rotellina e' una richiesta esplicita, e allora si ferma
    // lo scorrimento con `preventDefault` (per questo il listener non e'
    // passivo).
    if (!ev.ctrlKey && !ev.metaKey) return;
    ev.preventDefault();
    zoomGrafico(canvas, frazioneDisegnata(canvas, dentro(ev)),
                ev.deltaY < 0 ? 0.75 : 1 / 0.75);
  }, { passive: false });

  // Da telefono: un dito legge il punto, due dita zoomano.
  canvas.addEventListener("touchstart", (ev) => {
    if (ev.touches.length === 2) { canvas._pinch = distanzaTocchi(ev); return; }
    canvas._hover = dentro(ev).x; ridisegnaGrafico(canvas);
  }, { passive: true });
  canvas.addEventListener("touchmove", (ev) => {
    if (ev.touches.length === 2) {
      const d = distanzaTocchi(ev);
      if (canvas._pinch && d) {
        if (ev.cancelable) ev.preventDefault();
        zoomGrafico(canvas, 0.5, canvas._pinch / d);
        canvas._pinch = d;
      }
      return;
    }
    canvas._hover = dentro(ev).x; ridisegnaGrafico(canvas);
  }, { passive: false });
  canvas.addEventListener("touchend", (ev) => {
    if (!ev.touches.length) { canvas._pinch = null; canvas._hover = null; ridisegnaGrafico(canvas); }
  });
}

/* Dove sta il cursore rispetto all'AREA DISEGNATA, non al canvas. Gli assi
   occupano 46px a sinistra e 10 (46 con il secondo asse) a destra: prendendo la
   frazione sull'intera larghezza il perno dello zoom scivola, e su un grafico
   basso e largo si ingrandisce accanto a quello che si stava guardando. */
function frazioneDisegnata(canvas, p) {
  const d = canvas._dati;
  const destro = d && d.lines.some(L => L.axis === "right");
  const x0 = 46, x1 = p.larghezza - (destro ? 46 : 10);
  if (x1 <= x0) return 0.5;
  return Math.max(0, Math.min(1, (p.x - x0) / (x1 - x0)));
}

function distanzaTocchi(ev) {
  if (!ev.touches || ev.touches.length < 2) return 0;
  const [a, b] = [ev.touches[0], ev.touches[1]];
  return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 0;
}

/* Traffico WAN: RX/TX in bit/s a sinistra, latenza in ms a destra. Due assi
   perche' sono grandezze diverse: su una scala sola una schiaccia l'altra. */
function drawTrafficChart(canvas, series) {
  // Solo RX/TX. La latenza stava sul secondo asse: costava 36px di larghezza
  // (padR passa da 46 a 10 senza asse destro), una terza linea e due scale da
  // leggere insieme, per un valore che si legge meglio come numero — dove
  // infatti e' finito, sotto il grafico.
  drawLineChart(canvas, series, [
    { get: p => p.rx_bps, color: "teal", label: "RX" },
    { get: p => p.tx_bps, color: "green", label: "TX" },
  ], { left: "rate" });
}

/* ===================================================================
   MAPPA LAN — force-directed con nodi trascinabili (SVG)
   Simulazione: repulsione fra nodi + molla verso il router + centro.
   I nodi mantengono la posizione fra un update e l'altro.
   =================================================================== */
const SVGNS = "http://www.w3.org/2000/svg";
/* Spazio da lasciare sotto un nodo perche' il suo nome ci stia: l'etichetta e'
   disegnata a dy 27 (31 per gli hub) piu' l'altezza dei caratteri. */
const ARIA_ETICHETTA = 36;
const map = { svg: null, nodes: new Map(), links: [], raf: 0, drag: null, W: 0, H: 0,
              vista: null, pan: null, pinch: 0 };

/* Limiti dello zoom della mappa. Sotto 0,3x i nodi diventano puntini senza
   nome, sopra 6x si vede un nodo solo e ci si perde: due estremi in cui la
   mappa smette di essere una mappa. */
const MAP_ZOOM_MIN = 0.3, MAP_ZOOM_MAX = 6;

function startMap(devices) {
  const svg = $("#lanmap"); if (!svg) return;
  map.svg = svg;
  const rect = svg.getBoundingClientRect();
  const eraIntera = !map.vista || (map.vista.w >= map.W - 1 && map.vista.h >= map.H - 1);
  map.W = rect.width || 800; map.H = 520;
  // La simulazione lavora sempre nello stesso mondo `W x H`: lo zoom sposta la
  // finestra, non i nodi. Cosi' ingrandire non cambia dove le molle portano i
  // dispositivi, e tornando alla vista intera si ritrova la mappa di prima.
  //
  // Il mondo pero' cambia larghezza quando cambia la finestra del browser (o si
  // apre il drawer da telefono). Una vista intera va rifatta sulla misura nuova
  // — altrimenti resta il viewBox di prima e la mappa si vede tagliata — e una
  // zoomata va rimessa dentro i nuovi confini.
  if (eraIntera) azzeraVistaMappa();
  else { map.vista = limitaVistaMappa(map.vista); applicaVistaMappa(); }
  syncMapNodes(devices);
  buildMapDom();
  legend();
  bindMapDrag();
  bindMapZoom();
  if (!map.raf) tickMap();
}

function azzeraVistaMappa() {
  map.vista = { x: 0, y: 0, w: map.W, h: map.H };
  applicaVistaMappa();
}

function applicaVistaMappa() {
  const v = map.vista;
  if (!map.svg || !v) return;
  map.svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  const z = $("#map-zoom");
  if (z) z.classList.toggle("attivo", v.w < map.W - 1);
}

function livelloZoomMappa() {
  return map.vista ? map.W / map.vista.w : 1;
}

/* Zoom attorno a un punto del mondo: quello sotto il cursore resta fermo. Con
   il centro fisso si perde subito di vista il nodo che si stava guardando. */
function zoomMappa(fattore, perno) {
  const v = map.vista; if (!v) return;
  const liv = Math.max(MAP_ZOOM_MIN, Math.min(MAP_ZOOM_MAX, livelloZoomMappa() / fattore));
  const w = map.W / liv, h = map.H / liv;
  const px = perno ? perno.x : v.x + v.w / 2;
  const py = perno ? perno.y : v.y + v.h / 2;
  map.vista = limitaVistaMappa({
    x: px - (px - v.x) * (w / v.w),
    y: py - (py - v.y) * (h / v.h),
    w, h,
  });
  applicaVistaMappa();
}

/* La finestra resta dentro il mondo finche' ci sta: senza questo si finisce a
   guardare il vuoto fuori dalla mappa senza capire dove sono andati i nodi.
   Quando e' piu' grande del mondo (zoom sotto 1x) si centra. */
function limitaVistaMappa(v) {
  v.x = v.w >= map.W ? (map.W - v.w) / 2 : Math.max(0, Math.min(map.W - v.w, v.x));
  v.y = v.h >= map.H ? (map.H - v.h) / 2 : Math.max(0, Math.min(map.H - v.h, v.y));
  return v;
}
function stopMap() { if (map.raf) { cancelAnimationFrame(map.raf); map.raf = 0; } }

/* Click su un nodo della mappa -> espande la scheda dettagli di quel device
   nella tabella sottostante (stessa pagina) e ci scrolla. */
function openDeviceDetailFromMap(key) {
  devFilter.detailKey = key;
  renderDeviceList();
  const box = $("#dlist");
  const card = box && box.querySelector(".dev-scheda");
  if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
}

function syncMapNodes(devices) {
  const cx = map.W / 2, cy = map.H / 2;
  // nodo router al centro (fisso)
  if (!map.nodes.has("__router__"))
    map.nodes.set("__router__", { id: "__router__", router: true, x: cx, y: cy, vx: 0, vy: 0, fixed: true, type: "router", subnet: "" });
  const r = map.nodes.get("__router__"); r.x = cx; r.y = cy; r.name = routerName();

  const seen = new Set(["__router__"]);
  // Un hub per subnet: i device si agganciano al proprio hub, gli hub al router
  // (riflette la topologia multi-subnet reale).
  const subnets = [...new Set(devices.map(d => d.subnet).filter(Boolean))];
  subnets.forEach((sn, i) => {
    const id = "hub:" + sn; seen.add(id);
    let hb = map.nodes.get(id);
    if (!hb) {
      const ang = (i / Math.max(subnets.length, 1)) * Math.PI * 2;
      hb = { id, hub: true, subnet: sn, x: cx + Math.cos(ang) * 150, y: cy + Math.sin(ang) * 130, vx: 0, vy: 0 };
      map.nodes.set(id, hb);
    }
    hb.name = sn; hb.parent = "__router__";
  });
  devices.forEach((d, i) => {
    // Identita' = quella del backend (l'IP, vedi services/registry.py): con il
    // MAC due dispositivi che condividono la scheda (host e VM in bridge, o due
    // schede sullo stesso apparato) diventavano un nodo solo, e il click apriva
    // la scheda dell'altro.
    const id = d.key || (d.ips || [])[0] || d.mac || ("n" + i);
    seen.add(id);
    const parent = d.subnet && map.nodes.has("hub:" + d.subnet) ? "hub:" + d.subnet : "__router__";
    let nd = map.nodes.get(id);
    if (!nd) {
      const base = map.nodes.get(parent) || r;
      const ang = Math.random() * Math.PI * 2;
      nd = { id, x: base.x + Math.cos(ang) * 70 + Math.random() * 15, y: base.y + Math.sin(ang) * 70, vx: 0, vy: 0 };
      map.nodes.set(id, nd);
    }
    Object.assign(nd, { name: d.name, type: d.type, subnet: d.subnet, online: d.online, ips: d.ips, key: d.key, parent });
  });
  // rimuovi nodi spariti
  for (const id of [...map.nodes.keys()]) if (!seen.has(id)) map.nodes.delete(id);
  // links: ogni nodo -> il suo parent (device->hub, hub->router)
  map.links = [...map.nodes.values()].filter(n => n.parent).map(n => ({ s: map.nodes.get(n.parent), t: n, subnet: n.subnet }));
}

function buildMapDom() {
  const svg = map.svg;
  // I colori dei nodi vengono dal tema attivo; quelli delle subnet no: sono
  // dati del proprietario (config.yaml, subnets[].color) e restano i suoi.
  const pal = paletteTema();
  svg.innerHTML = "";
  const gl = document.createElementNS(SVGNS, "g");
  map.links.forEach(L => {
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("stroke", subnetColor(L.t.subnet, pal.off));
    line.setAttribute("stroke-width", "1.2"); line.setAttribute("opacity", "0.4");
    L.el = line; gl.appendChild(line);
  });
  svg.appendChild(gl);
  map.nodes.forEach(n => {
    const g = document.createElementNS(SVGNS, "g");
    g.style.cursor = n.hub ? "grab" : "pointer"; g.dataset.id = n.id;
    const rad = n.router ? 22 : (n.hub ? 17 : 13);
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("r", rad);
    c.setAttribute("fill", n.router ? pal.bg : (n.hub ? pal.bg2 : pal.panel));
    c.setAttribute("stroke", n.router ? pal.teal
      : (n.hub ? subnetColor(n.subnet, pal.muted)
        : (n.online ? subnetColor(n.subnet, pal.muted) : pal.off)));
    c.setAttribute("stroke-width", n.router ? 2.5 : (n.hub ? 2.5 : 2));
    if (n.hub) c.setAttribute("stroke-dasharray", "3 2");
    const glifo = document.createElementNS(SVGNS, "text");
    glifo.setAttribute("text-anchor", "middle"); glifo.setAttribute("dy", "0.35em");
    glifo.setAttribute("font-size", n.router ? 16 : (n.hub ? 13 : 12)); glifo.setAttribute("fill", pal.text);
    glifo.textContent = n.hub ? "◈" : (ICON[n.type] || "○");
    const lbl = document.createElementNS(SVGNS, "text");
    lbl.setAttribute("text-anchor", "middle"); lbl.setAttribute("dy", n.router ? "38" : (n.hub ? "31" : "27"));
    lbl.setAttribute("font-size", 11);
    lbl.setAttribute("fill", n.hub ? subnetColor(n.subnet, pal.muted)
      : (n.online === false ? pal.faint : pal.muted));
    lbl.setAttribute("font-family", "ui-monospace, monospace");
    lbl.textContent = (n.name || "").slice(0, 16);
    g.append(c, glifo, lbl); n.el = g; n.dot = c;
    svg.appendChild(g);
  });
}

function legend() {
  const present = new Set([...map.nodes.values()].map(n => n.subnet).filter(Boolean));
  $("#map-legend").innerHTML = [...present].map(sn =>
    `<span><i class="status-dot" style="background:${h(subnetColor(sn))}"></i>${h(sn)}</span>`).join("")
    + `<span class="muted">· clic: dettagli · trascina: i nodi, o il fondo per
       spostare la mappa · ctrl+rotellina o pizzico: zoom</span>`;
}

function tickMap() {
  const nodes = [...map.nodes.values()];
  const REP = 5200, SPRING = 0.012, LINK_LEN = 150, CENTER = 0.006, DAMP = 0.82;
  for (const a of nodes) {
    if (a.fixed || a.drag) continue;
    let fx = 0, fy = 0;
    for (const b of nodes) {
      if (a === b) continue;
      let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 0.01;
      const f = REP / d2; const d = Math.sqrt(d2);
      fx += (dx / d) * f; fy += (dy / d) * f;
    }
    // molla verso il parent (device->hub, hub->router)
    const p = map.nodes.get(a.parent) || map.nodes.get("__router__");
    const len = a.hub ? LINK_LEN : 82;
    let dx = p.x - a.x, dy = p.y - a.y, d = Math.hypot(dx, dy) || 1;
    fx += dx / d * (d - len) * SPRING * 14;
    fy += dy / d * (d - len) * SPRING * 14;
    // centro
    fx += (map.W / 2 - a.x) * CENTER; fy += (map.H / 2 - a.y) * CENTER;
    a.vx = (a.vx + fx) * DAMP; a.vy = (a.vy + fy) * DAMP;
    a.x += a.vx * 0.02; a.y += a.vy * 0.02;
    a.x = Math.max(24, Math.min(map.W - 24, a.x));
    // In basso serve piu' aria del raggio: il nome sta 27px sotto il centro del
    // nodo (31 per gli hub) e sporgeva dal riquadro — misurato sull'istanza,
    // un nome da dieci caratteri usciva di 6px e si leggeva a meta'.
    a.y = Math.max(24, Math.min(map.H - ARIA_ETICHETTA, a.y));
  }
  // render
  for (const L of map.links) if (L.el) {
    L.el.setAttribute("x1", L.s.x); L.el.setAttribute("y1", L.s.y);
    L.el.setAttribute("x2", L.t.x); L.el.setAttribute("y2", L.t.y);
  }
  for (const n of nodes) if (n.el) n.el.setAttribute("transform", `translate(${n.x},${n.y})`);
  map.raf = requestAnimationFrame(tickMap);
}

/* Da coordinate dello schermo a coordinate del mondo. Passa dalla finestra
   corrente, non da `map.W/H`: zoomati, un nodo trascinato saltava altrove
   perche' la conversione ignorava il viewBox. */
function puntoMappa(ev) {
  const r = map.svg.getBoundingClientRect();
  const e = ev.touches && ev.touches.length ? ev.touches[0] : ev;
  const v = map.vista || { x: 0, y: 0, w: map.W, h: map.H };
  return {
    x: v.x + (e.clientX - r.left) * (v.w / (r.width || 1)),
    y: v.y + (e.clientY - r.top) * (v.h / (r.height || 1)),
  };
}

function bindMapDrag() {
  const svg = map.svg;
  const pt = puntoMappa;
  const down = (ev) => {
    // Due dita: si sta pizzicando per zoomare, non trascinando.
    if (ev.touches && ev.touches.length === 2) { map.pinch = distanzaTocchi(ev); return; }
    const g = ev.target.closest("g[data-id]");
    const n = g && map.nodes.get(g.dataset.id);
    if (n && !n.router) {
      n.drag = true; n.moved = false; svg.classList.add("grabbing"); map.drag = n; ev.preventDefault();
      return;
    }
    // Sul fondo, dove non c'e' nessun dispositivo sotto il dito, si sposta la
    // vista. Il router e' fisso al centro e non si trascina: da li' si
    // sposta la mappa come dal fondo.
    map.pan = pt(ev);
    svg.classList.add("grabbing");
  };
  // Una volta sola PER ELEMENTO. Il commento qui diceva che l'svg viene
  // ricreato ad ogni render, e quando la pagina si ridisegnava tutta era vero;
  // da quando ha un refresh parziale l'elemento e' sempre lo stesso, e
  // riagganciare ogni 10 secondi accumulava un listener per ciclo finche' la
  // pagina restava aperta.
  if (map._svgBound !== svg) {
    map._svgBound = svg;
    svg.addEventListener("mousedown", down);
    svg.addEventListener("touchstart", down, { passive: false });
  }
  // Listener globali su window: registrati UNA SOLA volta. La pagina Dispositivi
  // e' live (ri-renderizzata ad ogni update): senza questa guardia si
  // accumulerebbero decine di listener. Riferiscono map.drag/map.svg live.
  if (!map._dragBound) {
    const move = (ev) => {
      if (!map.svg) return;
      if (ev.touches && ev.touches.length === 2 && map.pinch) {
        const d = distanzaTocchi(ev);
        if (d) {
          if (ev.cancelable) ev.preventDefault();
          zoomMappa(map.pinch / d, null);
          map.pinch = d;
        }
        return;
      }
      if (map.pan) {
        if (ev.cancelable) ev.preventDefault();
        spostaVistaMappa(ev);
        return;
      }
      if (!map.drag) return;
      // Da telefono il dito che trascina un nodo non deve anche far scorrere
      // la pagina: il listener e' non passivo apposta (vedi touchmove sotto).
      if (ev.cancelable) ev.preventDefault();
      map.drag.moved = true;
      const p = pt(ev); map.drag.x = p.x; map.drag.y = p.y; map.drag.vx = map.drag.vy = 0;
    };
    const up = (ev) => {
      const n = map.drag;
      map.pan = null; map.pinch = 0;
      if (n) {
        n.drag = false; map.drag = null;
        // click (senza trascinamento) su un device -> apre la scheda dettagli.
        // Un tocco annullato dal sistema non e' un clic e non deve aprire nulla.
        if (!n.moved && n.key && !(ev && ev.type === "touchcancel"))
          openDeviceDetailFromMap(n.key);
      }
      if (map.svg) map.svg.classList.remove("grabbing");
    };
    window.addEventListener("mousemove", move); window.addEventListener("touchmove", move, { passive: false });
    window.addEventListener("mouseup", up); window.addEventListener("touchend", up);
    // Un tocco interrotto dal sistema (gesto, chiamata) non manda touchend:
    // senza questo il nodo resterebbe agganciato al dito per sempre.
    window.addEventListener("touchcancel", up);
    map._dragBound = true;
  }
}

/* ── Zoom e spostamento della mappa ────────────────────────────────
   Si agisce sul `viewBox` dell'SVG, non sulle coordinate dei nodi: la
   simulazione continua a lavorare nello stesso mondo, quindi ingrandire non
   sposta niente e tornando alla vista intera si ritrova la mappa di prima.
   Trascinare un nodo resta trascinare un nodo: lo spostamento della vista
   parte solo dal fondo, dove non c'e' nessun dispositivo sotto il dito. */
function bindMapZoom() {
  const svg = map.svg;
  const zbox = $("#map-zoom");
  if (zbox && zbox._bound !== svg) {
    zbox._bound = svg;
    zbox.addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-zoom]"); if (!b) return;
      if (b.dataset.zoom === "reset") azzeraVistaMappa();
      else zoomMappa(b.dataset.zoom === "in" ? 1 / 1.4 : 1.4, null);
    });
  }
  if (map._zoomBound === svg) return;
  map._zoomBound = svg;

  svg.addEventListener("wheel", (ev) => {
    // Ctrl (o Cmd) come sui grafici: la mappa e' alta 520px dentro una pagina
    // che scorre, e prendersi la rotellina secca vorrebbe dire bloccare la
    // pagina ogni volta che il cursore ci passa sopra. Chi non vuole usare la
    // tastiera ha i tre pulsanti in alto a destra.
    if (!ev.ctrlKey && !ev.metaKey) return;
    ev.preventDefault();
    zoomMappa(ev.deltaY < 0 ? 1 / 1.2 : 1.2, puntoMappa(ev));
  }, { passive: false });

  svg.addEventListener("dblclick", (ev) => {
    if (ev.target.closest("g[data-id]")) return;   // doppio clic su un nodo: non e' un reset
    azzeraVistaMappa();
  });
}

/* Il punto afferrato deve restare sotto il dito. Da
     mondo = vista.x + schermo * (vista.w / larghezza)
   segue la nuova origine tenendo fisso quel punto: cosi' lo spostamento non
   accumula errore frame dopo frame. */
function spostaVistaMappa(ev) {
  const r = map.svg.getBoundingClientRect();
  const e = ev.touches && ev.touches.length ? ev.touches[0] : ev;
  const v = map.vista;
  map.vista = limitaVistaMappa({
    x: map.pan.x - (e.clientX - r.left) * (v.w / (r.width || 1)),
    y: map.pan.y - (e.clientY - r.top) * (v.h / (r.height || 1)),
    w: v.w, h: v.h,
  });
  applicaVistaMappa();
}

/* ===================================================================
   AVVIO
   =================================================================== */
/* ── Primo avvio guidato ────────────────────────────────────────────
   Senza config.yaml l'app non sa niente della rete: `subnets` e' vuota, quindi
   la discovery non cerca da nessuna parte e la dashboard resta vuota senza
   spiegare perche'. Questa pagina scrive la configurazione iniziale.
   Le subnet proposte vengono dalle interfacce dell'host: se non si leggono, si
   lascia il campo vuoto invece di indovinare una rete. */
/* Unico indirizzo scritto a mano nel frontend, e con un motivo: al primo avvio
   non esiste ancora una configurazione da cui ricavare un esempio, e un campo
   CIDR vuoto non dice che forma deve avere il valore. E' un esempio di
   FORMATO, non un suggerimento su una rete esistente: si sceglie la subnet
   domestica piu' comune proprio perche' sia riconoscibile a colpo d'occhio.
   Ovunque ci sia una config, l'esempio si ricava da quella (vedi esempioIP). */
const SETUP_CIDR_ESEMPIO = "192.168.1.0/24";

async function renderSetup() {
  const rs = await api("/api/setup/suggest", { auth: false, retry: false });
  const proposte = (rs.ok && rs.data.subnets) || [];
  const nota = rs.ok
    ? (proposte.length
        ? t("setup.proposta")
        : t("setup.nessunaProposta", { esempio: SETUP_CIDR_ESEMPIO }))
    : rs.error.messaggio;

  document.body.innerHTML = `<div class="login-wrap"><form class="card login" id="setup"
      style="width:460px">
    <div class="brand" style="justify-content:center;margin-bottom:6px"><span class="dot"></span> LANMng</div>
    <div class="muted" style="text-align:center;margin-bottom:14px">${h(t("setup.titolo"))}</div>
    <div class="muted" style="font-size:12px;margin-bottom:10px">${h(nota)}</div>
    <div id="sw-subnets"></div>
    <button type="button" class="btn" id="sw-add" style="margin-bottom:12px">${h(t("setup.aggiungiRete"))}</button>
    <details style="margin-bottom:12px">
      <summary class="muted" style="cursor:pointer;font-size:12px">${h(t("setup.routerFacoltativo"))}</summary>
      <div class="muted" style="font-size:12px;margin:8px 0">
        ${h(t("setup.routerSpiegazione"))}</div>
      <label>${h(t("setup.indirizzo"))}<input type="text" id="sw-rhost" class="mono" placeholder="${h(proposte.length ? proposte[0].cidr.replace(/\.\d+\/\d+$/, ".1") : "")}"></label>
      <label>${h(t("setup.utenteSsh"))}<input type="text" id="sw-ruser" value="root"></label>
    </details>
    <div id="sw-msg" class="cfg-note err" style="display:none"></div>
    <button class="btn" type="submit" style="width:100%;border-color:var(--teal);color:var(--teal)">${h(t("setup.salva"))}</button>
  </form></div>`;

  const box = $("#sw-subnets");
  const riga = (cidr = "", label = "") => {
    const d = document.createElement("div");
    d.className = "sw-riga";
    d.innerHTML = `<input type="text" class="mono sw-cidr" placeholder="${SETUP_CIDR_ESEMPIO}" value="${h(cidr)}">
      <input type="text" class="sw-label" placeholder="${h(t("setup.nome"))}" value="${h(label)}">
      <button type="button" class="btn sw-del" title="${h(t("setup.togli"))}">✕</button>`;
    $(".sw-del", d).onclick = () => d.remove();
    box.appendChild(d);
  };
  if (proposte.length) proposte.forEach(p => riga(p.cidr, p.label));
  else riga();
  $("#sw-add").onclick = () => riga();

  const msg = (testo) => { const m = $("#sw-msg"); m.textContent = testo; m.style.display = ""; };
  $("#setup").onsubmit = async (e) => {
    e.preventDefault();
    const subnets = [...document.querySelectorAll(".sw-riga")]
      .map((d, i) => ({ cidr: $(".sw-cidr", d).value.trim(),
                        label: $(".sw-label", d).value.trim() || (i === 0 ? "LAN" : `LAN ${i + 1}`),
                        scan: true }))
      .filter(s => s.cidr);
    if (!subnets.length) return msg(t("setup.serveUnaRete"));
    const rhost = $("#sw-rhost").value.trim();
    const body = { subnets };
    if (rhost) body.router = { host: rhost, user: $("#sw-ruser").value.trim() || "root" };
    const r = await api("/api/setup", { method: "POST", body, auth: false });
    if (!r.ok) return msg(r.error.messaggio);
    // La configurazione si legge all'avvio: finche' il servizio non riparte,
    // la dashboard mostrerebbe ancora zero subnet. Meglio dirlo che far
    // credere che il salvataggio non abbia funzionato.
    document.body.innerHTML = `<div class="login-wrap"><div class="card login" style="width:460px">
      <div class="brand" style="justify-content:center;margin-bottom:10px"><span class="dot"></span> LANMng</div>
      <p>${h(tp("setup.salvata", subnets.length))}</p>
      <p class="muted">${h(t("setup.riavvia"))}</p>
      <button class="btn" style="width:100%" onclick="location.reload()">${h(t("azione.ricarica"))}</button>
    </div></div>`;
  };
}

async function boot() {
  // Primo avvio: senza configurazione non c'e' niente da mostrare, e il login
  // arriverebbe prima di sapere quale rete guardare.
  const rsetup = await api("/api/setup/status", { auth: false, retry: false, timeout: 4000 });
  if (rsetup.ok && rsetup.data.setup_required) return renderSetup();

  // Gate auth: se serve il login e non siamo autenticati, mostra la pagina di login.
  // Budget corto e nessun ritentativo: con il backend spento i due tentativi
  // da 12s lascerebbero la pagina bianca per mezzo minuto prima di disegnare
  // qualsiasi cosa. auth:false perche' qui il pannello di login non serve:
  // c'e' gia' la schermata di login a tutto schermo.
  const rst = await api("/api/auth/status", { auth: false, retry: false, timeout: 4000 });
  if (rst.ok) {
    const st = rst.data;
    if (st.auth_required && !st.authenticated) return renderLogin(st);
    if (st.auth_required) { const lo = $("#logout"); if (lo) { lo.style.display = ""; lo.onclick = doLogout; } }
    renderSecurityBanner(st.insecure);
  } else {
    // Prima si proseguiva in silenzio: la dashboard si disegnava vuota e
    // nessuna pagina spiegava perche'.
    toast(rst.error.messaggio, { level: "err", durata: 0, chiave: "boot",
      azione: { label: t("azione.ricarica"), onclick: () => location.reload() } });
  }
  bindMobileNav();
  bindAlert();
  // Ricaricare o chiudere la scheda con l'editor della configurazione
  // modificato: il browser chiede conferma da solo, se glielo si dice.
  window.addEventListener("beforeunload", (e) => {
    if (!cfgSporco()) return;
    e.preventDefault();
    e.returnValue = "";
  });
  document.querySelectorAll("#nav a").forEach(a => {
    a.onclick = () => go(a.dataset.route);
    // Sono <a> senza href: senza tabindex non ricevono il fuoco e il drawer
    // non sarebbe percorribile da tastiera. Invio/Spazio valgono come clic.
    a.tabIndex = 0;
    a.onkeydown = (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      go(a.dataset.route);
    };
  });
  const initial = (location.hash.replace(/^#\/?/, "") || "dashboard");
  go(initial);
  connectWS();
  // Il conto alla rovescia della riconnessione e l'eta' del dato scorrono
  // anche quando non arriva niente: e' proprio quando non arriva niente che
  // devono dirlo.
  setInterval(aggiornaStatoConnessione, 1000);
  // primo caricamento via REST se il WS tarda
  // Primo caricamento via REST se il WebSocket tarda. Se anche questo fallisce
  // lo dice gia' il toast del boot: qui non si insiste.
  api("/api/snapshot", { retry: false }).then(r => {
    if (r.ok && !state.snap.system) { state.snap = r.data; onData(); }
  });
}

/* Avviso permanente quando il backend dichiara di rispondere senza login:
   il pentest ha trovato l'API aperta perche' nulla lo rendeva visibile. */
function renderSecurityBanner(reasons) {
  const el = $("#sec-banner");
  if (!el) return;
  if (!Array.isArray(reasons) || !reasons.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.innerHTML = `<b>${h(t("sicurezza.bannerTitolo"))}</b>
    <ul>${reasons.map(r => `<li>${h(r)}</li>`).join("")}</ul>`;
  el.hidden = false;
}

async function doLogout() {
  const r = await api("/api/auth/logout", { method: "POST", auth: false });
  // Ricaricare comunque: il cookie potrebbe essere gia' caduto, e restare
  // dentro una dashboard che si crede autenticata sarebbe peggio.
  if (!r.ok) toastErrore(r.error);
  location.reload();
}

function renderLogin(st) {
  const first = !st.password_set;
  document.body.innerHTML = `<div class="login-wrap"><form class="card login" id="login">
    <div class="brand" style="justify-content:center;margin-bottom:6px"><span class="dot"></span> LANMng</div>
    <div class="muted" style="text-align:center;margin-bottom:14px">${h(first ? t("login.primoAccesso") : t("login.accedi"))}</div>
    <label>${h(t("login.utente"))}<input type="text" id="lg-user" value="${h(st.username || "admin")}"></label>
    <label>${h(t("login.password"))}<input type="password" id="lg-pass" autocomplete="${first ? "new-password" : "current-password"}"></label>
    ${first ? `<label>${h(t("login.ripetiPassword"))}<input type="password" id="lg-pass2"
      autocomplete="new-password"></label>` : ""}
    <div id="lg-msg" class="cfg-note err" style="display:none"></div>
    <button class="btn" type="submit" style="width:100%;border-color:var(--teal);color:var(--teal);margin-top:6px">${h(first ? t("login.impostaEntra") : t("login.entra"))}</button>
  </form></div>`;
  const msg = (testo) => { const m = $("#lg-msg"); m.textContent = testo; m.style.display = ""; };
  $("#lg-pass").focus();
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    const user = $("#lg-user").value, pass = $("#lg-pass").value;
    if (first) {
      // La password si scrive due volte perche' non si vede: un refuso al
      // primo accesso chiuderebbe fuori dalla dashboard chi la sta creando, e
      // per rientrare servirebbe mettere le mani nei file sul server.
      if (pass !== $("#lg-pass2").value) return msg(t("login.nonCoincidono"));
      if (pass.length < 6) return msg(t("login.troppoCorta", { n: 6 }));
      const rp = await api("/api/auth/password", { method: "POST", body: { password: pass }, auth: false });
      if (!rp.ok) return msg(rp.error.messaggio);
    }
    const r = await api("/api/auth/login", {
      method: "POST", body: { username: user, password: pass }, auth: false,
    });
    if (r.ok) location.reload();
    else msg(r.error.messaggio);
  };
}
document.addEventListener("DOMContentLoaded", boot);
