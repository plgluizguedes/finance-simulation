"""Camada 2: do alerta ao dossiê e à decisão. Elegibilidade, prioridade, orçamento e revisão rotineira."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from ..agent.apply import apply_registro, save_decision
from ..agent.base import Agent
from ..agent.schema import coerce_registro
from ..calendar_b3 import Calendar
from ..collector.base import BudgetExceeded, NotAvailable, SourceUnavailable
from ..config import Config
from ..db import Database, dumps, loads, now_iso
from ..eventlog import log_event, log_system
from ..simulation.broker import BrokerSim
from .budget import ESCAL, IA_CALLS, IA_TOKENS, BudgetManager
from .dossier import build_dossier, save_dossier

SEV_RANK = {"INFO": 0, "ATENCAO": 1, "CRITICO": 2}
LIMIT_TYPES = ("LIMITE_POSICAO", "LIMITE_SETOR", "LIMITE_CAIXA", "DRAWDOWN_CARTEIRA")
Enrich = Callable[[str], dict[str, Any]]
POSTPONE_TTL_SESSIONS = 5      # alerta adiado por orçamento que passa disso perdeu a urgência: expira em vez de gastar orçamento novo


def eligibility(alert: dict[str, Any]) -> tuple[bool, str]:
    """Regras da seção 4 do desenho. Devolve (elegível, motivo)."""
    tipo, sev, held, ticker = alert["tipo"], alert["severidade"], bool(alert["em_carteira"]), alert["ticker"]
    if alert["cooldown"]:
        return False, "em cooldown: mesmo ativo e tipo já escalado recentemente, sem aumento de severidade"
    if tipo.startswith(LIMIT_TYPES):
        return True, "violação de limite do perfil que exige decisão"
    if tipo == "EVENTO_AGENDADO" and held:
        return True, "evento agendado relevante a posição aberta"
    if held and SEV_RANK[sev] >= SEV_RANK["ATENCAO"]:
        return True, "severidade ATENCAO ou CRITICO em ativo da carteira"
    if ticker == "*" and sev == "CRITICO":
        return True, "alerta CRITICO de mercado"
    if not held and sev == "CRITICO":
        return True, "severidade CRITICO em ativo da watchlist"
    return False, "abaixo do critério de escalonamento (ATENCAO/CRITICO em carteira, CRITICO em watchlist)"


def _priority(group: list[dict[str, Any]], held_first: bool) -> tuple[int, int, str]:
    held = any(a["em_carteira"] or a["ticker"] == "*" for a in group)
    sev = max(SEV_RANK[a["severidade"]] for a in group)
    return (0 if (held and held_first) else 1, -sev, group[0]["ticker"])


def _upsert_escalation(db: Database, alert: dict[str, Any], profile_id: str, pregao: str, status: str, motivo: str, *,
                       is_mock: bool, dossier_id: int | None = None, decision_id: int | None = None, consumo: dict[str, Any] | None = None) -> None:
    exists = db.one("SELECT id FROM escalations WHERE alert_id = ?", (alert["id"],))
    values = {"status": status, "motivo": motivo, "dossier_id": dossier_id, "decision_id": decision_id,
              "consumo_json": dumps(consumo) if consumo is not None else None}
    if exists:
        db.execute("UPDATE escalations SET status = ?, motivo = ?, dossier_id = ?, decision_id = ?, consumo_json = ? WHERE id = ?",
                   (values["status"], values["motivo"], values["dossier_id"], values["decision_id"], values["consumo_json"], exists["id"]))
    else:
        db.insert("escalations", {"alert_id": alert["id"], "profile_id": profile_id, "ticker": alert["ticker"], "pregao": pregao,
                                  "is_mock": int(is_mock), "created_at": now_iso(), **values})


def _hydrate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        r["detalhe"] = loads(r["detalhe_json"], {})
        r["refs"] = loads(r["refs_json"], {})
    return rows


def _call_agent(agent: Agent, dossier: dict[str, Any], tipo: str) -> tuple[Any, str | None]:
    try:
        raw = agent.analyze(dossier, {"tipo": tipo})
    except Exception as exc:                                  # agente é fronteira externa: qualquer falha vira 'nenhuma ação'
        return None, f"falha do agente: {type(exc).__name__}"
    return raw, None


def run_escalation(
    db: Database, cfg: Config, cal: Calendar, broker: BrokerSim, budget: BudgetManager, agent: Agent, profile_id: str, pregao: str, *,
    is_mock: bool, execute_orders: bool, enrich: Enrich | None = None,
) -> dict[str, Any]:
    """Escalona os alertas do pregão (e os adiados de dias anteriores), respeitando o orçamento. Idempotente por alert_id."""
    todays = _hydrate(db.query("SELECT * FROM alerts WHERE profile_id = ? AND pregao = ? ORDER BY id", (profile_id, pregao)))
    # alertas já processados neste pregão (reexecução) não consomem de novo
    done = {r["alert_id"] for r in db.query("SELECT alert_id FROM escalations WHERE profile_id = ? AND status IN ('EXECUTADO','NAO_ELEGIVEL')", (profile_id,))}
    postponed = _hydrate(db.query(
        "SELECT a.* FROM alerts a JOIN escalations e ON e.alert_id = a.id WHERE e.profile_id = ? AND e.status = 'ADIADO_ORCAMENTO' AND a.pregao < ? ORDER BY a.id",
        (profile_id, pregao)))
    result = {"executados": 0, "adiados": 0, "nao_elegiveis": 0, "agrupados": 0, "expirados": 0}
    fresh: list[dict[str, Any]] = []
    with db.transaction():
        for a in postponed:
            if len(cal.sessions_between(date.fromisoformat(a["pregao"]), date.fromisoformat(pregao))) > POSTPONE_TTL_SESSIONS:
                _upsert_escalation(db, a, profile_id, pregao, "NAO_ELEGIVEL",
                                   f"adiado por orçamento e expirado após {POSTPONE_TTL_SESSIONS} pregões", is_mock=is_mock)
                result["expirados"] += 1
            else:
                fresh.append(a)
    queue = [a for a in fresh + todays if a["id"] not in done]

    eligible: list[dict[str, Any]] = []
    with db.transaction():
        for a in queue:
            ok, why = eligibility(a)
            if ok:
                eligible.append(a)
            else:
                _upsert_escalation(db, a, profile_id, pregao, "NAO_ELEGIVEL", why, is_mock=is_mock)
                result["nao_elegiveis"] += 1

    groups: dict[str, list[dict[str, Any]]] = {}
    for a in eligible:
        groups.setdefault(a["ticker"], []).append(a)
    ordered = sorted(groups.values(), key=lambda g: _priority(g, cfg.budgets.prioridade.carteira_antes_da_watchlist))

    for group in ordered:
        group.sort(key=lambda a: (-SEV_RANK[a["severidade"]], a["id"]))
        primary, extras = group[0], group[1:]
        ok, why = budget.can_escalate(profile_id, needs_brapi=enrich is not None and primary["ticker"] != "*")
        if not ok:
            with db.transaction():
                for a in group:
                    _upsert_escalation(db, a, profile_id, pregao, "ADIADO_ORCAMENTO", f"adiado por orçamento: {why}", is_mock=is_mock)
                log_event(db, origem="sistema", tipo="ESCALONAMENTO_ADIADO", profile_id=profile_id, pregao=pregao,
                          motivo=f"{primary['ticker']}: adiado por orçamento ({why})", is_mock=is_mock,
                          ref={"alert_ids": [a["id"] for a in group]})
            log_system(db, "WARNING", "escalation", f"{profile_id} {primary['ticker']}: adiado por orçamento ({why})")
            result["adiados"] += len(group)
            continue

        enrichment: dict[str, Any] | None = None
        brapi_used = 0
        if enrich is not None and primary["ticker"] != "*":
            before = int(budget.used_month("*", "brapi_req", budget._clock_day()))
            try:
                enrichment = enrich(primary["ticker"])
            except (BudgetExceeded, NotAvailable, SourceUnavailable) as exc:
                enrichment = {"status": "indisponivel", "motivo": str(exc)}
            brapi_used = int(budget.used_month("*", "brapi_req", budget._clock_day())) - before

        dossier, refs = build_dossier(db, cfg, cal, profile_id, pregao, group, tipo="ALERTA", budget=budget.snapshot(),
                                      enrichment=enrichment, is_mock=is_mock)
        with db.transaction():
            dossier_id = save_dossier(db, profile_id, primary["ticker"], pregao, "ALERTA", dossier, refs, is_mock=is_mock)
            raw, falha = _call_agent(agent, dossier, "ALERTA")
            registro, schema_err = coerce_registro(raw, agent.origem)
            decision_id = save_decision(db, profile_id, pregao, primary["ticker"], "ALERTA", agent.origem, agent.nome, registro, dossier_id,
                                        is_mock=is_mock or agent.is_mock, schema_error=falha or (schema_err if raw is not None else schema_err))
            applied = apply_registro(db, cfg, broker, profile_id, pregao, registro, decision_id, origem=agent.origem,
                                     execute_orders=execute_orders, is_mock=is_mock or agent.is_mock)
            consumo = {"brapi_req": brapi_used, "ia_chamadas": 1, "ia_tokens": 0, "ordens": [o.get("id") for o in applied["ordens"]]}
            _upsert_escalation(db, primary, profile_id, pregao, "EXECUTADO", "pesquisa dirigida concluída", is_mock=is_mock,
                               dossier_id=dossier_id, decision_id=decision_id, consumo=consumo)
            for a in extras:
                _upsert_escalation(db, a, profile_id, pregao, "EXECUTADO", f"agrupado ao alerta #{primary['id']} do mesmo ativo (sem consumo extra)",
                                   is_mock=is_mock, dossier_id=dossier_id, decision_id=decision_id, consumo={"agrupado_em": primary["id"]})
            budget.add(profile_id, ESCAL, 1)
            budget.add(profile_id, IA_CALLS, 1)
            budget.add(profile_id, IA_TOKENS, 0)
            log_event(db, origem="sistema", tipo="ESCALONAMENTO_EXECUTADO", profile_id=profile_id, pregao=pregao,
                      motivo=f"{primary['ticker']}: {primary['tipo']} ({primary['severidade']})", is_mock=is_mock,
                      ref={"alert_ids": [a["id"] for a in group], "dossier_id": dossier_id, "decision_id": decision_id})
        result["executados"] += 1
        result["agrupados"] += len(extras)
    return result


def review_due(db: Database, cfg: Config, cal: Calendar, profile_id: str, pregao: str) -> bool:
    """Revisão rotineira (camada 3): a cada N pregões desde a última (ou desde o início da carteira)."""
    every = cfg.profiles[profile_id].revisao.rotina_a_cada_pregoes
    last = db.scalar("SELECT MAX(pregao) FROM decisions WHERE profile_id = ? AND tipo = 'REVISAO'", (profile_id,))
    start = last or db.scalar("SELECT iniciado_em FROM profile_state WHERE profile_id = ?", (profile_id,))
    if not start:
        return False
    return len(cal.sessions_between(date.fromisoformat(start), date.fromisoformat(pregao))) >= every


def run_review(db: Database, cfg: Config, cal: Calendar, broker: BrokerSim, budget: BudgetManager, agent: Agent, profile_id: str,
               pregao: str, *, is_mock: bool, execute_orders: bool) -> int | None:
    """Revisão rotineira da carteira. Uma por pregão no máximo (idempotente). Não consome escalonamento, mas conta chamada de IA."""
    if db.scalar("SELECT 1 FROM decisions WHERE profile_id = ? AND tipo = 'REVISAO' AND pregao = ?", (profile_id, pregao)):
        return None
    if not review_due(db, cfg, cal, profile_id, pregao):
        return None
    prof = cfg.profiles[profile_id]
    if budget.used(profile_id, IA_CALLS) >= prof.orcamento.chamadas_ia_max_por_dia:
        log_event(db, origem="sistema", tipo="REVISAO_ADIADA", profile_id=profile_id, pregao=pregao,
                  motivo="revisão rotineira adiada por orçamento de chamadas de IA", is_mock=is_mock)
        return None
    dossier, refs = build_dossier(db, cfg, cal, profile_id, pregao, [], tipo="REVISAO", budget=budget.snapshot(), is_mock=is_mock)
    with db.transaction():
        dossier_id = save_dossier(db, profile_id, None, pregao, "REVISAO", dossier, refs, is_mock=is_mock)
        raw, falha = _call_agent(agent, dossier, "REVISAO")
        registro, schema_err = coerce_registro(raw, agent.origem)
        decision_id = save_decision(db, profile_id, pregao, None, "REVISAO", agent.origem, agent.nome, registro, dossier_id,
                                    is_mock=is_mock or agent.is_mock, schema_error=falha or schema_err)
        apply_registro(db, cfg, broker, profile_id, pregao, registro, decision_id, origem=agent.origem,
                       execute_orders=execute_orders, is_mock=is_mock or agent.is_mock)
        budget.add(profile_id, IA_CALLS, 1)
    return decision_id
