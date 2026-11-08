"""BrokerSim: a corretora virtual.

Regras (R3): decisão com dados até D só executa em D+1 (abertura ou fechamento, conforme o perfil); custos e slippage
do perfil; stops no pior caso com OHLC diário; proventos e eventos corporativos na data correta.

Ordem das etapas de um pregão (chamadas pelo agendador):
  apply_corporate_events -> execute -> stops -> credit_accruals -> mark_to_market
Os eventos corporativos vêm ANTES das ordens e dos stops de propósito: na data-ex o preço bruto já caiu
(desdobramento, dividendo), e avaliar o stop antes do ajuste da posição geraria uma saída falsa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..calendar_b3 import Calendar
from ..config import Config, Profile
from ..db import Database, dumps, now_iso
from ..eventlog import log_event
from ..rules.engine import REDUZIDA, VETADA, RuleContext, RuleResult, Violation, check_order, cost_context
from .costs import apply_slippage, calc_costs, round_to_tick, split_lots
from .state import PortfolioState, last_prices, load_state


@dataclass
class OrderRequest:
    profile_id: str
    ticker: str
    side: str                              # COMPRA | VENDA
    dimensionamento: str                   # quantidade | peso_alvo_pct | fracao_posicao
    pregao_decisao: str
    origem: str                            # manual | mock | agente | sistema
    motivo: str = ""
    quantidade: int | None = None
    peso_alvo_pct: float | None = None
    fracao_posicao: float | None = None
    stop_pct: float | None = None
    alvo_pct: float | None = None
    decision_id: int | None = None
    tipo_ordem: str = "NORMAL"             # NORMAL | CONSTRUCAO_INICIAL | STOP | ALVO | GATILHO
    idem_key: str | None = None


class BrokerSim:
    def __init__(self, db: Database, cfg: Config, cal: Calendar, *, is_mock: bool = False):
        self.db = db
        self.cfg = cfg
        self.cal = cal
        self.is_mock = int(is_mock)

    # ------------------------------------------------------------------ utilidades
    def _bar(self, ticker: str, pregao: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM prices_raw WHERE ticker = ? AND pregao = ?", (ticker, pregao))

    def _prev(self, pregao: str) -> str:
        return self.cal.prev_day(date.fromisoformat(pregao)).isoformat()

    def _avg_volume(self, ticker: str, before: str, window: int) -> float | None:
        rows = self.db.query(
            "SELECT volume_fin FROM prices_raw WHERE ticker = ? AND pregao < ? ORDER BY pregao DESC LIMIT ?",
            (ticker, before, window),
        )
        if len(rows) < window:
            return None
        return sum(r["volume_fin"] or 0 for r in rows) / window

    def _turnover_month(self, profile_id: str, pregao: str) -> float:
        start = pregao[:8] + "01"
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(valor), 0) FROM fills WHERE profile_id = ? AND lado = 'COMPRA' AND pregao >= ? "
            "AND pregao <= ? AND COALESCE(tipo, '') != 'CONSTRUCAO_INICIAL'", (profile_id, start, pregao), 0.0))

    def _somente_reduzir(self, profile_id: str) -> bool:
        return bool(self.db.scalar("SELECT somente_reduzir FROM profile_state WHERE profile_id = ?", (profile_id,), 0))

    def _record_violations(self, profile_id: str, order_id: int | None, violations: list[Violation], pregao: str) -> None:
        for v in violations:
            self.db.insert("rule_events", {
                "profile_id": profile_id, "order_id": order_id, "regra": v.regra, "resultado": v.resultado,
                "motivo": v.motivo, "detalhe_json": dumps(v.detalhe), "pregao": pregao, "is_mock": self.is_mock,
                "created_at": now_iso(),
            })

    # ------------------------------------------------------------------ início da carteira
    def start_profile(self, profile_id: str, pregao: str) -> None:
        profile = self.cfg.profiles[profile_id]
        with self.db.transaction():
            if self.db.scalar("SELECT iniciado_em FROM profile_state WHERE profile_id = ?", (profile_id,)):
                return
            self.db.insert("profiles", {
                "profile_id": profile_id, "version_hash": profile.version_hash(),
                "status_parametros": profile.status_parametros,
                "config_json": dumps(profile.model_dump()), "created_at": now_iso(),
            }, or_ignore=True)
            self.db.insert("profile_state", {
                "profile_id": profile_id, "pausado": 0, "iniciado_em": pregao, "ultimo_pregao": None,
                "somente_reduzir": 0, "pico_patrimonio": profile.capital_inicial,
                "capital_inicial": profile.capital_inicial, "atualizado_em": now_iso(),
            }, or_replace=True)
            self.db.insert("cash_ledger", {
                "profile_id": profile_id, "pregao": pregao, "tipo": "APORTE", "valor": profile.capital_inicial,
                "ref_json": dumps({"motivo": "capital virtual inicial"}), "is_mock": self.is_mock,
            })
            self.db.insert("portfolio_history", {
                "profile_id": profile_id, "pregao": pregao, "patrimonio": profile.capital_inicial,
                "caixa": profile.capital_inicial, "posicoes_valor": 0.0, "retorno_dia": 0.0,
                "pico": profile.capital_inicial, "drawdown": 0.0, "defasados": 0, "is_mock": self.is_mock,
            }, or_replace=True)
            log_event(self.db, origem="sistema", tipo="CARTEIRA_INICIADA", profile_id=profile_id, pregao=pregao,
                      motivo=f"capital virtual de R$ {profile.capital_inicial:,.2f}", is_mock=bool(self.is_mock),
                      ref={"perfil_versao": profile.version_hash()})

    # ------------------------------------------------------------------ agendamento (R4 na entrada)
    def schedule(self, req: OrderRequest) -> int | None:
        """Valida a ordem contra o perfil e a agenda para o pregão seguinte. Ordem vetada fica registrada como VETADA."""
        profile = self.cfg.profiles[req.profile_id]
        key = req.idem_key or (
            f"{req.origem}:{req.decision_id}:{req.ticker}:{req.side}:{req.pregao_decisao}:{req.tipo_ordem}"
        )
        existing = self.db.one("SELECT id FROM orders WHERE profile_id = ? AND idem_key = ?", (req.profile_id, key))
        if existing:
            return int(existing["id"])

        exec_day = self.cal.next_day(date.fromisoformat(req.pregao_decisao)).isoformat()
        state = load_state(self.db, req.profile_id)
        tickers = list(state.posicoes) + [req.ticker]
        prices = last_prices(self.db, tickers, req.pregao_decisao)

        est_qty = self._estimate_quantity(req, state, prices)
        ctx = self._context(profile, state, prices, exec_day, req.ticker, req.pregao_decisao, None)
        result = (
            check_order(profile, self.cfg.universe, state, ctx, ticker=req.ticker, side=req.side,
                        quantity=est_qty, tipo_ordem=req.tipo_ordem)
            if est_qty > 0
            else RuleResult(VETADA, 0, [Violation("quantidade", VETADA, "quantidade estimada igual a zero (sem preço ou sem espaço)")])
        )
        status = "VETADA" if result.status == VETADA else "AGENDADA"
        with self.db.transaction():
            order_id = self.db.insert("orders", {
                "profile_id": req.profile_id, "ticker": req.ticker, "lado": req.side, "tipo_ordem": req.tipo_ordem,
                "dimensionamento": req.dimensionamento, "quantidade": req.quantidade, "peso_alvo_pct": req.peso_alvo_pct,
                "fracao_posicao": req.fracao_posicao, "stop_pct": req.stop_pct, "alvo_pct": req.alvo_pct,
                "pregao_decisao": req.pregao_decisao, "pregao_exec": exec_day, "status": status, "origem": req.origem,
                "motivo": req.motivo if status == "AGENDADA" else f"{req.motivo} | vetada: {result.motivo}",
                "decision_id": req.decision_id, "idem_key": key, "is_mock": self.is_mock, "created_at": now_iso(),
            })
            self._record_violations(req.profile_id, order_id, result.violacoes, req.pregao_decisao)
            log_event(self.db, origem=req.origem, tipo="ORDEM_AGENDADA" if status == "AGENDADA" else "ORDEM_VETADA",
                      profile_id=req.profile_id, pregao=req.pregao_decisao, motivo=req.motivo or result.motivo,
                      is_mock=bool(self.is_mock),
                      ref={"order_id": order_id, "decision_id": req.decision_id, "ticker": req.ticker, "lado": req.side})
        return order_id

    def _estimate_quantity(self, req: OrderRequest, state: PortfolioState, prices: dict[str, float]) -> int:
        held = state.posicoes.get(req.ticker)
        if req.dimensionamento == "quantidade":
            return int(req.quantidade or 0)
        if req.dimensionamento == "fracao_posicao":
            return max(1, math.floor((req.fracao_posicao or 0) * held.quantidade)) if held else 0
        price = prices.get(req.ticker)
        if not price or req.peso_alvo_pct is None:
            return 0
        equity = state.patrimonio(prices)
        current = (held.quantidade if held else 0) * price
        return max(0, math.floor((equity * req.peso_alvo_pct / 100 - current) / price))

    def _context(self, profile: Profile, state: PortfolioState, prices: dict[str, float], pregao: str,
                 ticker: str, ref_day: str, exec_price: float | None) -> RuleContext:
        rate, fixed = cost_context(profile)
        avg = self._avg_volume(ticker, pregao, profile.radar.janela_volume_dias)
        return RuleContext(
            pregao=pregao, is_trading_day=self.cal.is_trading_day(date.fromisoformat(pregao)), prices=prices,
            avg_volume_20d={ticker: avg} if avg is not None else {},
            turnover_month_brl=self._turnover_month(profile.id, ref_day),
            somente_reduzir=self._somente_reduzir(profile.id), exec_price=exec_price, cost_rate=rate, cost_fixed=fixed,
        )

    # ------------------------------------------------------------------ execução em D+1
    def execute(self, profile_id: str, pregao: str) -> list[int]:
        rows = self.db.query(
            "SELECT * FROM orders WHERE profile_id = ? AND pregao_exec = ? AND status = 'AGENDADA' "
            "ORDER BY CASE lado WHEN 'VENDA' THEN 0 ELSE 1 END, id", (profile_id, pregao))
        fills: list[int] = []
        for order in rows:
            with self.db.transaction():
                fill_id = self._execute_one(order, pregao)
            if fill_id:
                fills.append(fill_id)
        return fills

    def _fail(self, order: dict[str, Any], pregao: str, regra: str, motivo: str) -> None:
        profile_id = order["profile_id"]
        self._record_violations(profile_id, order["id"], [Violation(regra, VETADA, motivo)], pregao)
        self.db.execute("UPDATE orders SET status = 'VETADA', motivo = ? WHERE id = ?",
                        (f"{order['motivo'] or ''} | não executada: {motivo}", order["id"]))
        log_event(self.db, origem=order["origem"], tipo="ORDEM_VETADA", profile_id=profile_id, pregao=pregao,
                  motivo=motivo, is_mock=bool(self.is_mock), ref={"order_id": order["id"], "ticker": order["ticker"]})

    def _execute_one(self, order: dict[str, Any], pregao: str) -> int | None:
        profile = self.cfg.profiles[order["profile_id"]]
        ticker, side = order["ticker"], order["lado"]
        bar = self._bar(ticker, pregao)
        if bar is None or not bar["close"] or bar["close"] <= 0:
            self._fail(order, pregao, "preco", f"sem negociação ou sem preço de {ticker} em {pregao} (sem dado)")
            return None

        use_open = profile.execucao.momento == "abertura_d_mais_1"
        ref = float(bar["open"] if use_open and bar["open"] else bar["close"])
        price = apply_slippage(ref, side, profile.execucao)
        price = min(price, float(bar["high"])) if side == "COMPRA" else max(price, float(bar["low"]))

        state = load_state(self.db, profile.id)
        prev = self._prev(pregao)
        prices = last_prices(self.db, list(state.posicoes) + [ticker], prev)
        prices.setdefault(ticker, ref)  # ativo novo: referência do próprio pregão, só para o teto por posição

        held = state.posicoes.get(ticker)
        equity = state.patrimonio(prices)
        dim = order["dimensionamento"]
        if dim == "quantidade":
            qty = int(order["quantidade"] or 0)
        elif dim == "fracao_posicao":
            qty = max(1, math.floor((order["fracao_posicao"] or 0) * held.quantidade)) if held else 0
        else:
            current = (held.quantidade if held else 0) * prices[ticker]
            qty = max(0, math.floor((equity * (order["peso_alvo_pct"] or 0) / 100 - current) / price))
        if qty <= 0:
            self._fail(order, pregao, "quantidade", "nada a executar (posição já no peso-alvo ou sem quantidade)")
            return None

        ctx = self._context(profile, state, prices, pregao, ticker, prev, price)
        result = check_order(profile, self.cfg.universe, state, ctx, ticker=ticker, side=side, quantity=qty,
                             tipo_ordem=order["tipo_ordem"])
        self._record_violations(profile.id, order["id"], result.violacoes, pregao)
        if result.status == VETADA:
            self._fail(order, pregao, "regras", result.motivo)
            return None

        fill_id = self._fill(order, profile, result.quantidade, price, ref, pregao, bar)
        status = "REDUZIDA" if result.status == REDUZIDA or result.quantidade < qty else "EXECUTADA"
        self.db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order["id"]))
        return fill_id

    def _fill(self, order: dict[str, Any], profile: Profile, qty: int, price: float, ref: float,
              pregao: str, bar: dict[str, Any]) -> int:
        side, ticker = order["lado"], order["ticker"]
        notional = round(qty * price, 2)
        costs = calc_costs(notional, profile.execucao)
        lote, fracionario = split_lots(qty)
        fill_id = self.db.insert("fills", {
            "order_id": order["id"], "profile_id": profile.id, "ticker": ticker, "lado": side, "quantidade": qty,
            "preco": price, "valor": notional, "custos": costs.total, "slippage_brl": round(abs(price - ref) * qty, 2),
            "pregao": pregao, "tipo": order["tipo_ordem"], "is_mock": self.is_mock,
            "detalhe_json": dumps({
                "preco_referencia": ref, "lote_padrao": lote, "fracionario": fracionario,
                "corretagem": costs.corretagem, "emolumentos": round(costs.emolumentos, 4),
                "liquidacao": round(costs.liquidacao, 4),
                "dado_de_origem": {"ticker": ticker, "pregao": pregao, "fonte": bar["source"]},
            }),
        })
        sign = -1 if side == "COMPRA" else 1
        self.db.insert("cash_ledger", {"profile_id": profile.id, "pregao": pregao, "tipo": "COMPRA" if side == "COMPRA" else "VENDA",
                                       "valor": sign * notional, "ref_json": dumps({"fill_id": fill_id, "ticker": ticker}),
                                       "is_mock": self.is_mock})
        self.db.insert("cash_ledger", {"profile_id": profile.id, "pregao": pregao, "tipo": "CUSTO", "valor": -costs.total,
                                       "ref_json": dumps({"fill_id": fill_id}), "is_mock": self.is_mock})
        if side == "COMPRA":
            self._apply_buy(order, profile, qty, price, costs.total, pregao)
        else:
            self._apply_sell(order, profile, qty, price, costs.total, pregao, fill_id)
        log_event(self.db, origem=order["origem"], tipo="ORDEM_EXECUTADA", profile_id=profile.id, pregao=pregao,
                  motivo=order["motivo"] or order["tipo_ordem"], is_mock=bool(self.is_mock),
                  ref={"order_id": order["id"], "fill_id": fill_id, "decision_id": order["decision_id"],
                       "dado": {"ticker": ticker, "pregao": pregao, "fonte": bar["source"]}})
        return fill_id

    def _apply_buy(self, order: dict[str, Any], profile: Profile, qty: int, price: float, costs: float, pregao: str) -> None:
        pos = self.db.one("SELECT * FROM positions WHERE profile_id = ? AND ticker = ?", (profile.id, order["ticker"]))
        tick = profile.execucao.tick_brl
        stop_pct = order["stop_pct"] if order["stop_pct"] is not None else profile.limites.stop_padrao_pct
        new_stop = round_to_tick(price * (1 - stop_pct / 100), tick, "down")
        new_alvo = round_to_tick(price * (1 + order["alvo_pct"] / 100), tick, "up") if order["alvo_pct"] else None
        if pos and pos["quantidade"] > 0:
            total_qty = pos["quantidade"] + qty
            cm = (pos["quantidade"] * pos["custo_medio"] + qty * price + costs) / total_qty
            stop = new_stop if order["stop_pct"] is not None or pos["stop"] is None else pos["stop"]
            alvo = new_alvo if new_alvo is not None else pos["alvo"]
            self.db.execute(
                "UPDATE positions SET quantidade = ?, custo_medio = ?, stop = ?, alvo = ?, atualizado_em = ? "
                "WHERE profile_id = ? AND ticker = ?", (total_qty, cm, stop, alvo, now_iso(), profile.id, order["ticker"]))
        else:
            self.db.insert("positions", {
                "profile_id": profile.id, "ticker": order["ticker"], "quantidade": qty,
                "custo_medio": (qty * price + costs) / qty, "stop": new_stop, "alvo": new_alvo,
                "aberta_em": pregao, "atualizado_em": now_iso(), "is_mock": self.is_mock,
            }, or_replace=True)

    def _apply_sell(self, order: dict[str, Any], profile: Profile, qty: int, price: float, costs: float,
                    pregao: str, fill_id: int) -> None:
        pos = self.db.one("SELECT * FROM positions WHERE profile_id = ? AND ticker = ?", (profile.id, order["ticker"]))
        if not pos:
            return
        net_per_share = (qty * price - costs) / qty
        basis = qty * pos["custo_medio"]
        pnl = qty * net_per_share - basis
        self.db.insert("trades", {
            "profile_id": profile.id, "ticker": order["ticker"], "entrada": pos["aberta_em"], "saida": pregao,
            "quantidade": qty, "preco_entrada": pos["custo_medio"], "preco_saida": net_per_share, "pnl": pnl,
            "pnl_pct": pnl / basis if basis else 0.0, "motivo_saida": order["tipo_ordem"] if order["tipo_ordem"] in ("STOP", "ALVO") else (order["motivo"] or "VENDA"),
            "fill_id": fill_id, "is_mock": self.is_mock,
        })
        remaining = pos["quantidade"] - qty
        if remaining <= 0:
            self.db.execute("DELETE FROM positions WHERE profile_id = ? AND ticker = ?", (profile.id, order["ticker"]))
        else:
            self.db.execute("UPDATE positions SET quantidade = ?, atualizado_em = ? WHERE profile_id = ? AND ticker = ?",
                            (remaining, now_iso(), profile.id, order["ticker"]))

    # ------------------------------------------------------------------ stops e alvos (pior caso)
    def stops(self, profile_id: str, pregao: str) -> list[int]:
        profile = self.cfg.profiles[profile_id]
        out: list[int] = []
        state = load_state(self.db, profile_id)
        for pos in list(state.posicoes.values()):
            bar = self._bar(pos.ticker, pregao)
            if bar is None or not bar["close"]:
                continue
            if pos.aberta_em == pregao and profile.execucao.momento != "abertura_d_mais_1":
                continue  # comprada no fechamento: o stop só vale a partir do pregão seguinte
            exit_ref: float | None = None
            tipo = ""
            if pos.stop and bar["open"] <= pos.stop:
                exit_ref, tipo = float(bar["open"]), "STOP"          # gap: sai na abertura
            elif pos.stop and bar["low"] <= pos.stop:
                exit_ref, tipo = float(pos.stop), "STOP"             # mínima tocou o stop
            elif pos.alvo and bar["high"] >= pos.alvo:
                exit_ref, tipo = float(pos.alvo), "ALVO"             # só se a máxima tocou o alvo
            if exit_ref is None:
                continue
            with self.db.transaction():
                price = max(apply_slippage(exit_ref, "VENDA", profile.execucao), float(bar["low"]))
                order_id = self.db.insert("orders", {
                    "profile_id": profile_id, "ticker": pos.ticker, "lado": "VENDA", "tipo_ordem": tipo,
                    "dimensionamento": "quantidade", "quantidade": pos.quantidade,
                    "pregao_decisao": pregao, "pregao_exec": pregao, "status": "EXECUTADA", "origem": "sistema",
                    "motivo": f"{tipo} atingido (pior caso com OHLC diário)", "idem_key": f"auto:{tipo}:{pos.ticker}:{pregao}",
                    "is_mock": self.is_mock, "created_at": now_iso(),
                }, or_ignore=True)
                if not order_id:
                    continue  # reexecução: já processado
                order = self.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
                assert order is not None
                out.append(self._fill(order, profile, pos.quantidade, price, exit_ref, pregao, bar))
        return out

    # ------------------------------------------------------------------ eventos corporativos e proventos
    def apply_corporate_events(self, profile_id: str, pregao: str) -> int:
        state = load_state(self.db, profile_id)
        if not state.posicoes:
            return 0
        applied = 0
        marks = ",".join("?" for _ in state.posicoes)
        events = self.db.query(
            f"SELECT * FROM corporate_events WHERE data_ex = ? AND ticker IN ({marks}) ORDER BY id",
            (pregao, *state.posicoes))
        for ev in events:
            pos = state.posicoes[ev["ticker"]]
            with self.db.transaction():
                claimed = self.db.execute(
                    "INSERT OR IGNORE INTO event_applications (profile_id, event_id, pregao) VALUES (?, ?, ?)",
                    (profile_id, ev["id"], pregao)).rowcount
                if not claimed:
                    continue
                if ev["tipo"] in ("DIVIDENDO", "JCP"):
                    pay = ev["data_pagamento"] if ev["data_pagamento"] and ev["data_pagamento"] >= pregao else pregao
                    total = round(pos.quantidade * ev["valor"], 2)
                    self.db.insert("dividend_accruals", {
                        "profile_id": profile_id, "event_id": ev["id"], "ticker": pos.ticker,
                        "quantidade": pos.quantidade, "valor_total": total, "pregao_credito": pay, "creditado": 0,
                    }, or_ignore=True)
                    log_event(self.db, origem="sistema", tipo="PROVENTO_A_RECEBER", profile_id=profile_id, pregao=pregao,
                              motivo=f"{ev['tipo']} de {pos.ticker}: R$ {total:,.2f} bruto, crédito em {pay}",
                              is_mock=bool(self.is_mock), ref={"event_id": ev["id"], "ticker": pos.ticker})
                else:
                    self._apply_quantity_event(profile_id, pos.ticker, ev, pregao)
                applied += 1
        return applied

    def _apply_quantity_event(self, profile_id: str, ticker: str, ev: dict[str, Any], pregao: str) -> None:
        pos = self.db.one("SELECT * FROM positions WHERE profile_id = ? AND ticker = ?", (profile_id, ticker))
        if not pos:
            return
        profile = self.cfg.profiles[profile_id]
        factor = float(ev["fator"])
        exact = pos["quantidade"] * factor
        new_qty = math.floor(exact + 1e-9)
        tick = profile.execucao.tick_brl
        basis = pos["quantidade"] * pos["custo_medio"]
        if new_qty <= 0:
            return
        bar = self._bar(ticker, pregao)
        leftover = exact - new_qty
        if leftover > 1e-9 and bar:
            value = round(leftover * float(bar["close"]), 2)
            self.db.insert("cash_ledger", {
                "profile_id": profile_id, "pregao": pregao, "tipo": "AJUSTE", "valor": value,
                "ref_json": dumps({"motivo": "fração de ações do evento corporativo, paga em dinheiro", "event_id": ev["id"]}),
                "is_mock": self.is_mock})
        self.db.execute(
            "UPDATE positions SET quantidade = ?, custo_medio = ?, stop = ?, alvo = ?, atualizado_em = ? "
            "WHERE profile_id = ? AND ticker = ?",
            (new_qty, basis / new_qty,
             round_to_tick(pos["stop"] / factor, tick) if pos["stop"] else None,
             round_to_tick(pos["alvo"] / factor, tick) if pos["alvo"] else None,
             now_iso(), profile_id, ticker))
        log_event(self.db, origem="sistema", tipo="EVENTO_CORPORATIVO_APLICADO", profile_id=profile_id, pregao=pregao,
                  motivo=f"{ev['tipo']} de {ticker} (fator {factor:g}): {pos['quantidade']} -> {new_qty} ações",
                  is_mock=bool(self.is_mock), ref={"event_id": ev["id"], "ticker": ticker})

    def credit_accruals(self, profile_id: str, pregao: str) -> float:
        total = 0.0
        for acc in self.db.query(
            "SELECT * FROM dividend_accruals WHERE profile_id = ? AND creditado = 0 AND pregao_credito <= ? ORDER BY id",
            (profile_id, pregao),
        ):
            with self.db.transaction():
                done = self.db.execute("UPDATE dividend_accruals SET creditado = 1 WHERE id = ? AND creditado = 0",
                                       (acc["id"],)).rowcount
                if not done:
                    continue
                self.db.insert("cash_ledger", {
                    "profile_id": profile_id, "pregao": pregao, "tipo": "PROVENTO", "valor": acc["valor_total"],
                    "ref_json": dumps({"event_id": acc["event_id"], "ticker": acc["ticker"], "bruto": True}),
                    "is_mock": self.is_mock})
                log_event(self.db, origem="sistema", tipo="PROVENTO_CREDITADO", profile_id=profile_id, pregao=pregao,
                          motivo=f"{acc['ticker']}: R$ {acc['valor_total']:,.2f} (bruto, sem retenção de IR)",
                          is_mock=bool(self.is_mock), ref={"event_id": acc["event_id"]})
                total += acc["valor_total"]
        return total

    # ------------------------------------------------------------------ marcação a mercado
    def mark_to_market(self, profile_id: str, pregao: str) -> dict[str, Any]:
        profile = self.cfg.profiles[profile_id]
        state = load_state(self.db, profile_id)
        prices: dict[str, float] = {}
        stale = 0
        for t in state.posicoes:
            bar = self._bar(t, pregao)
            if bar and bar["close"]:
                prices[t] = float(bar["close"])
            else:
                stale += 1  # sem negócio no pregão: usa o último preço conhecido e SINALIZA (R2)
                prices.update(last_prices(self.db, [t], pregao))
        positions_value = state.valor_posicoes(prices)
        equity = state.caixa + positions_value

        pst = self.db.one("SELECT * FROM profile_state WHERE profile_id = ?", (profile_id,)) or {}
        prev = self.db.one("SELECT patrimonio, pico FROM portfolio_history WHERE profile_id = ? AND pregao < ? "
                           "ORDER BY pregao DESC LIMIT 1", (profile_id, pregao))
        prev_equity = float(prev["patrimonio"]) if prev else float(pst.get("capital_inicial") or profile.capital_inicial)
        peak = max(float(prev["pico"]) if prev else prev_equity, equity)
        drawdown = equity / peak - 1 if peak else 0.0

        with self.db.transaction():
            self.db.insert("portfolio_history", {
                "profile_id": profile_id, "pregao": pregao, "patrimonio": equity, "caixa": state.caixa,
                "posicoes_valor": positions_value, "retorno_dia": equity / prev_equity - 1 if prev_equity else 0.0,
                "pico": peak, "drawdown": drawdown, "defasados": stale, "is_mock": self.is_mock,
            }, or_replace=True)
            self.db.execute("DELETE FROM position_snapshots WHERE profile_id = ? AND pregao = ?", (profile_id, pregao))
            for t, p in state.posicoes.items():
                self.db.insert("position_snapshots", {
                    "profile_id": profile_id, "pregao": pregao, "ticker": t, "quantidade": p.quantidade,
                    "preco_fech": prices.get(t), "custo_medio": p.custo_medio})
            self.db.execute("UPDATE profile_state SET pico_patrimonio = ?, atualizado_em = ? WHERE profile_id = ?",
                            (peak, now_iso(), profile_id))
            self._update_drawdown_mode(profile, pst, -drawdown * 100, pregao)
        return {"patrimonio": equity, "caixa": state.caixa, "drawdown": drawdown, "defasados": stale}

    def _update_drawdown_mode(self, profile: Profile, pst: dict[str, Any], dd_pct: float, pregao: str) -> None:
        on = bool(pst.get("somente_reduzir"))
        lim = profile.limites
        if not on and dd_pct >= lim.drawdown_max_carteira_pct:
            self.db.execute("UPDATE profile_state SET somente_reduzir = 1 WHERE profile_id = ?", (profile.id,))
            self._mode_event(profile.id, pregao, "somente reduzir ATIVADO",
                             f"drawdown de {dd_pct:.1f}% atingiu o máximo de {lim.drawdown_max_carteira_pct:g}%")
        elif on and dd_pct <= lim.drawdown_retomada_pct:
            self.db.execute("UPDATE profile_state SET somente_reduzir = 0 WHERE profile_id = ?", (profile.id,))
            self._mode_event(profile.id, pregao, "somente reduzir DESATIVADO",
                             f"drawdown recuou a {dd_pct:.1f}% (retomada em {lim.drawdown_retomada_pct:g}%)")

    def _mode_event(self, profile_id: str, pregao: str, regra: str, motivo: str) -> None:
        self._record_violations(profile_id, None, [Violation(regra, "MODO", motivo)], pregao)
        log_event(self.db, origem="sistema", tipo="MODO_CARTEIRA", profile_id=profile_id, pregao=pregao,
                  motivo=f"{regra}: {motivo}", is_mock=bool(self.is_mock))
