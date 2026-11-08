"""Estado de uma carteira lido do banco: caixa, posições, valorização e exposição."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Universe
from ..db import Database


@dataclass
class Position:
    ticker: str
    quantidade: int
    custo_medio: float
    stop: float | None
    alvo: float | None
    aberta_em: str


@dataclass
class PortfolioState:
    profile_id: str
    caixa: float
    posicoes: dict[str, Position] = field(default_factory=dict)

    def valor_posicoes(self, prices: dict[str, float]) -> float:
        return sum(p.quantidade * prices.get(p.ticker, p.custo_medio) for p in self.posicoes.values())

    def patrimonio(self, prices: dict[str, float]) -> float:
        return self.caixa + self.valor_posicoes(prices)


def load_state(db: Database, profile_id: str) -> PortfolioState:
    caixa = float(db.scalar("SELECT COALESCE(SUM(valor), 0) FROM cash_ledger WHERE profile_id = ?", (profile_id,), 0.0))
    rows = db.query("SELECT * FROM positions WHERE profile_id = ? AND quantidade > 0", (profile_id,))
    positions = {
        r["ticker"]: Position(r["ticker"], int(r["quantidade"]), float(r["custo_medio"]), r["stop"], r["alvo"], r["aberta_em"])
        for r in rows
    }
    return PortfolioState(profile_id=profile_id, caixa=caixa, posicoes=positions)


def last_prices(db: Database, tickers: list[str], pregao: str) -> dict[str, float]:
    """Último fechamento bruto conhecido até o pregão (inclusive) de cada ticker. Sem dado, o ticker não aparece."""
    out: dict[str, float] = {}
    for t in tickers:
        row = db.one(
            "SELECT close FROM prices_raw WHERE ticker = ? AND pregao <= ? ORDER BY pregao DESC LIMIT 1", (t, pregao)
        )
        if row and row["close"]:
            out[t] = float(row["close"])
    return out


def exposure(state: PortfolioState, prices: dict[str, float], universe: Universe) -> dict[str, dict[str, float]]:
    """Peso de cada ativo e de cada setor sobre o patrimônio (em %)."""
    equity = state.patrimonio(prices)
    by_ticker: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    if equity <= 0:
        return {"ativo": by_ticker, "setor": by_sector}
    for p in state.posicoes.values():
        weight = p.quantidade * prices.get(p.ticker, p.custo_medio) / equity * 100
        by_ticker[p.ticker] = weight
        sector = universe.setor(p.ticker)
        by_sector[sector] = by_sector.get(sector, 0.0) + weight
    return {"ativo": by_ticker, "setor": by_sector}
