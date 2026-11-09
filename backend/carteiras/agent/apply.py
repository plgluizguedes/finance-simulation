"""Do Registro de Decisão às ordens: cada ordem vai ao BrokerSim, que a valida nas regras do perfil (R4)."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..db import Database, dumps, loads, now_iso
from ..eventlog import log_event
from ..memory import store as memory
from ..simulation.broker import BrokerSim, OrderRequest
from .schema import DecisaoAtivo, RegistroDecisao


def save_decision(db: Database, profile_id: str, pregao: str, ticker: str | None, tipo: str, origem: str, agente: str,
                  registro: RegistroDecisao, dossier_id: int | None, *, is_mock: bool, schema_error: str | None = None) -> int:
    decision_id = db.insert("decisions", {
        "profile_id": profile_id, "pregao": pregao, "ticker": ticker if ticker != "*" else None, "tipo": tipo, "origem": origem,
        "agente": agente, "is_mock": int(is_mock), "registro_json": registro.model_dump_json(), "dossier_id": dossier_id,
        "parecer_regras_json": None, "created_at": now_iso(),
    })
    log_event(db, origem=origem, tipo="DECISAO_FORA_DO_SCHEMA" if schema_error else "DECISAO_REGISTRADA", profile_id=profile_id,
              pregao=pregao, motivo=(schema_error or registro.leitura_de_mercado)[:300],
              ref={"decision_id": decision_id, "dossier_id": dossier_id}, is_mock=is_mock)
    return decision_id


def to_order(d: DecisaoAtivo, profile_id: str, pregao: str, origem: str, decision_id: int, tipo_ordem: str = "NORMAL") -> OrderRequest | None:
    if d.acao in ("manter", "nenhuma") or d.ticker == "*":
        return None
    side = "COMPRA" if d.acao == "comprar" else "VENDA"
    t = d.tamanho
    dim, val = (t.dimensionamento, t.valor) if t else ("fracao_posicao", 1.0)     # 'vender' sem tamanho = posição inteira
    return OrderRequest(
        profile_id=profile_id, ticker=d.ticker, side=side, dimensionamento=dim, pregao_decisao=pregao, origem=origem,
        motivo=d.justificativa or f"decisão #{decision_id}", decision_id=decision_id,
        quantidade=int(val) if dim == "quantidade" else None,
        peso_alvo_pct=val if dim == "peso_alvo_pct" else None,
        fracao_posicao=val if dim == "fracao_posicao" else None,
        stop_pct=d.stop_pct, alvo_pct=d.alvo_pct, tipo_ordem=tipo_ordem,
    )


def apply_registro(db: Database, cfg: Config, broker: BrokerSim, profile_id: str, pregao: str, registro: RegistroDecisao,
                   decision_id: int, *, origem: str, execute_orders: bool, is_mock: bool, tipo_ordem: str = "NORMAL") -> dict[str, Any]:
    """Agenda as ordens da decisão, grava o plano e anexa o parecer das regras à decisão."""
    order_ids: list[int] = []
    if execute_orders:
        for d in registro.decisao:
            req = to_order(d, profile_id, pregao, origem, decision_id, tipo_ordem)
            if req:
                oid = broker.schedule(req)
                if oid is not None:
                    order_ids.append(oid)
    else:
        if any(d.acao in ("comprar", "vender", "reduzir") for d in registro.decisao):
            log_event(db, origem="sistema", tipo="ORDENS_NAO_AGENDADAS", profile_id=profile_id, pregao=pregao,
                      motivo="decisão do MockAgent em modo real: registrada, mas sem ordens (ver docs/DECISIONS.md D15)",
                      ref={"decision_id": decision_id}, is_mock=is_mock)
    plan_id = memory.save_plan(db, profile_id, decision_id, pregao, registro, is_mock=is_mock)
    parecer: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for oid in order_ids:
        orders.append(db.one("SELECT id, ticker, lado, status, pregao_exec, motivo FROM orders WHERE id = ?", (oid,)) or {})
        for ev in db.query("SELECT regra, resultado, motivo, detalhe_json FROM rule_events WHERE order_id = ?", (oid,)):
            parecer.append({"order_id": oid, "regra": ev["regra"], "resultado": ev["resultado"], "motivo": ev["motivo"],
                            "detalhe": loads(ev["detalhe_json"], None)})
    db.execute("UPDATE decisions SET parecer_regras_json = ? WHERE id = ?", (dumps({"ordens": orders, "eventos": parecer}), decision_id))
    return {"ordens": orders, "parecer": parecer, "plano_id": plan_id}
