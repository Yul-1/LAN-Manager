/* ===================================================================
   terminal.js — Emulatore di terminale minimale (sottoinsieme VT100/xterm)

   Scritto a mano perche' la CSP della dashboard e' `default-src 'self'`:
   niente librerie da CDN. Copre quello che serve davvero a una shell:
   cursore, cancellazioni, colori/attributi SGR, regione di scroll, buffer
   alternativo (top/htop/vim ci entrano) e scrollback.

   Uso:
     const term = new Terminal(container, { scrollback: 1000 });
     term.onData(s => ws.send(...));      // tasti premuti
     term.write(str);                     // output dal server
     term.fit();                          // ricalcola righe/colonne
   =================================================================== */

const TERM_BASE = ["#12151c", "#e2685a", "#5bd97f", "#c9a227",
                   "#5b9cf0", "#b07de0", "#4dd6e0", "#c8cfdc"];
const TERM_BRIGHT = ["#4a5265", "#ff8a7a", "#84eda3", "#e8c14b",
                     "#84b8ff", "#cda3ff", "#7fe9f2", "#eef2f8"];

const A_BOLD = 1, A_DIM = 2, A_ITALIC = 4, A_UNDER = 8, A_INVERSE = 16;

function termColor(n) {
  if (n < 8) return TERM_BASE[n];
  if (n < 16) return TERM_BRIGHT[n - 8];
  if (n < 232) {                                  // cubo 6x6x6
    const i = n - 16, s = [0, 95, 135, 175, 215, 255];
    return `rgb(${s[Math.floor(i / 36) % 6]},${s[Math.floor(i / 6) % 6]},${s[i % 6]})`;
  }
  const g = 8 + (n - 232) * 10;                   // scala di grigi
  return `rgb(${g},${g},${g})`;
}

const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

class Terminal {
  constructor(container, opts = {}) {
    this.cols = 100; this.rows = 30;
    this.maxScrollback = opts.scrollback || 1000;
    this.dataCb = () => {};

    this.el = document.createElement("div");
    this.el.className = "term";
    this.el.tabIndex = 0;
    this.screenEl = document.createElement("pre");
    this.screenEl.className = "term-screen";
    this.el.appendChild(this.screenEl);
    container.appendChild(this.el);

    // Cella di riferimento per misurare larghezza/altezza del carattere.
    this.probe = document.createElement("span");
    this.probe.className = "term-probe";
    this.probe.textContent = "M";
    this.el.appendChild(this.probe);

    this.reset();
    this._bindInput();
    this._frame = 0;
  }

  /* ── Stato ──────────────────────────────────────────────────── */

  reset() {
    this.buffer = this._blank(this.rows);
    this.alt = null;                 // buffer alternativo (TUI a schermo intero)
    this.scrollback = [];
    this.x = 0; this.y = 0;
    this.saved = null;
    this.style = { f: null, b: null, a: 0 };
    this.scrollTop = 0; this.scrollBot = this.rows - 1;
    this.cursorVisible = true;
    this.appCursor = false;          // frecce in modalita' applicazione
    this.bracketedPaste = false;
    this.parse = { state: "text", buf: "" };
  }

  _blank(rows) {
    return Array.from({ length: rows }, () => this._blankRow());
  }
  _blankRow() {
    return Array.from({ length: this.cols }, () => ({ c: " ", f: null, b: null, a: 0 }));
  }

  onData(cb) { this.dataCb = cb; }

  /* ── Dimensioni ─────────────────────────────────────────────── */

  fit() {
    const cw = this.probe.getBoundingClientRect().width || 8;
    const ch = this.probe.getBoundingClientRect().height || 17;
    const box = this.el.getBoundingClientRect();
    const cols = Math.max(20, Math.floor((box.width - 16) / cw));
    const rows = Math.max(5, Math.floor((box.height - 12) / ch));
    if (cols === this.cols && rows === this.rows) return null;
    this.resize(cols, rows);
    return { cols, rows };
  }

