"""Valida qualquer ordem simulada contra os limites do perfil, venha ela de onde vier (manual, mock, agente, sistema).

O resultado é VETADA (nada executa), REDUZIDA (quantidade menor) ou OK. Cada ajuste devolve um motivo para
registro em rule_events. Os limites são lidos do perfil; este módulo não conhece nenhum número.

Ordens do tipo CONSTRUCAO_INICIAL ficam isentas de giro mensal e de posições mínimas (senão a carteira nunca seria montada).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Profile, Universe
from ..simulation.costs import Costs
from ..simulation.state import PortfolioState

OK, REDUZIDA, VETADA = "OK", "REDUZIDA", "VETADA"


@dataclass
class RuleContext:
    pregao: str
    is_trading_day: bool
    prices: dict[str, float]              # preço de referência por ticker (fechamento anterior ou preço de execução)
    avg_volume_20d: dict[str, float]      # volume financeiro médio de 20 dias por ticker; ausente = sem dado
    turnover_month_brl: float             # compras já feitas no mês corrente, sem a construção inicial
    somente_reduzir: bool
    exec_price: float | None = None       # preço de execução, quando já conhecido (inclui slippage)
    cost_rate: float = 0.0                # custo proporcional (emolumentos + liquidação) em fração
    cost_fixed: float = 0.0               # corretagem fixa por ordem


@dataclass
class Violation:
    regra: str
    resultado: str
    motivo: str
    detalhe: dict[str, float | str] = field(default_factory=dict)


@dataclass
class RuleResult:
    status: str
    quantidade: int
    violacoes: list[Violation] = field(default_factory=list)

    @property
    def motivo(self) -> str:
        return "; ".join(v.motivo for v in self.violacoes)


def cost_context(profile: Profile) -> tuple[float, float]:
    e = profile.execucao
    return (e.emolumentos_pct + e.liquidacao_pct) / 100, e.corretagem_brl


def check_order(
    profile: Profile,
    universe: Universe,
    state: PortfolioState,
    ctx: RuleContext,
    *,
    ticker: str,
    side: str,
    quantity: int,
    tipo_ordem: str = "NORMAL",
) -> RuleResult:
    lim = profile.limites
    violations: list[Violation] = []

    def veto(regra: str, motivo: str, **detalhe: float | str) -> RuleResult:
        violations.append(Violation(regra, VETADA, motivo, dict(detalhe)))
        return RuleResult(VETADA, 0, violations)

    if quantity <= 0:
        return veto("quantidade", "quantidade zero ou negativa")
    if not ctx.is_trading_day:
        return veto("pregao", f"{ctx.pregao} não é dia de pregão")

    held = state.posicoes.get(ticker)

    # ---- VENDA: sem venda a descoberto; reduzir risco sempre é permitido -----------------
    if side == "VENDA":
        have = held.quantidade if held else 0
        if have <= 0:
            return veto("sem_venda_a_descoberto", f"não há posição em {ticker} para vender")
        if quantity > have:
            violations.append(Violation("sem_venda_a_descoberto", REDUZIDA,
                                        f"venda reduzida de {quantity} para {have} (tamanho da posição)",
                                        {"pedido": quantity, "posicao": have}))
            return RuleResult(REDUZIDA, have, violations)
        return RuleResult(OK, quantity, violations)

    # ---- COMPRA ---------------------------------------------------------------------------
    if ctx.somente_reduzir:
        return veto("drawdown_maximo", "carteira em modo somente reduzir (drawdown máximo atingido)")
    if ticker not in profile.universo.tickers:
        return veto("universo", f"{ticker} está fora do universo do perfil {profile.id}")
    minimo = profile.universo.liquidez_minima.volume_financeiro_medio_20d
    avg = ctx.avg_volume_20d.get(ticker)
    if avg is None:
        return veto("liquidez", f"sem dado de liquidez de {ticker} (volume médio de 20 dias indisponível)")
    if avg < minimo:
        return veto("liquidez", f"liquidez de {ticker} abaixo do mínimo do perfil", media=avg, minimo=minimo)

    price = ctx.exec_price or ctx.prices.get(ticker)
    if not price or price <= 0:
        return veto("preco", f"sem preço de referência para {ticker}")

    equity = state.patrimonio(ctx.prices)
    if equity <= 0:
        return veto("patrimonio", "patrimônio zerado")

    qty = quantity
    initial = tipo_ordem == "CONSTRUCAO_INICIAL"

    if held is None and len(state.posicoes) >= lim.posicoes_max:
        return veto("posicoes_max", f"já há {len(state.posicoes)} posições (máximo {lim.posicoes_max})")

    def reduce(regra: str, new_qty: int, motivo: str, **detalhe: float | str) -> bool:
        nonlocal qty
        if new_qty < qty:
            violations.append(Violation(regra, REDUZIDA, motivo, dict(detalhe)))
            qty = max(new_qty, 0)
        return qty > 0

    # máximo por posição
    current_value = (held.quantidade if held else 0) * ctx.prices.get(ticker, price)
    room = equity * lim.max_por_posicao_pct / 100 - current_value
    if not reduce("max_por_posicao", math.floor(max(room, 0) / price),
                  f"compra de {ticker} reduzida pelo máximo por posição ({lim.max_por_posicao_pct:g}%)",
                  limite_pct=lim.max_por_posicao_pct):
        return veto("max_por_posicao", f"{ticker} já está no máximo por posição ({lim.max_por_posicao_pct:g}%)")

    # máximo por setor
    sector = universe.setor(ticker)
    sector_value = sum(
        p.quantidade * ctx.prices.get(p.ticker, p.custo_medio)
        for p in state.posicoes.values() if universe.setor(p.ticker) == sector
    )
    room = equity * lim.max_por_setor_pct / 100 - sector_value
    if not reduce("max_por_setor", math.floor(max(room, 0) / price),
                  f"compra de {ticker} reduzida pelo máximo do setor {sector} ({lim.max_por_setor_pct:g}%)",
                  setor=sector, limite_pct=lim.max_por_setor_pct):
        return veto("max_por_setor", f"setor {sector} já está no máximo ({lim.max_por_setor_pct:g}%)")

    # caixa mínimo (considera os custos da própria compra)
    spendable = state.caixa - equity * lim.caixa_minimo_pct / 100 - ctx.cost_fixed
    per_share = price * (1 + ctx.cost_rate)
    if not reduce("caixa_minimo", math.floor(max(spendable, 0) / per_share),
                  f"compra de {ticker} reduzida para preservar o caixa mínimo ({lim.caixa_minimo_pct:g}%)",
                  limite_pct=lim.caixa_minimo_pct):
        return veto("caixa_minimo", f"sem caixa disponível acima do mínimo de {lim.caixa_minimo_pct:g}%")

    # giro mensal
    if not initial:
        room = equity * lim.giro_max_mensal_pct / 100 - ctx.turnover_month_brl
        if not reduce("giro_max_mensal", math.floor(max(room, 0) / price),
                      f"compra de {ticker} reduzida pelo giro máximo mensal ({lim.giro_max_mensal_pct:g}%)",
                      limite_pct=lim.giro_max_mensal_pct):
            return veto("giro_max_mensal", f"giro mensal já atingiu {lim.giro_max_mensal_pct:g}% do patrimônio")

    status = REDUZIDA if qty < quantity else OK
    return RuleResult(status, qty, violations)


def costs_for(profile: Profile, notional: float) -> Costs:
    from ..simulation.costs import calc_costs

    return calc_costs(notional, profile.execucao)
