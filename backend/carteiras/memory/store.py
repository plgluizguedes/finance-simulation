"""Persistência e consultas da memória."""

from __future__ import annotations

from typing import Any

from ..agent.schema import RegistroDecisao
from ..db import Database, dumps, loads, now_iso


def save_plan(db: Database, profile_id: str, decision_id: int, pregao: str, registro: RegistroDecisao, *, is_mock: bool) -> int | None:
    """Grava o plano futuro e cada gatilho (já em forma verificável por código). Sem passos nem gatilhos, não cria plano."""
    plan = registro.plano_futuro
    if not plan.passos and not plan.gatilhos:
        return None
    plan_id = db.insert("plans", {
        "profile_id": profile_id, "decision_id": decision_id, "descricao": " | ".join(plan.passos) or "plano com gatilhos",
        "status": "ATIVO", "is_mock": int(is_mock), "created_at": now_iso(),
    })
    for g in plan.gatilhos:
        db.insert("triggers", {
            "plan_id": plan_id, "profile_id": profile_id, "ticker": g.condicao.ticker or g.acao.ticker,
            "descricao": g.descricao, "tipo": g.condicao.tipo,
            "condicao_json": dumps(g.condicao.model_dump()),
            "invalida_json": dumps(g.invalida_se.model_dump()) if g.invalida_se else None,
            "acao_json": dumps(g.acao.model_dump()), "status": "PENDENTE", "criado_pregao": pregao,
            "is_mock": int(is_mock), "created_at": now_iso(),
        })
    return plan_id


def pending_triggers(db: Database, profile_id: str, ticker: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM triggers WHERE profile_id = ? AND status = 'PENDENTE'"
    params: list[Any] = [profile_id]
    if ticker:
        sql += " AND (ticker = ? OR ticker IS NULL)"
        params.append(ticker)
    rows = db.query(sql + " ORDER BY id", params)
    for r in rows:
        r["condicao"] = loads(r["condicao_json"], {})
        r["invalida"] = loads(r["invalida_json"], None)
        r["acao"] = loads(r["acao_json"], {})
        r["avaliacao"] = loads(r["ultima_avaliacao_json"], None)
    return rows


def _decision_row(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    reg = loads(row["registro_json"], {})
    orders = db.query("SELECT id, ticker, lado, status, tipo_ordem FROM orders WHERE decision_id = ? ORDER BY id", (row["id"],))
    return {
        "id": row["id"], "pregao": row["pregao"], "ticker": row["ticker"], "tipo": row["tipo"], "origem": row["origem"],
        "is_mock": row["is_mock"], "confianca": (reg.get("confianca") or {}).get("valor"),
        "acoes": [{"ticker": d.get("ticker"), "acao": d.get("acao")} for d in reg.get("decisao", [])],
        "ordens": orders,
    }


def recent_decisions(db: Database, profile_id: str, *, ticker: str | None = None, before: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    sql = "SELECT * FROM decisions WHERE profile_id = ?"
    params: list[Any] = [profile_id]
    if ticker:
        sql += " AND ticker = ?"
        params.append(ticker)
    if before:
        sql += " AND pregao <= ?"
        params.append(before)
    rows = db.query(sql + " ORDER BY pregao DESC, id DESC LIMIT ?", (*params, limit))
    return [_decision_row(db, r) for r in rows]


def lessons(db: Database, profile_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = db.query("SELECT id, titulo, descricao, categoria, is_mock FROM lessons WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
                    (profile_id, limit))
    return rows


def calibration(db: Database, profile_id: str) -> list[dict[str, Any]]:
    rows = db.query("SELECT faixa_confianca, n, acertos, is_mock FROM calibration WHERE profile_id = ? ORDER BY faixa_confianca", (profile_id,))
    for r in rows:
        r["taxa_acerto"] = r["acertos"] / r["n"] if r["n"] else None
    return rows


def error_categories(db: Database, profile_id: str) -> list[dict[str, Any]]:
    return db.query("SELECT categoria, n, is_mock FROM error_categories WHERE profile_id = ? ORDER BY n DESC", (profile_id,))