  resize(cols, rows) {
    const grow = (row) => {
      while (row.length < cols) row.push({ c: " ", f: null, b: null, a: 0 });
      return row.slice(0, cols);
    };
    this.cols = cols;
    this.buffer = this.buffer.map(grow);
    while (this.buffer.length < rows) this.buffer.push(this._blankRow());
    // Rimpicciolendo si tolgono prima le righe sotto il cursore (spazio non
    // ancora usato); solo quando finiscono si sacrifica la storia in alto,
    // che pero' resta consultabile nello scrollback.
    while (this.buffer.length > rows) {
      if (this.buffer.length - 1 > this.y) this.buffer.pop();
      else { this.scrollback.push(this.buffer.shift()); this.y = Math.max(0, this.y - 1); }
    }
    if (this.scrollback.length > this.maxScrollback)
      this.scrollback.splice(0, this.scrollback.length - this.maxScrollback);
    if (this.alt) {
      this.alt = this.alt.map(grow);
      while (this.alt.length < rows) this.alt.push(this._blankRow());
      this.alt.length = rows;
    }
    this.rows = rows;
    this.scrollTop = 0; this.scrollBot = rows - 1;
    this.y = Math.min(this.y, rows - 1);
    this.x = Math.min(this.x, cols - 1);
    this._render();
  }

  get active() { return this.alt || this.buffer; }

  /* ── Parser dell'output ─────────────────────────────────────── */

  write(text) {
    for (const ch of text) this._feed(ch);
    this._schedule();
  }

  _feed(ch) {
    const p = this.parse;
    if (p.state === "esc") {
      if (ch === "[") { p.state = "csi"; p.buf = ""; return; }
      if (ch === "]") { p.state = "osc"; p.buf = ""; return; }
      if (ch === "M") { this._reverseIndex(); p.state = "text"; return; }
      if (ch === "7") { this._saveCursor(); p.state = "text"; return; }
      if (ch === "8") { this._restoreCursor(); p.state = "text"; return; }
      // set di caratteri e altre sequenze a due byte: ignorate
      p.state = (ch === "(" || ch === ")" || ch === "#" || ch === "%") ? "skip1" : "text";
      return;
    }
    if (p.state === "skip1") { p.state = "text"; return; }
    if (p.state === "csi") {
      if (ch >= "@" && ch <= "~") { this._csi(p.buf, ch); p.state = "text"; }
      // Sequenza mai chiusa: senza tetto il buffer crescerebbe all'infinito
      // (basta un `cat` di un file ostile dentro la sessione SSH).
      else if (p.buf.length > 64) { p.state = "text"; p.buf = ""; }
      else p.buf += ch;
      return;
    }
    if (p.state === "osc") {
      // Titolo finestra e simili: si consuma fino a BEL o ST, senza effetti.
      if (ch === "\x07") p.state = "text";
      else if (ch === "\x1b") p.state = "oscEsc";
      else if (p.buf.length > 1024) { p.state = "text"; p.buf = ""; }
      else p.buf += ch;
      return;
    }
    if (p.state === "oscEsc") { p.state = "text"; return; }

    switch (ch) {
      case "\x1b": p.state = "esc"; return;
      case "\r": this.x = 0; return;
      case "\n": this._newline(); return;
      case "\b": this.x = Math.max(0, this.x - 1); return;
      case "\t": this.x = Math.min(this.cols - 1, (Math.floor(this.x / 8) + 1) * 8); return;
      case "\x07": return;                          // campanello: nessun suono
      default:
        if (ch < " ") return;
        this._put(ch);
    }
  }

  _put(ch) {
    if (this.x >= this.cols) { this.x = 0; this._newline(); }
    this.active[this.y][this.x] = { c: ch, f: this.style.f, b: this.style.b, a: this.style.a };
    this.x++;
  }

  _newline() {
    if (this.y === this.scrollBot) this._scrollUp();
    else this.y = Math.min(this.y + 1, this.rows - 1);
  }

  _scrollUp() {
    const buf = this.active;
    const out = buf.splice(this.scrollTop, 1)[0];
    // Solo il buffer normale accumula scrollback: quello alternativo e' volatile.
    if (!this.alt) {
      this.scrollback.push(out);
      if (this.scrollback.length > this.maxScrollback) this.scrollback.shift();
    }
    buf.splice(this.scrollBot, 0, this._blankRow());
  }

  _reverseIndex() {
    if (this.y === this.scrollTop) {
      const buf = this.active;
      buf.splice(this.scrollBot, 1);
      buf.splice(this.scrollTop, 0, this._blankRow());
    } else this.y = Math.max(0, this.y - 1);
  }

