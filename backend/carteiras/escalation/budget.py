"""Orçamentos. Nada estoura em silêncio: o que não cabe é recusado aqui e quem chamou registra 'adiado por orçamento'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from ..collector.base import BudgetExceeded
from ..config import Config
from ..db import Database

BRAPI = "brapi_req"
ESCAL = "escalonamento"
IA_CALLS = "ia_chamadas"
IA_TOKENS = "ia_tokens"


class BudgetManager:
    def __init__(self, db: Database, cfg: Config, today: date | None = None, clock: Callable[[], date] = date.today):
        """`today`: dia de referência dos contadores por perfil (na reprocessagem, o pregão decidido).
        `clock`: relógio real, usado só para a cota mensal da brapi, que é gasta quando a coleta acontece."""
        self.db = db
        self.cfg = cfg
        self._today = today
        self._clock = clock

    def day(self) -> str:
        return (self._today or self._clock()).isoformat()

    def _clock_day(self) -> str:
        return self._clock().isoformat()

    # ------------------------------------------------------------------ contadores
    def used(self, profile_id: str, recurso: str, dia: str | None = None) -> float:
        return float(self.db.scalar("SELECT usado FROM budget_usage WHERE dia = ? AND profile_id = ? AND recurso = ?",
                                    (dia or self.day(), profile_id, recurso), 0.0))

    def add(self, profile_id: str, recurso: str, amount: float = 1.0, dia: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO budget_usage (dia, profile_id, recurso, usado) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (dia, profile_id, recurso) DO UPDATE SET usado = usado + excluded.usado",
            (dia or self.day(), profile_id, recurso, amount))

    def used_month(self, profile_id: str, recurso: str, dia: str | None = None) -> float:
        month = (dia or self.day())[:7]
        return float(self.db.scalar("SELECT COALESCE(SUM(usado), 0) FROM budget_usage WHERE profile_id = ? AND recurso = ? AND dia LIKE ?",
                                    (profile_id, recurso, f"{month}-%"), 0.0))

    # ------------------------------------------------------------------ brapi (cota mensal global)
    def brapi_limit(self) -> int:
        b = self.cfg.budgets.brapi
        return int(b.cota_mensal * b.fracao_maxima)

    def brapi_remaining(self) -> int:
        return max(0, self.brapi_limit() - int(self.used_month("*", BRAPI, self._clock_day())))

    def spend_brapi(self) -> None:
        """Gasta uma requisição da brapi antes de cada chamada. Serve de `on_request` do BrapiProvider."""
        if self.brapi_remaining() <= 0:
            raise BudgetExceeded(f"cota mensal da brapi (limite de {self.brapi_limit()} requisições) esgotada")
        self.add("*", BRAPI, 1.0, self._clock_day())

    # ------------------------------------------------------------------ por perfil
    def escalation_limit(self, profile_id: str) -> int:
        return self.cfg.profiles[profile_id].orcamento.escalonamentos_max_por_dia

    def can_escalate(self, profile_id: str, *, needs_brapi: bool) -> tuple[bool, str | None]:
        prof = self.cfg.profiles[profile_id]
        if self.used(profile_id, ESCAL) >= prof.orcamento.escalonamentos_max_por_dia:
            return False, f"limite de {prof.orcamento.escalonamentos_max_por_dia} escalonamentos por dia atingido"
        if self.used(profile_id, IA_CALLS) >= prof.orcamento.chamadas_ia_max_por_dia:
            return False, f"limite de {prof.orcamento.chamadas_ia_max_por_dia} chamadas de IA por dia atingido"
        if self.used(profile_id, IA_TOKENS) >= prof.orcamento.tokens_ia_max_por_dia:
            return False, f"limite de {prof.orcamento.tokens_ia_max_por_dia} tokens de IA por dia atingido"
        if needs_brapi and self.brapi_remaining() < self.cfg.budgets.brapi.requisicoes_por_escalonamento:
            return False, "cota mensal da brapi insuficiente para a pesquisa dirigida"
        return True, None

    def snapshot(self, dia: str | None = None) -> dict[str, Any]:
        """Consumo atual, para dossiê e para a aba Sistema e Logs."""
        dia = dia or self.day()
        usado_mes = self.used_month("*", BRAPI, self._clock_day())
        out: dict[str, Any] = {"dia": dia, "brapi": {
            "usado_mes": int(usado_mes), "limite_mes": self.brapi_limit(),
            "cota_mensal": self.cfg.budgets.brapi.cota_mensal, "fracao_maxima": self.cfg.budgets.brapi.fracao_maxima,
            "pct_da_cota": round(usado_mes / self.cfg.budgets.brapi.cota_mensal * 100, 3),
        }, "perfis": {}}
        for pid, prof in self.cfg.profiles.items():
            o = prof.orcamento
            out["perfis"][pid] = {
                "escalonamentos": {"usado": self.used(pid, ESCAL, dia), "limite": o.escalonamentos_max_por_dia},
                "ia_chamadas": {"usado": self.used(pid, IA_CALLS, dia), "limite": o.chamadas_ia_max_por_dia},
                "ia_tokens": {"usado": self.used(pid, IA_TOKENS, dia), "limite": o.tokens_ia_max_por_dia},
            }
        return out
