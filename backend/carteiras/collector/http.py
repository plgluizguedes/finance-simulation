"""Cliente HTTP educado: um pedido por vez, intervalo mínimo, nova tentativa limitada e respeito ao Retry-After.

Regra R7: sem scraping agressivo e sem burlar bloqueios. Se a fonte recusar, levanta SourceUnavailable.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from ..settings import mask_secrets
from .base import SourceUnavailable

USER_AGENT = "carteiras-b3/0.1 (simulador pessoal; uso individual)"


class HttpClient:
    def __init__(self, min_interval: float = 1.0, retries: int = 2, timeout: float = 30.0, session: Any = None):
        self.min_interval = min_interval
        self.retries = retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _get(self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None) -> Any:
        merged = {"User-Agent": USER_AGENT, **(headers or {})}
        last_error = "sem resposta"
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, headers=merged, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}"
            else:
                status = resp.status_code
                if status == 200:
                    return resp
                if status in (401, 403, 451):
                    raise SourceUnavailable(f"acesso recusado pela fonte (HTTP {status}); não vou contornar")
                last_error = f"HTTP {status}"
                if status == 429 or status >= 500:
                    retry_after = resp.headers.get("Retry-After", "")
                    time.sleep(min(float(retry_after), 60) if retry_after.isdigit() else 2.0 * (attempt + 1))
                    continue
                raise SourceUnavailable(f"resposta inesperada da fonte ({last_error})")
        raise SourceUnavailable(mask_secrets(f"fonte indisponível após {self.retries + 1} tentativas ({last_error})"))

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        resp = self._get(url, params, headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceUnavailable("a fonte devolveu algo que não é JSON") from exc

    def get_bytes(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> bytes:
        return bytes(self._get(url, params, headers).content)
