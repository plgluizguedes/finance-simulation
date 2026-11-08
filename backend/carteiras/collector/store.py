"""Gravação dos dados coletados no banco, com prioridade entre fontes e rótulo is_mock."""

from __future__ import annotations

from collections.abc import Iterable

from ..db import Database, now_iso
from .base import CorpEvent, Filing, MacroPoint, PriceBar

# Fonte mais confiável vence quando duas fontes trazem o mesmo ticker e pregão.
SOURCE_PRIORITY = {"cotahist": 3, "b3": 3, "brapi": 2, "yfinance": 1, "mock": 0}


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 1)


def upsert_prices(db: Database, bars: Iterable[PriceBar], *, is_mock: bool = False) -> tuple[int, int]:
    """Grava preços brutos. Devolve (gravados, ignorados por haver fonte melhor)."""
    written = skipped = 0
    stamp = now_iso()
    with db.transaction():
        for bar in bars:
            current = db.one("SELECT source FROM prices_raw WHERE ticker = ? AND pregao = ?", (bar.ticker, bar.pregao))
            if current is not None and _priority(current["source"]) > _priority(bar.source):
                skipped += 1
                continue
            db.insert(
                "prices_raw",
                {
                    "ticker": bar.ticker, "pregao": bar.pregao, "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume_fin": bar.volume_fin, "quantidade": bar.quantidade,
                    "negocios": bar.negocios, "source": bar.source, "is_mock": int(is_mock), "collected_at": stamp,
                },
                or_replace=True,
            )
            written += 1
    return written, skipped


def upsert_events(db: Database, events: Iterable[CorpEvent], *, is_mock: bool = False) -> int:
    n = 0
    with db.transaction():
        for ev in events:
            cur = db.insert(
                "corporate_events",
                {
                    "ticker": ev.ticker, "tipo": ev.tipo, "data_com": ev.data_com, "data_ex": ev.data_ex,
                    "data_pagamento": ev.data_pagamento, "valor": ev.valor, "fator": ev.fator,
                    "source": ev.source, "is_mock": int(is_mock),
                },
                or_ignore=True,
            )
            n += 1 if cur else 0
    return n


def upsert_macro(db: Database, points: Iterable[MacroPoint], *, nome: str, source: str, is_mock: bool = False) -> int:
    n = 0
    with db.transaction():
        for p in points:
            db.insert(
                "macro_series",
                {"codigo": p.codigo, "nome": nome, "data": p.data, "valor": p.valor, "source": source, "is_mock": int(is_mock)},
                or_replace=True,
            )
            n += 1
    return n


def upsert_filings(db: Database, filings: Iterable[Filing], *, source: str, is_mock: bool = False) -> int:
    n = 0
    with db.transaction():
        for f in filings:
            cur = db.insert(
                "filings",
                {
                    "protocolo": f.protocolo, "cnpj": f.cnpj, "nome_emissor": f.nome_emissor, "categoria": f.categoria,
                    "assunto": f.assunto, "data_referencia": f.data_referencia, "data_entrega": f.data_entrega,
                    "link": f.link, "ticker": f.ticker, "source": source, "is_mock": int(is_mock),
                },
                or_ignore=True,
            )
            n += 1 if cur else 0
    return n


def set_source_status(
    db: Database, name: str, status: str, detalhe: str = "", dados_ate: str | None = None, *, is_mock: bool = False
) -> None:
    stamp = now_iso()
    prev = db.one("SELECT ultimo_ok, dados_ate FROM data_sources WHERE name = ?", (name,))
    ultimo_ok = stamp if status in ("ok", "mock", "plano_b") else (prev or {}).get("ultimo_ok")
    dados_ate = dados_ate or (prev or {}).get("dados_ate")
    db.insert(
        "data_sources",
        {
            "name": name, "status": status, "detalhe": detalhe, "dados_ate": dados_ate,
            "ultima_tentativa": stamp, "ultimo_ok": ultimo_ok, "is_mock": int(is_mock),
        },
        or_replace=True,
    )