  _saveCursor() { this.saved = { x: this.x, y: this.y, style: { ...this.style } }; }
  _restoreCursor() {
    if (!this.saved) return;
    this.x = this.saved.x; this.y = this.saved.y; this.style = { ...this.saved.style };
  }

  _csi(buf, final) {
    const priv = buf.startsWith("?");
    const nums = (priv ? buf.slice(1) : buf).split(";").map(v => (v === "" ? null : parseInt(v, 10)));
    const n = (i, d = 1) => (nums[i] == null || isNaN(nums[i]) ? d : nums[i]);
    // Le sequenze che si ripetono (inserisci/cancella righe, scroll, cancella
    // caratteri) vanno limitate allo schermo: `\x1b[999999999S` bloccherebbe la
    // pagina per secondi in splice inutili.
    const rep = (i, max) => Math.min(Math.max(n(i), 0), max);

    if (priv) {                                   // modalita' DEC
      const on = final === "h";
      for (const mode of nums) {
        if (mode === 25) this.cursorVisible = on;
        else if (mode === 1) this.appCursor = on;
        else if (mode === 2004) this.bracketedPaste = on;
        else if (mode === 1049 || mode === 47 || mode === 1047) this._altScreen(on);
      }
      return;
    }

    switch (final) {
      case "A": this.y = Math.max(this.scrollTop, this.y - n(0)); break;
      case "B": this.y = Math.min(this.scrollBot, this.y + n(0)); break;
      case "C": this.x = Math.min(this.cols - 1, this.x + n(0)); break;
      case "D": this.x = Math.max(0, this.x - n(0)); break;
      case "E": this.y = Math.min(this.rows - 1, this.y + n(0)); this.x = 0; break;
      case "F": this.y = Math.max(0, this.y - n(0)); this.x = 0; break;
      case "G": case "`": this.x = Math.min(this.cols - 1, n(0) - 1); break;
      case "d": this.y = Math.min(this.rows - 1, n(0) - 1); break;
      case "H": case "f":
        this.y = Math.min(this.rows - 1, n(0) - 1);
        this.x = Math.min(this.cols - 1, n(1) - 1);
        break;
      case "J": this._eraseDisplay(n(0, 0)); break;
      case "K": this._eraseLine(n(0, 0)); break;
      case "L": this._insertLines(rep(0, this.rows)); break;
      case "M": this._deleteLines(rep(0, this.rows)); break;
      case "P": this._deleteChars(rep(0, this.cols)); break;
      case "@": this._insertChars(rep(0, this.cols)); break;
      case "X": this._eraseChars(rep(0, this.cols)); break;
      case "S": for (let i = 0; i < rep(0, this.rows); i++) this._scrollUp(); break;
      case "T": for (let i = 0; i < rep(0, this.rows); i++) this._reverseIndex(); break;
      case "r":
        this.scrollTop = Math.max(0, n(0) - 1);
        this.scrollBot = Math.min(this.rows - 1, n(1, this.rows) - 1);
        this.x = 0; this.y = this.scrollTop;
        break;
      case "s": this._saveCursor(); break;
      case "u": this._restoreCursor(); break;
      case "m": this._sgr(nums); break;
      default: break;                              // sequenza non gestita: ignorata
    }
  }

  _altScreen(on) {
    if (on && !this.alt) { this.alt = this._blank(this.rows); this._saveCursor(); this.x = 0; this.y = 0; }
    else if (!on && this.alt) { this.alt = null; this._restoreCursor(); }
  }

  _sgr(nums) {
    if (!nums.length || (nums.length === 1 && nums[0] == null)) nums = [0];
    for (let i = 0; i < nums.length; i++) {
      const v = nums[i] == null ? 0 : nums[i];
      if (v === 0) this.style = { f: null, b: null, a: 0 };
      else if (v === 1) this.style.a |= A_BOLD;
      else if (v === 2) this.style.a |= A_DIM;
      else if (v === 3) this.style.a |= A_ITALIC;
      else if (v === 4) this.style.a |= A_UNDER;
      else if (v === 7) this.style.a |= A_INVERSE;
      else if (v === 22) this.style.a &= ~(A_BOLD | A_DIM);
      else if (v === 23) this.style.a &= ~A_ITALIC;
      else if (v === 24) this.style.a &= ~A_UNDER;
      else if (v === 27) this.style.a &= ~A_INVERSE;
      else if (v >= 30 && v <= 37) this.style.f = termColor(v - 30);
      else if (v === 39) this.style.f = null;
      else if (v >= 40 && v <= 47) this.style.b = termColor(v - 40);
      else if (v === 49) this.style.b = null;
      else if (v >= 90 && v <= 97) this.style.f = termColor(v - 90 + 8);
      else if (v >= 100 && v <= 107) this.style.b = termColor(v - 100 + 8);
      else if (v === 38 || v === 48) {
        const target = v === 38 ? "f" : "b";
        if (nums[i + 1] === 5) { this.style[target] = termColor(nums[i + 2] || 0); i += 2; }
        else if (nums[i + 1] === 2) {
          this.style[target] = `rgb(${nums[i + 2] || 0},${nums[i + 3] || 0},${nums[i + 4] || 0})`;
          i += 4;
        }
      }
    }
  }

