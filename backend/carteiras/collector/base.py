"""Tipos de dados e a interface DataProvider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SourceUnavailable(RuntimeError):
    """A fonte não respondeu ou bloqueou o acesso. Registrar e usar o plano B; nunca contornar (R7)."""


class NotAvailable(RuntimeError):
    """Este provedor não oferece o dado pedido."""


class BudgetExceeded(RuntimeError):
    """O orçamento de requisições acabou. O trabalho é adiado e registrado."""


@dataclass(frozen=True)
class PriceBar:
    ticker: str
    pregao: str               # AAAA-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume_fin: float
    quantidade: float
    negocios: int
    source: str


@dataclass(frozen=True)
class CorpEvent:
    ticker: str
    tipo: str                 # DIVIDENDO | JCP | DESDOBRAMENTO | GRUPAMENTO | BONIFICACAO
    data_ex: str
    source: str
    data_com: str | None = None
    data_pagamento: str | None = None
    valor: float = 0.0
    fator: float = 1.0


@dataclass(frozen=True)
class MacroPoint:
    codigo: int
    data: str
    valor: float


@dataclass(frozen=True)
class Filing:
    protocolo: str
    cnpj: str
    nome_emissor: str
    categoria: str
    assunto: str
    data_referencia: str
    data_entrega: str
    link: str
    ticker: str | None = None


class DataProvider(Protocol):
    """Interface das fontes de dados. Cada método pode levantar NotAvailable ou SourceUnavailable."""

    name: str

    def bulk_prices(self, pregao: str) -> list[PriceBar]:
        """Preços de todos os ativos do pregão (não consome cota)."""

    def ticker_quote(self, ticker: str) -> list[PriceBar]:
        """Cotação e janela recente de um ativo (consome cota)."""

    def corporate_events(self, ticker: str, inicio: str, fim: str) -> list[CorpEvent]: ...

    def macro_series(self, codigo: int, inicio: str | None, fim: str | None) -> list[MacroPoint]: ...

    def filings(self, emissor: str, inicio: str, fim: str) -> list[Filing]: ...
