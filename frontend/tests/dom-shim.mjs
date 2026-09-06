/* ===================================================================
   dom-shim.mjs — DOM minimo per far girare terminal.js fuori dal browser

   terminal.js e' uno script classico che chiude con `window.Terminal = Terminal`.
   Si carica in un contesto vm insieme a questo shim: non serve ne' jsdom ne'
   alcun pacchetto npm.

   Copre solo cio' che l'emulatore tocca davvero: createElement, appendChild,
   addEventListener, getBoundingClientRect e requestAnimationFrame. Quest'ultima
   resta un no-op di proposito: cosi' `_render()` non gira mai e le asserzioni
   guardano la griglia (`term.active`), che e' il vero oggetto sotto esame,
   invece dell'HTML prodotto.
   =================================================================== */

// Misure della cella e del riquadro scelte per dare 100x30 in fit():
//   cols = floor((816 - 16) / 8) = 100     rows = floor((522 - 12) / 17) = 30
const CELLA = { width: 8, height: 17 };
const RIQUADRO = { width: 816, height: 522 };

export function creaElemento(rect = RIQUADRO) {
  const el = {
    className: "", tabIndex: 0, textContent: "", innerHTML: "",
    style: {}, children: [],
    scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    _rect: rect,
    appendChild(figlio) { this.children.push(figlio); return figlio; },
    removeChild(figlio) {
      this.children = this.children.filter((c) => c !== figlio);
      return figlio;
    },
    remove() {},
    focus() {},
    addEventListener(tipo, cb) { (this._listener[tipo] ||= []).push(cb); },
    removeEventListener() {},
    getBoundingClientRect() {
      return { ...this._rect, top: 0, left: 0, right: this._rect.width,
               bottom: this._rect.height };
    },
  };
  el._listener = {};
  return el;
}

export function creaContesto() {
  // La prima createElement produce this.el (il riquadro), le successive
  // screenEl e probe. La sonda deve misurare la CELLA, non il riquadro.
  let creati = 0;
  const document = {
    createElement() {
      creati += 1;
      return creaElemento(creati === 3 ? CELLA : RIQUADRO);
    },
    execCommand: () => true,
  };
  const window = { getSelection: () => null, clipboardData: null };
  return {
    window, document, console,
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
  };
}

export { CELLA, RIQUADRO };