  /* ── Cancellazioni ──────────────────────────────────────────── */

  _cell() { return { c: " ", f: null, b: this.style.b, a: 0 }; }

  _eraseLine(mode) {
    const row = this.active[this.y];
    const from = mode === 0 ? this.x : 0;
    const to = mode === 1 ? this.x + 1 : this.cols;
    for (let i = from; i < to; i++) row[i] = this._cell();
  }

  _eraseDisplay(mode) {
    if (mode === 2 || mode === 3) {
      this.active.forEach((_, i) => { this.active[i] = this._blankRow(); });
      return;
    }
    this._eraseLine(mode);
    const from = mode === 0 ? this.y + 1 : 0;
    const to = mode === 0 ? this.rows : this.y;
    for (let i = from; i < to; i++) this.active[i] = this._blankRow();
  }

  _insertLines(n) {
    for (let i = 0; i < n; i++) {
      this.active.splice(this.scrollBot, 1);
      this.active.splice(this.y, 0, this._blankRow());
    }
  }
  _deleteLines(n) {
    for (let i = 0; i < n; i++) {
      this.active.splice(this.y, 1);
      this.active.splice(this.scrollBot, 0, this._blankRow());
    }
  }
  _deleteChars(n) {
    const row = this.active[this.y];
    row.splice(this.x, n);
    while (row.length < this.cols) row.push(this._cell());
  }
  _insertChars(n) {
    const row = this.active[this.y];
    for (let i = 0; i < n; i++) row.splice(this.x, 0, this._cell());
    row.length = this.cols;
  }
  _eraseChars(n) {
    const row = this.active[this.y];
    for (let i = this.x; i < Math.min(this.x + n, this.cols); i++) row[i] = this._cell();
  }

  /* ── Rendering ──────────────────────────────────────────────── */

  _schedule() {
    if (this._frame) return;
    this._frame = requestAnimationFrame(() => { this._frame = 0; this._render(); });
  }

  _render() {
    const atBottom = this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight < 24;
    const rows = this.alt ? [] : this.scrollback;
    const html = rows.map(r => this._rowHtml(r, -1)).join("\n")
      + (rows.length ? "\n" : "")
      + this.active.map((r, i) => this._rowHtml(r, i === this.y ? this.x : -1)).join("\n");
    this.screenEl.innerHTML = html;
    if (atBottom) this.el.scrollTop = this.el.scrollHeight;
  }

  _rowHtml(row, cursorX) {
    let html = "", run = "", style = null;
    const flush = () => {
      if (!run) return;
      html += style ? `<span style="${style}">${escapeHtml(run)}</span>` : escapeHtml(run);
      run = "";
    };
    // Ultima colonna piena: si evita di disegnare gli spazi di coda.
    let last = row.length - 1;
    while (last >= 0 && row[last].c === " " && !row[last].b && last !== cursorX) last--;

    for (let i = 0; i <= Math.max(last, cursorX); i++) {
      const cell = row[i] || { c: " ", f: null, b: null, a: 0 };
      const cs = this._cellStyle(cell, i === cursorX);
      if (cs !== style) { flush(); style = cs; }
      run += cell.c;
    }
    flush();
    return html || " ";
  }

