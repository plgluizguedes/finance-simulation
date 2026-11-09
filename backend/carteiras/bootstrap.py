"""Montagem dos objetos compartilhados por CLI e API: configuração, banco, calendário, agente e agendador."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import settings
from .agent.base import Agent
from .agent.mock import MockAgent
from .calendar_b3 import Calendar
from .collector import mock as mock_collector
from .collector.brapi import BrapiProvider
from .collector.pipeline import collect_real
from .config import Config, load_config
from .db import Database, open_db
from .escalation.budget import BudgetManager
from .eventlog import log_event
from .scheduler.runner import RoundResult, Runner
from .seeds.mock_memory import seed_mock_memory


@dataclass
class App:
    cfg: Config
    cal: Calendar
    db: Database
    mode: str
    agent: Agent

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    def budget(self) -> BudgetManager:
        return BudgetManager(self.db, self.cfg)

    def inbox(self) -> Path:
        path = settings.data_dir() / "inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def collect(self) -> dict[str, object]:
        """Coleta do dia. Em modo mock não há fonte externa: os dados são gerados por `seed-mock`."""
        if self.is_mock:
            return {"modo": "mock", "mensagem": "sem coleta externa em modo mock"}
        budget = self.budget()
        provider = BrapiProvider(on_request=budget.spend_brapi)
        return collect_real(self.db, self.cfg, budget, inbox=self.inbox(), provider=provider)

    def runner(self, **kw: object) -> Runner:
        return Runner(
            self.db, self.cfg, self.cal, mode=self.mode, agent=self.agent,
            collector=None if self.is_mock else self.collect,
            execute_mock_orders=settings.mock_agent_executes_orders(self.mode), **kw,  # type: ignore[arg-type]
        )

    def seed_mock(self, *, end: date | None = None, history_sessions: int = 300, window: int = 70) -> RoundResult:
        """Gera os dados MOCK e roda o pipeline real sobre as últimas `window` sessões. Só em modo mock."""
        if not self.is_mock:
            raise RuntimeError("seed-mock só roda em CARTEIRAS_MODE=mock: nunca grava dados de mentira no banco real")
        end = end or self.cal.on_or_before(date.today() - timedelta(days=1))
        mock_collector.generate(self.db, self.cfg, self.cal, end=end, sessions=history_sessions)
        from .adjust.adjust import rebuild_all
        rebuild_all(self.db, self.cfg.universe.all_tickers() + [self.cfg.universe.indice.codigo])
        start = mock_collector.seed_window(end, self.cal, window)
        log_event(self.db, origem="sistema", tipo="SEED_MOCK", motivo=f"dados MOCK até {end.isoformat()}, carteiras desde {start.isoformat()}",
                  is_mock=True)
        result = self.runner(clock=lambda: end).run_round(start_at=start.isoformat())
        seed_mock_memory(self.db, self.cfg)
        return result


def build_app(mode: str | None = None, db_path: str | Path | None = None) -> App:
    settings.load_dotenv()
    mode = mode or settings.current_mode()
    cfg = load_config()
    cal = Calendar(cfg.calendar_closed, cfg.calendar_open)
    db = open_db(db_path if db_path is not None else settings.db_path(mode))
    return App(cfg=cfg, cal=cal, db=db, mode=mode, agent=MockAgent())
