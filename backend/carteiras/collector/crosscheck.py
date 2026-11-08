"""Conferência entre duas fontes de preço (critério de aceite 2 dos dados). Função pura."""

from __future__ import annotations

from .base import PriceBar


def compare(a: list[PriceBar], b: list[PriceBar], tolerance_pct: float = 0.5) -> list[dict[str, object]]:
    """Compara abertura, máxima, mínima e fechamento por (ticker, pregão). Devolve só as divergências."""
    index = {(x.ticker, x.pregao): x for x in b}
    out: list[dict[str, object]] = []
    for bar in a:
        other = index.get((bar.ticker, bar.pregao))
        if other is None:
            out.append({"ticker": bar.ticker, "pregao": bar.pregao, "campo": "ausente", "a": bar.source, "b": None})
            continue
        for campo in ("open", "high", "low", "close"):
            va, vb = getattr(bar, campo), getattr(other, campo)
            if va and abs(va - vb) / va * 100 > tolerance_pct:
                out.append({"ticker": bar.ticker, "pregao": bar.pregao, "campo": campo, "a": va, "b": vb})
    return out
