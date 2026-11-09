"""Rodada do agendador (seção 8 do desenho).

Cada rodada identifica os pregões pendentes de cada carteira (do último processado ao último com dados) e os processa em
ordem cronológica. Cada etapa de cada pregão é gravada em `checkpoints`; uma falha no meio retoma de onde parou, e rodar de
novo não duplica ordens, alertas, eventos nem escalonamentos (chaves únicas e chaves de idempotência).
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..agent.base import Agent
from ..calendar_b3 import Calendar
from ..config import Config
from ..db import Database, dumps, now_iso
from ..escalation.budget import BudgetManager
from ..escalation.escalate import Enrich, run_escalation, run_review
from ..eventlog import log_event, log_system
from ..marketdata import available_until
from ..memory.triggers import evaluate_triggers
from ..radar.radar import run_radar
from ..seeds.initial import apply_initial
from ..simulation.broker import BrokerSim

ETAPAS = ("eventos", "execucao", "stops", "proventos", "marcacao", "gatilhos", "radar", "escalonamento", "revisao")


@dataclass
class RoundResult:
    run_id: int
    status: str
    processados: list[tuple[str, str]] = field(default_factory=list)      # (perfil, pregão), na ordem em que foram feitos
    erros: list[str] = field(default_factory=list)
    pulados: list[str] = field(default_factory=list)
    pendentes_sem_dado: list[str] = field(default_factory=list)


class StepFailure(RuntimeError):
    pass


class Runner:
    def __init__(
        self, db: Database, cfg: Config, cal: Calendar, *, mode: str, agent: Agent,
        collector: Callable[[], dict[str, Any]] | None = None, enrich: Enrich | None = None,
        execute_mock_orders: bool = True, clock: Callable[[], date] = date.today,
        fail_after: tuple[str, str, str] | None = None,
    ):
        self.db, self.cfg, self.cal, self.mode, self.agent = db, cfg, cal, mode, agent
        self.collector, self.enrich = collector, enrich
        self.is_mock = mode == "mock"
        self.execute_orders = (not agent.is_mock) or execute_mock_orders
        self.clock = clock
        self.fail_after = fail_after      # (perfil, pregão, etapa): injeta falha para testar a retomada
        self.broker = BrokerSim(db, cfg, cal, is_mock=self.is_mock)

    # ------------------------------------------------------------------ checkpoints
    def _done(self, pid: str, pregao: str, etapa: str) -> bool:
        return bool(self.db.scalar("SELECT 1 FROM checkpoints WHERE profile_id = ? AND pregao = ? AND etapa = ? AND status = 'ok'",
                                   (pid, pregao, etapa)))

    def _mark(self, pid: str, pregao: str, etapa: str, status: str, detalhe: Any = None) -> None:
        self.db.insert("checkpoints", {"profile_id": pid, "pregao": pregao, "etapa": etapa, "status": status,
                                       "detalhe": dumps(detalhe) if detalhe is not None else None, "ts": now_iso()}, or_replace=True)

    # ------------------------------------------------------------------ pendências
    def last_data_day(self) -> str | None:
        return available_until(self.db)

    def pending(self, pid: str, until: str, start_at: str | None = None) -> list[str]:
        """Pregões a processar, em ordem. Carteira nova: começa em `start_at` (ou no último pregão com dados)."""
        state = self.db.one("SELECT iniciado_em, ultimo_pregao FROM profile_state WHERE profile_id = ?", (pid,))
        if not state or not state["iniciado_em"] or state["ultimo_pregao"] is None:
            start = state["iniciado_em"] if state and state["iniciado_em"] else (start_at or until)
            if start > until:
                return []
            return [start] + [d.isoformat() for d in self.cal.sessions_between(date.fromisoformat(start), date.fromisoformat(until))]
        return [d.isoformat() for d in self.cal.sessions_between(date.fromisoformat(state["ultimo_pregao"]), date.fromisoformat(until))]

    # ------------------------------------------------------------------ uma etapa
    def _step(self, pid: str, pregao: str, etapa: str, fn: Callable[[], Any]) -> None:
        if self._done(pid, pregao, etapa):
            return
        try:
            detail = fn()
        except Exception as exc:
            self._mark(pid, pregao, etapa, "falhou", str(exc)[:300])
            log_system(self.db, "ERROR", "scheduler", f"{pid} {pregao} etapa {etapa}: {type(exc).__name__}: {exc}",
                       {"trace": traceback.format_exc(limit=4)})
            raise StepFailure(f"{pid} {pregao} {etapa}: {type(exc).__name__}: {exc}") from exc
        self._mark(pid, pregao, etapa, "ok", detail if isinstance(detail, (dict, list, int, float, str)) else None)
        if self.fail_after == (pid, pregao, etapa):
            raise StepFailure(f"falha injetada após {etapa}")

    def _start(self, pid: str, pregao: str) -> None:
        self.broker.start_profile(pid, pregao)
        apply_initial(self.db, self.cfg, self.broker, pid, pregao, is_mock=self.is_mock)

    def process_session(self, pid: str, pregao: str) -> None:
        """Todas as etapas de um (carteira, pregão). Retomável: etapas já concluídas são puladas."""
        b = self.broker
        state = self.db.one("SELECT iniciado_em FROM profile_state WHERE profile_id = ?", (pid,))
        if not state or not state["iniciado_em"] or pregao == state["iniciado_em"]:
            # dia de início: carteira criada e ordens iniciais agendadas para o pregão seguinte; nada a executar ainda
            self._step(pid, pregao, "inicio", lambda: self._start(pid, pregao))
            self.db.execute("UPDATE profile_state SET ultimo_pregao = ?, atualizado_em = ? WHERE profile_id = ?", (pregao, now_iso(), pid))
            return
        budget = BudgetManager(self.db, self.cfg, today=date.fromisoformat(pregao), clock=self.clock)
        # eventos corporativos ANTES de ordens e stops no dia ex (docs/DECISIONS.md, D14)
        self._step(pid, pregao, "eventos", lambda: b.apply_corporate_events(pid, pregao))
        self._step(pid, pregao, "execucao", lambda: b.execute(pid, pregao))
        self._step(pid, pregao, "stops", lambda: b.stops(pid, pregao))
        self._step(pid, pregao, "proventos", lambda: b.credit_accruals(pid, pregao))
        self._step(pid, pregao, "marcacao", lambda: b.mark_to_market(pid, pregao))
        self._step(pid, pregao, "gatilhos", lambda: evaluate_triggers(self.db, b, pid, pregao, is_mock=self.is_mock))
        self._step(pid, pregao, "radar", lambda: len(run_radar(self.db, self.cfg, self.cal, pid, pregao, is_mock=self.is_mock)))
        self._step(pid, pregao, "escalonamento", lambda: run_escalation(
            self.db, self.cfg, self.cal, b, budget, self.agent, pid, pregao, is_mock=self.is_mock,
            execute_orders=self.execute_orders, enrich=self.enrich))
        self._step(pid, pregao, "revisao", lambda: run_review(
            self.db, self.cfg, self.cal, b, budget, self.agent, pid, pregao, is_mock=self.is_mock,
            execute_orders=self.execute_orders))
        self.db.execute("UPDATE profile_state SET ultimo_pregao = ?, atualizado_em = ? WHERE profile_id = ?", (pregao, now_iso(), pid))
        log_event(self.db, origem="sistema", tipo="PREGAO_PROCESSADO", profile_id=pid, pregao=pregao,
                  motivo="todas as etapas concluídas", is_mock=self.is_mock)

    # ------------------------------------------------------------------ rodada
    def run_round(self, *, until: str | None = None, start_at: str | None = None, profiles: list[str] | None = None) -> RoundResult:
        run_id = self.db.insert("scheduler_runs", {"iniciada_em": now_iso(), "status": "RODANDO"})
        result = RoundResult(run_id=run_id, status="RODANDO")
        try:
            if self.collector is not None:
                try:
                    self.collector()
                except Exception as exc:                                    # coleta falha → segue com o que há em cache, com aviso
                    result.erros.append(f"coleta: {type(exc).__name__}: {exc}")
                    log_system(self.db, "ERROR", "scheduler", f"coleta falhou: {type(exc).__name__}: {exc}")
            limit = until or self.last_data_day()
            if not limit:
                result.status = "SEM_DADOS"
                result.pendentes_sem_dado.append("nenhum preço coletado ainda")
                return result
            for pid in profiles or self.cfg.profile_ids():
                paused = self.db.scalar("SELECT pausado FROM profile_state WHERE profile_id = ?", (pid,))
                if paused:
                    result.pulados.append(f"{pid}: carteira pausada")
                    continue
                try:
                    for pregao in self.pending(pid, limit, start_at):
                        if pregao > limit:
                            break
                        self.process_session(pid, pregao)
                        result.processados.append((pid, pregao))
                except StepFailure as exc:
                    result.erros.append(str(exc))        # preserva a ordem cronológica: não pula para o pregão seguinte
            result.status = "PARCIAL" if result.erros else "OK"
            return result
        finally:
            if result.status == "RODANDO":
                result.status = "FALHA"
            self.db.execute("UPDATE scheduler_runs SET finalizada_em = ?, status = ?, pregoes_json = ?, detalhe = ? WHERE id = ?",
                            (now_iso(), result.status, dumps(sorted({p for _, p in result.processados})),
                             dumps({"erros": result.erros, "pulados": result.pulados}), run_id))
