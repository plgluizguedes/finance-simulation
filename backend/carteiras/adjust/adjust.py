"""Ajuste retroativo de preços. A série bruta serve para execução e conciliação; a ajustada, para indicadores.

Convenção do campo `fator` dos eventos: multiplicador de quantidade.
  desdobramento 1:2 -> 2.0 (preço cai pela metade); grupamento 10:1 -> 0.1; bonificação de 10% -> 1.1.
Fator de preço de cada evento (aplicado a todos os pregões ANTES da data-ex):
  provento em dinheiro: (P_anterior - valor) / P_anterior;  split/grupamento/bonificação: 1 / fator.
"""

from __future__ import annotations

from typing import Any

from ..db import Database

CASH_TYPES = {"DIVIDENDO", "JCP"}
QTY_TYPES = {"DESDOBRAMENTO", "GRUPAMENTO", "BONIFICACAO"}


def price_factors(bars: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Lista (data_ex, fator de preço). Eventos que não dá para calcular (sem preço anterior) são omitidos."""
    out: list[tuple[str, float]] = []
    for ev in events:
        ex = ev["data_ex"]
        if ev["tipo"] in QTY_TYPES:
            fator = float(ev["fator"])
            if fator > 0:
                out.append((ex, 1.0 / fator))
        elif ev["tipo"] in CASH_TYPES:
            prev = [b for b in bars if b["pregao"] < ex]
            if not prev:
                continue
            p_prev = float(prev[-1]["close"])
            valor = float(ev["valor"])
            if 0 < valor < p_prev:
                out.append((ex, (p_prev - valor) / p_prev))
    return out


def build_adjusted(bars: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`bars` em ordem cronológica. Devolve as barras ajustadas, com o fator acumulado de cada pregão."""
    factors = price_factors(bars, events)
    adjusted: list[dict[str, Any]] = []
    for bar in bars:
        f = 1.0
        for ex, fe in factors:
            if ex > bar["pregao"]:
                f *= fe
        adjusted.append(
            {
                "ticker": bar["ticker"], "pregao": bar["pregao"],
                "open": bar["open"] * f, "high": bar["high"] * f, "low": bar["low"] * f, "close": bar["close"] * f,
                "volume_fin": bar["volume_fin"], "fator": f, "is_mock": bar.get("is_mock", 0),
            }
        )
    return adjusted


def unexplained_jumps(bars: list[dict[str, Any]], events: list[dict[str, Any]], pct: float) -> list[dict[str, Any]]:
    """Saltos de fechamento acima de `pct` (em %) sem evento corporativo registrado na data-ex ou no pregão vizinho."""
    ex_days = {e["data_ex"] for e in events}
    jumps: list[dict[str, Any]] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        if prev["close"] <= 0:
            continue
        change = (cur["close"] / prev["close"] - 1) * 100
        if abs(change) >= pct and not ({prev["pregao"], cur["pregao"]} & ex_days):
            jumps.append({"ticker": cur["ticker"], "pregao": cur["pregao"], "variacao_pct": change})
    return jumps


def rebuild_adjusted(db: Database, ticker: str) -> int:
    bars = db.query(
        "SELECT ticker, pregao, open, high, low, close, volume_fin, is_mock FROM prices_raw "
        "WHERE ticker = ? ORDER BY pregao", (ticker,),
    )
    events = db.query(
        "SELECT tipo, data_ex, valor, fator FROM corporate_events WHERE ticker = ? ORDER BY data_ex", (ticker,)
    )
    adjusted = build_adjusted(bars, events)
    with db.transaction():
        db.execute("DELETE FROM prices_adj WHERE ticker = ?", (ticker,))
        for row in adjusted:
            db.insert("prices_adj", row)
    return len(adjusted)


def rebuild_all(db: Database, tickers: list[str]) -> int:
    return sum(rebuild_adjusted(db, t) for t in tickers)
