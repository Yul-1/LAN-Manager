"""
services/ratelimit.py — Rate limiter in-memory (senza dipendenze)
=================================================================
Finestra scorrevole per chiave (es. IP). Uso: proteggere il login da
brute-force e i trigger di scan da abusi. Adeguato a un servizio mono-utente
LAN; non e' un limiter distribuito.
"""
from __future__ import annotations

import math
import time


class RateLimiter:
    def __init__(self, max_hits: int, window_seconds: float):
        self.max = max_hits
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return hits

    def allowed(self, key: str) -> bool:
        """True se la chiave non ha ancora saturato la finestra."""
        return len(self._recent(key, time.monotonic())) < self.max

    def hit(self, key: str) -> None:
        """Registra un evento per la chiave."""
        now = time.monotonic()
        self._recent(key, now).append(now)

    def retry_after(self, key: str) -> int:
        """Secondi da attendere prima che la finestra liberi uno slot.

        Serve per l'header `Retry-After`: senza, il client puo' solo dire
        "troppe richieste" e l'utente ritenta a caso."""
        now = time.monotonic()
        hits = self._recent(key, now)
        if len(hits) < self.max:
            return 0
        return max(1, math.ceil(self.window - (now - min(hits))))

    def refund(self, key: str) -> None:
        """Cancella l'ultimo evento registrato per la chiave.

        Serve quando la richiesta e' stata rifiutata prima di fare alcunche':
        un bersaglio non valido non ha toccato la rete, e non deve consumare
        uno slot pensato per limitare il traffico vero. Senza, dieci clic con
        il campo vuoto bloccavano gli strumenti per un minuto."""
        hits = self._hits.get(key)
        if hits:
            hits.pop()

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
