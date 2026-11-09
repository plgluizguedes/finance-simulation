"""Avaliação dos gatilhos dos planos, por código e sem IA (seção 10 do desenho).

Resultado de cada avaliação fica registrado: disparado, não disparado (com distância) ou invalidado. Só dados até o pregão
avaliado são usados, e só em pregões posteriores ao da criação do gatilho (sem look-ahead).
"""

from __future__ import annotations

from typing import Any

from ..db import Database, dumps, loads
from ..eventlog import log_event
from ..marketdata import bar_raw, bars_adj
from ..radar.indicators import avg_volume
from ..simulation.broker import BrokerSim, OrderRequest
from ..simulation.state import load_state

SEM_DADO = "sem_dado"


def measure(db: Database, profile_id: str, cond: dict[str, Any], pregao: str, vol_window: int) -> tuple[float | str | None, float | str | None]:
    """Devolve (medido, alvo) da condição no pregão. medido None = sem dado."""
    tipo, ticker = cond["tipo"], cond.get("ticker")
    if tipo == "data_apos":
        return pregao, cond["data"]
    if tipo in ("preco_fechamento_acima", "preco_fechamento_abaixo"):
        bar = bar_raw(db, ticker, pregao)
        return (bar["close"] if bar and bar["close"] else None), cond["valor"]
    if tipo == "volume_relativo_acima":
        bars = bars_adj(db, ticker, pregao, vol_window + 1)
        avg = avg_volume(bars, vol_window)
        if not avg or not bars or bars[-1]["pregao"] != pregao:
            return None, cond["valor"]
        return (bars[-1]["volume_fin"] or 0) / avg, cond["valor"]
    if tipo == "drawdown_posicao_acima":
        pos = load_state(db, profile_id).posicoes.get(ticker)
        bar = bar_raw(db, ticker, pregao)
        if not pos or not bar or not bar["close"]:
            return None, cond["valor"]
        return (pos.custo_medio - bar["close"]) / pos.custo_medio * 100, cond["valor"]
    raise ValueError(f"condição desconhecida: {tipo}")


def holds(tipo: str, medido: float | str, alvo: float | str) -> bool:
    if tipo == "data_apos":
        return str(medido) > str(alvo)
    if tipo == "preco_fechamento_abaixo":
        return float(medido) <= float(alvo)
    return float(medido) >= float(alvo)        # acima, volume relativo, drawdown


def distance(tipo: str, medido: float | str | None, alvo: float | str | None) -> float | None:
    """Distância percentual até disparar (0 = disparou). None quando não se aplica ou sem dado."""
    if medido is None or tipo == "data_apos" or not alvo:
        return None
    m, a = float(medido), float(alvo)
    if tipo == "preco_fechamento_abaixo":
        return max(0.0, (m - a) / m * 100)
    return max(0.0, (a - m) / a * 100)


def evaluate_triggers(db: Database, broker: BrokerSim, profile_id: str, pregao: str, *, is_mock: bool) -> dict[str, int]:
    profile = broker.cfg.profiles[profile_id]
    window = profile.radar.janela_volume_dias
    counts = {"disparados": 0, "invalidados": 0, "pendentes": 0, "sem_dado": 0}
    rows = db.query("SELECT * FROM triggers WHERE profile_id = ? AND status = 'PENDENTE' AND criado_pregao < ? ORDER BY id",
                    (profile_id, pregao))
    for t in rows:
        cond, inv, acao = loads(t["condicao_json"], {}), loads(t["invalida_json"], None), loads(t["acao_json"], {})
        with db.transaction():
            if inv:
                m_inv, a_inv = measure(db, profile_id, inv, pregao, window)
                if m_inv is not None and holds(inv["tipo"], m_inv, a_inv):
                    db.execute("UPDATE triggers SET status = 'INVALIDADO', ultima_avaliacao = ?, ultima_avaliacao_json = ? WHERE id = ?",
                               (pregao, dumps({"resultado": "invalidado", "medido": m_inv, "limiar": a_inv}), t["id"]))
                    log_event(db, origem="sistema", tipo="GATILHO_INVALIDADO", profile_id=profile_id, pregao=pregao,
                              motivo=t["descricao"], ref={"trigger_id": t["id"], "plan_id": t["plan_id"]}, is_mock=is_mock)
                    counts["invalidados"] += 1
                    continue
            medido, alvo = measure(db, profile_id, cond, pregao, window)
            if medido is None:
                db.execute("UPDATE triggers SET ultima_avaliacao = ?, ultima_avaliacao_json = ? WHERE id = ?",
                           (pregao, dumps({"resultado": SEM_DADO, "motivo": "sem dado do ativo neste pregão"}), t["id"]))
                counts["sem_dado"] += 1
                continue
            if not holds(cond["tipo"], medido, alvo):
                db.execute("UPDATE triggers SET ultima_avaliacao = ?, ultima_avaliacao_json = ? WHERE id = ?",
                           (pregao, dumps({"resultado": "nao_disparado", "medido": medido, "limiar": alvo,
                                           "distancia_pct": distance(cond["tipo"], medido, alvo)}), t["id"]))
                counts["pendentes"] += 1
                continue
            db.execute("UPDATE triggers SET status = 'DISPARADO', disparado_em = ?, ultima_avaliacao = ?, ultima_avaliacao_json = ? WHERE id = ?",
                       (pregao, pregao, dumps({"resultado": "disparado", "medido": medido, "limiar": alvo}), t["id"]))
            origem = db.scalar("SELECT d.origem FROM plans p JOIN decisions d ON d.id = p.decision_id WHERE p.id = ?", (t["plan_id"],)) or "sistema"
            log_event(db, origem="sistema", tipo="GATILHO_DISPARADO", profile_id=profile_id, pregao=pregao, motivo=t["descricao"],
                      ref={"trigger_id": t["id"], "plan_id": t["plan_id"]}, payload={"medido": medido, "limiar": alvo}, is_mock=is_mock)
            counts["disparados"] += 1
        # a ordem passa pelo agendador e pelas regras como qualquer outra (R4); a chave de idempotência é o gatilho
        tam = acao["tamanho"]
        broker.schedule(OrderRequest(
            profile_id=profile_id, ticker=acao["ticker"], side="COMPRA" if acao["acao"] == "comprar" else "VENDA",
            dimensionamento=tam["dimensionamento"], pregao_decisao=pregao, origem=origem, motivo=f"gatilho #{t['id']}: {t['descricao']}",
            quantidade=int(tam["valor"]) if tam["dimensionamento"] == "quantidade" else None,
            peso_alvo_pct=tam["valor"] if tam["dimensionamento"] == "peso_alvo_pct" else None,
            fracao_posicao=tam["valor"] if tam["dimensionamento"] == "fracao_posicao" else None,
            stop_pct=acao.get("stop_pct"), alvo_pct=acao.get("alvo_pct"), tipo_ordem="GATILHO", idem_key=f"gatilho:{t['id']}",
        ))
    counts["avaliados"] = len(rows)
    return counts
