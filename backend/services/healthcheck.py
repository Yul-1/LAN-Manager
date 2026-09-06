"""
services/healthcheck.py — Healthcheck HTTP / porte TCP
======================================================
Probe periodici sui servizi definiti in services.yaml (sezione http):
  - type: http  -> GET sull'URL, ok se lo status code e' fra expect_status
  - type: tcp   -> prova ad aprire host:port, ok se la connessione riesce
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from services.errors import exc_text
from services.nettools import target_blocked


log = logging.getLogger("healthcheck")


@dataclass
class CheckResult:
    name: str
    type: str            # http | tcp
    target: str          # url o host:port
    ok: bool
    latency_ms: float
    detail: str
    dashboard: bool = False   # scelto dal proprietario: fra i servizi in evidenza

    def to_dict(self) -> dict:
        return {
            "kind": "healthcheck",
            "name": self.name,
            "type": self.type,
            "target": self.target,
            "status": "up" if self.ok else "down",
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "detail": self.detail,
            "dashboard": self.dashboard,
        }


class HealthChecker:

    async def run_all(self, checks: list[dict]) -> list[CheckResult]:
        # return_exceptions: un imprevisto su una singola probe (es. una
        # risoluzione che solleva) non deve far sparire l'intera vista servizi.
        results = await asyncio.gather(*[self._run_one(c) for c in checks],
                                       return_exceptions=True)
        out: list[CheckResult] = []
        for check, res in zip(checks, results):
            if isinstance(res, BaseException):
                log.error(f"healthcheck '{check.get('name', '?')}': {res}")
                res = _result(check, ok=False, latency_ms=0.0, detail=str(res)[:80])
            out.append(res)
        return out

    async def _run_one(self, c: dict) -> CheckResult:
        t = c.get("type")
        if t == "tcp":
            return await self._tcp(c)
        if t == "ping":
            return await self._ping(c)
        return await self._http(c)

    async def _ping(self, c: dict) -> CheckResult:
        from services.ping import ping_host
        host = c.get("host", "")
        if await target_blocked(host):
            return _result(c, ok=False, latency_ms=0.0, detail="target non consentito")
        online, latency = await ping_host(host, count=1, timeout=c.get("timeout", 2))
        return _result(c, ok=online, latency_ms=latency,
                       detail="raggiungibile" if online else "nessuna risposta")

    async def _http(self, c: dict) -> CheckResult:
        url = c.get("url", "")
        expect = set(c.get("expect_status", [200]))
        timeout = c.get("timeout", 5)
        if await target_blocked(urlparse(url).hostname or ""):
            return _result(c, ok=False, latency_ms=0.0, detail="target non consentito")
        start = time.perf_counter()
        try:
            # verify=False: le probe LAN spesso colpiscono servizi con certificati
            # self-signed; la verifica del cert romperebbe il monitoraggio. Il
            # target e' comunque ristretto agli host non-speciali (vedi services/nettools.target_blocked).
            async with httpx.AsyncClient(timeout=timeout, verify=False) as cli:
                resp = await cli.get(url)
            latency = (time.perf_counter() - start) * 1000
            ok = resp.status_code in expect
            return _result(c, ok=ok, latency_ms=latency, detail=f"HTTP {resp.status_code}")
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            # 80 caratteri tagliavano il messaggio a meta' frase, proprio dove
            # sta il motivo. exc_text copre le eccezioni senza testo, che
            # altrimenti producevano un detail vuoto.
            log.debug(f"healthcheck {_target(c)}: {exc_text(e)}")
            return _result(c, ok=False, latency_ms=latency, detail=exc_text(e)[:200])

    async def _tcp(self, c: dict) -> CheckResult:
        host, port, timeout = c.get("host", ""), c.get("port", 0), c.get("timeout", 5)
        if await target_blocked(host):
            return _result(c, ok=False, latency_ms=0.0, detail="target non consentito")
        start = time.perf_counter()
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            latency = (time.perf_counter() - start) * 1000
            return _result(c, ok=True, latency_ms=latency, detail="connect ok")
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            # 80 caratteri tagliavano il messaggio a meta' frase, proprio dove
            # sta il motivo. exc_text copre le eccezioni senza testo, che
            # altrimenti producevano un detail vuoto.
            log.debug(f"healthcheck {_target(c)}: {exc_text(e)}")
            return _result(c, ok=False, latency_ms=latency, detail=exc_text(e)[:200])


def _target(c: dict) -> str:
    t = c.get("type")
    if t == "tcp":
        return f"{c.get('host','')}:{c.get('port','')}"
    if t == "ping":
        return c.get("host", "")
    return c.get("url", "")


def _result(c: dict, ok: bool, latency_ms: float, detail: str) -> CheckResult:
    return CheckResult(
        name=c.get("name", _target(c)),
        type=c.get("type", "http"),
        target=_target(c),
        ok=ok, latency_ms=latency_ms, detail=detail,
        # Assente = fuori dalla dashboard: e' come si comportava prima che la
        # scelta esistesse, cosi' un catalogo gia' scritto non cambia da solo.
        dashboard=bool(c.get("dashboard", False)),
    )


_checker: HealthChecker | None = None


def get_health_checker() -> HealthChecker:
    global _checker
    if _checker is None:
        _checker = HealthChecker()
    return _checker