  _cellStyle(cell, isCursor) {
    let fg = cell.f, bg = cell.b;
    if (cell.a & A_INVERSE) { const t = fg; fg = bg || "#0d1017"; bg = t || "#c8cfdc"; }
    if (isCursor && this.cursorVisible) { const t = fg; fg = bg || "#0d1017"; bg = t || "#4dd6e0"; }
    const parts = [];
    if (fg) parts.push(`color:${fg}`);
    if (bg) parts.push(`background:${bg}`);
    if (cell.a & A_BOLD) parts.push("font-weight:700");
    if (cell.a & A_DIM) parts.push("opacity:.65");
    if (cell.a & A_ITALIC) parts.push("font-style:italic");
    if (cell.a & A_UNDER) parts.push("text-decoration:underline");
    return parts.length ? parts.join(";") : null;
  }

  /* ── Input da tastiera ──────────────────────────────────────── */

  _bindInput() {
    this.el.addEventListener("keydown", (e) => {
      const seq = this._keySeq(e);
      if (seq == null) return;      // gestito dal browser (copia, incolla, ...)
      e.preventDefault();
      if (seq) this.dataCb(seq);
    });
    this.el.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text");
      if (!text) return;
      this.dataCb(this.bracketedPaste ? `\x1b[200~${text}\x1b[201~` : text);
    });
    this.el.addEventListener("mousedown", () => this.el.focus());
  }

  /* Selezione attiva dentro il terminale: serve a decidere se Ctrl+C deve
     copiare (come si aspetta chi arriva dal browser) o interrompere il
     comando (come si aspetta chi arriva dalla shell). */
  _hasSelection() {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    return this.el.contains(sel.getRangeAt(0).commonAncestorContainer);
  }

  _copySelection() {
    if (!this._hasSelection()) return false;
    // navigator.clipboard non esiste su http://: execCommand e' deprecato ma
    // e' l'unico che funziona fuori dai contesti sicuri, ed e' il nostro caso.
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    }
  }

  /* Ritorna: stringa = da inviare alla shell · "" = tasto gestito, non inviare
     nulla · null = lascia fare al browser (copia, incolla, sue scorciatoie). */
  _keySeq(e) {
    if (e.metaKey) return null;                    // scorciatoie del browser (macOS)

    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (e.ctrlKey && e.shiftKey && key === "c") {
      // Ctrl+Shift+C: copia esplicita. Va intercettato, altrimenti il browser
      // aprirebbe gli strumenti di sviluppo.
      this._copySelection();
      return "";
    }
    // Ctrl+Shift+V e Ctrl+V: l'incolla lo fa il browser e arriva dall'evento
    // "paste"; intercettarli lo impedirebbe del tutto.
    if (e.ctrlKey && key === "v") return null;
    // Ctrl+C: con del testo selezionato copia (comportamento da browser),
    // senza selezione interrompe il comando (comportamento da shell).
    if (e.ctrlKey && !e.shiftKey && key === "c" && this._hasSelection()) return null;
    const cur = (letter) => (this.appCursor ? `\x1bO${letter}` : `\x1b[${letter}`);
    const named = {
      ArrowUp: cur("A"), ArrowDown: cur("B"), ArrowRight: cur("C"), ArrowLeft: cur("D"),
      Home: cur("H"), End: cur("F"), Enter: "\r", Tab: "\t", Backspace: "\x7f",
      Escape: "\x1b", Delete: "\x1b[3~", Insert: "\x1b[2~",
      PageUp: "\x1b[5~", PageDown: "\x1b[6~",
      F1: "\x1bOP", F2: "\x1bOQ", F3: "\x1bOR", F4: "\x1bOS",
      F5: "\x1b[15~", F6: "\x1b[17~", F7: "\x1b[18~", F8: "\x1b[19~",
      F9: "\x1b[20~", F10: "\x1b[21~", F11: "\x1b[23~", F12: "\x1b[24~",
    };
    if (named[e.key] !== undefined) return named[e.key];
    if (e.ctrlKey && e.key.length === 1) {
      // Ctrl+C/D/Z e compagnia: codice di controllo corrispondente.
      const code = e.key.toUpperCase().charCodeAt(0);
      if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);
      if (e.key === " ") return "\x00";
      return null;                                  // resto delle combinazioni: al browser
    }
    if (e.altKey && e.key.length === 1) return "\x1b" + e.key;
    if (e.key.length === 1) return e.key;
    return null;
  }

  focus() { this.el.focus(); }

  dispose() {
    if (this._frame) cancelAnimationFrame(this._frame);
    this.el.remove();
  }
}

window.Terminal = Terminal;
