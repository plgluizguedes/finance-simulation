"""brapi.dev: coleta por ativo (plano B da coleta em lote, ver D4 em docs/DECISIONS.md).

AVISO: o formato de resposta abaixo (historicalDataPrice, dividendsData) segue o que se conhece da API e NÃO foi
validado contra resposta real durante a construção. A documentação pública cita o endpoint /api/v2/stocks/quote;
por isso a URL base e o caminho são configuráveis. Cada requisição consome orçamento (budget_usage).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..settings import TZ, optional_env
from .base import BudgetExceeded, CorpEvent, NotAvailable, PriceBar, SourceUnavailable
from .http import HttpClient

DEFAULT_BASE = "https://brapi.dev/api"
TEST_TICKERS = {"PETR4", "VALE3", "MGLU3", "ITUB4"}  # funcionam sem token, segundo a documentação


def _day(value: Any) -> str | None:
    """A brapi devolve datas como epoch em segundos ou como texto ISO."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")
        return str(value)[:10] if value else None
    except (ValueError, OSError, OverflowError):
        return None


def parse_history(ticker: str, payload: Any) -> list[PriceBar]:
    try:
        results = payload["results"]
        rows = results[0].get("historicalDataPrice") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise SourceUnavailable("brapi devolveu um formato inesperado") from exc
    bars: list[PriceBar] = []
    for r in rows:
        day = _day(r.get("date"))
        try:
            o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue  # linha sem preço: não se inventa dado (R2)
        if not day or c <= 0:
            continue
        vol = float(r.get("volume") or 0)
        bars.append(PriceBar(ticker=ticker, pregao=day, open=o, high=h, low=lo, close=c,
                             volume_fin=vol * c, quantidade=vol, negocios=0, source="brapi"))
    return bars


def parse_events(ticker: str, payload: Any) -> list[CorpEvent]:
    try:
        data = payload["results"][0].get("dividendsData") or {}
    except (KeyError, IndexError, TypeError):
        return []
    events: list[CorpEvent] = []
    for d in data.get("cashDividends") or []:
        ex = _day(d.get("lastDatePrior")) or _day(d.get("approvedOn"))
        pay = _day(d.get("paymentDate"))
        try:
            rate = float(d.get("rate"))
        except (TypeError, ValueError):
            continue
        label = str(d.get("label") or "").upper()
        if ex:
            events.append(CorpEvent(ticker=ticker, tipo="JCP" if "JCP" in label or "JUROS" in label else "DIVIDENDO",
                                    data_ex=ex, data_com=ex, data_pagamento=pay, valor=rate, source="brapi"))
    for s in data.get("stockDividends") or []:
        ex = _day(s.get("lastDatePrior")) or _day(s.get("approvedOn"))
        label = str(s.get("label") or "").upper()
        try:
            factor = float(s.get("factor"))
        except (TypeError, ValueError):
            continue
        if not ex or factor <= 0:
            continue
        tipo = "DESDOBRAMENTO" if "DESD" in label or "SPLIT" in label else "GRUPAMENTO" if "GRUP" in label else "BONIFICACAO"
        events.append(CorpEvent(ticker=ticker, tipo=tipo, data_ex=ex, fator=factor, source="brapi"))
    return events


class BrapiProvider:
    name = "brapi"

    def __init__(
        self,
        http: HttpClient | Any | None = None,
        token: str | None = None,
        base_url: str = DEFAULT_BASE,
        on_request: Callable[[], None] | None = None,
    ):
        self.http = http or HttpClient(min_interval=1.0)
        self.token = token if token is not None else optional_env("BRAPI_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.on_request = on_request  # gasta orçamento antes de cada requisição; levanta BudgetExceeded

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self.on_request:
            self.on_request()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        return self.http.get_json(f"{self.base_url}{path}", params=params, headers=headers)

    def _check_ticker(self, ticker: str) -> None:
        if not self.token and ticker not in TEST_TICKERS:
            raise NotAvailable(f"{ticker}: sem BRAPI_TOKEN só funcionam {sorted(TEST_TICKERS)}")

    def bulk_prices(self, pregao: str) -> list[PriceBar]:
        raise NotAvailable("a brapi não tem coleta em lote; use ticker_quote")

    def ticker_quote(self, ticker: str, range_: str = "5d") -> list[PriceBar]:
        self._check_ticker(ticker)
        payload = self._get(f"/quote/{quote(ticker)}", {"range": range_, "interval": "1d", "fundamental": "false"})
        return parse_history(ticker, payload)

    def corporate_events(self, ticker: str, inicio: str, fim: str) -> list[CorpEvent]:
        self._check_ticker(ticker)
        payload = self._get(f"/quote/{quote(ticker)}", {"dividends": "true", "range": "1d", "interval": "1d"})
        return [e for e in parse_events(ticker, payload) if inicio <= e.data_ex <= fim]

    def macro_series(self, codigo: int, inicio: str | None, fim: str | None) -> list[Any]:
        raise NotAvailable("macro vem do Banco Central")

    def filings(self, emissor: str, inicio: str, fim: str) -> list[Any]:
        raise NotAvailable("documentos vêm da CVM")


__all__ = ["BrapiProvider", "BudgetExceeded", "parse_events", "parse_history"]
