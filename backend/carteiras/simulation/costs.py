"""Custos, slippage, tick e lote. Todos os valores vêm do perfil (nunca fixos no código)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Execucao

LOTE_PADRAO = 100


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """Arredonda ao tick. direction: 'up' (compra, pior caso), 'down' (venda, pior caso) ou 'nearest'."""
    steps = price / tick
    n = math.ceil(steps - 1e-9) if direction == "up" else math.floor(steps + 1e-9) if direction == "down" else round(steps)
    return round(n * tick, 10)


def apply_slippage(price: float, side: str, exec_cfg: Execucao) -> float:
    """Compra paga mais, venda recebe menos. Arredondado ao tick contra o investidor."""
    factor = 1 + exec_cfg.slippage_pct / 100 if side == "COMPRA" else 1 - exec_cfg.slippage_pct / 100
    return round_to_tick(price * factor, exec_cfg.tick_brl, "up" if side == "COMPRA" else "down")


def split_lots(quantity: int) -> tuple[int, int]:
    """Lote padrão (múltiplos de 100) e resto no fracionário (sufixo F)."""
    return (quantity // LOTE_PADRAO) * LOTE_PADRAO, quantity % LOTE_PADRAO


@dataclass(frozen=True)
class Costs:
    corretagem: float
    emolumentos: float
    liquidacao: float

    @property
    def total(self) -> float:
        return round(self.corretagem + self.emolumentos + self.liquidacao, 2)


def calc_costs(notional: float, exec_cfg: Execucao) -> Costs:
    return Costs(
        corretagem=exec_cfg.corretagem_brl,
        emolumentos=notional * exec_cfg.emolumentos_pct / 100,
        liquidacao=notional * exec_cfg.liquidacao_pct / 100,
    )
