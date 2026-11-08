"""Indicadores técnicos simples, funções puras sobre listas de barras (dicts com open, high, low, close, volume_fin)."""

from __future__ import annotations

from typing import Any

Bar = dict[str, Any]


def pct_change(new: float, old: float) -> float | None:
    return (new / old - 1) * 100 if old else None


def atr(bars: list[Bar], n: int) -> float | None:
    """Média da amplitude verdadeira dos últimos n pregões. Precisa de n+1 barras."""
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:], strict=True):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    return sum(trs) / n


def avg_volume(bars: list[Bar], n: int) -> float | None:
    """Volume financeiro médio dos n pregões, sem contar o último (o do dia analisado)."""
    window = bars[-n - 1:-1]
    if len(window) < n:
        return None
    return sum(b["volume_fin"] or 0 for b in window) / n


def window_return(bars: list[Bar], k: int) -> float | None:
    if len(bars) < k + 1:
        return None
    return pct_change(bars[-1]["close"], bars[-k - 1]["close"])


def highest_high(bars: list[Bar], n: int) -> float | None:
    """Maior máxima dos n pregões ANTERIORES ao último."""
    window = bars[-n - 1:-1]
    return max(b["high"] for b in window) if len(window) == n else None


def lowest_low(bars: list[Bar], n: int) -> float | None:
    window = bars[-n - 1:-1]
    return min(b["low"] for b in window) if len(window) == n else None
