"""Montagem do dossiê (entrada do agente). Tudo com referências exatas a fontes e datas (R8). Sem dado → 'sem dado'."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..calendar_b3 import Calendar
from ..config import Config
from ..db import Database, dumps, loads, now_iso
from ..marketdata import bar_raw, bars_adj, bars_raw, macro_latest
from ..memory import store as memory
from ..radar.indicators import atr, avg_volume, highest_high, lowest_low, pct_change, window_return
from ..simulation.state import exposure, last_prices, load_state

SEM_DADO = "sem dado"
SELIC, CDI, IPCA, DOLAR = 432, 12, 433, 1


def _round(v: float | None, n: int = 4) -> float | str:
    return round(v, n) if v is not None else SEM_DADO


def asset_block(db: Database, cfg: Config, profile_id: str, ticker: str, pregao: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    r = cfg.profiles[profile_id].radar
    adj = bars_adj(db, ticker, pregao, max(r.janela_rompimento_longa_dias, 40) + 1)
    sem_dado: list[str] = []
    if len(adj) < 2:
        return {"setor": cfg.universe.setor(ticker) if ticker in cfg.universe.ativos else SEM_DADO, "janela": [], "indicadores": {},
                "sem_dado": ["historico"]}
    ind: dict[str, Any] = {}
    ind["variacao_1d_pct"] = _round(pct_change(adj[-1]["close"], adj[-2]["close"]), 2)
    for k in (5, 20):
        w = window_return(adj, k)
        ind[f"variacao_{k}d_pct"] = _round(w, 2)
        if w is None:
            sem_dado.append(f"variacao_{k}d")
    a = atr(adj[:-1], r.janela_atr_dias)
    ind["atr"] = _round(a)
    avg = avg_volume(adj, r.janela_volume_dias)
    ind["volume_relativo"] = _round((adj[-1]["volume_fin"] or 0) / avg, 2) if avg else SEM_DADO
    ind["maxima_20d"] = _round(highest_high(adj, r.janela_rompimento_curta_dias))
    ind["minima_20d"] = _round(lowest_low(adj, r.janela_rompimento_curta_dias))
    ibov = bars_raw(db, "IBOV", pregao, 2)
    ibov_ret = pct_change(ibov[-1]["close"], ibov[-2]["close"]) if len(ibov) == 2 and ibov[-1]["pregao"] == pregao else None
    ind["variacao_ibov_1d_pct"] = _round(ibov_ret, 2)
    if a is None:
        sem_dado.append("atr")
    if avg is None:
        sem_dado.append("volume_relativo")
    refs.append({"tabela": "prices_adj", "ticker": ticker, "de": adj[max(0, len(adj) - 20)]["pregao"], "ate": adj[-1]["pregao"]})
    return {
        "setor": cfg.universe.setor(ticker) if ticker in cfg.universe.ativos else SEM_DADO,
        "janela": [{k: b[k] for k in ("pregao", "open", "high", "low", "close", "volume_fin")} for b in adj[-20:]],
        "indicadores": ind, "sem_dado": sem_dado,
    }


def position_block(db: Database, profile_id: str, ticker: str, pregao: str, cal: Calendar, refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    pos = load_state(db, profile_id).posicoes.get(ticker)
    if not pos:
        return None
    bar = bar_raw(db, ticker, pregao)
    close = bar["close"] if bar and bar["close"] else None
    refs.append({"tabela": "positions", "profile_id": profile_id, "ticker": ticker})
    return {
        "quantidade": pos.quantidade, "custo_medio": round(pos.custo_medio, 4), "fechamento": close if close else SEM_DADO,
        "pnl_pct": _round((close / pos.custo_medio - 1) * 100, 2) if close else SEM_DADO,
        "pnl_brl": _round(pos.quantidade * (close - pos.custo_medio), 2) if close else SEM_DADO,
        "stop": pos.stop, "alvo": pos.alvo, "aberta_em": pos.aberta_em,
        "pregoes_em_posicao": len(cal.sessions_between(date.fromisoformat(pos.aberta_em), date.fromisoformat(pregao))),
    }


def portfolio_block(db: Database, cfg: Config, profile_id: str, pregao: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    profile = cfg.profiles[profile_id]
    state = load_state(db, profile_id)
    prices = last_prices(db, list(state.posicoes), pregao)
    equity = state.patrimonio(prices)
    exp = exposure(state, prices, cfg.universe) if state.posicoes and equity > 0 else {"ativo": {}, "setor": {}}
    hist = db.one("SELECT drawdown, patrimonio FROM portfolio_history WHERE profile_id = ? AND pregao <= ? ORDER BY pregao DESC LIMIT 1",
                  (profile_id, pregao))
    pst = db.one("SELECT somente_reduzir, pausado FROM profile_state WHERE profile_id = ?", (profile_id,)) or {}
    refs.append({"tabela": "portfolio_history", "profile_id": profile_id, "pregao": pregao})
    lim = profile.limites
    return {
        "patrimonio": _round(equity, 2), "caixa": round(state.caixa, 2), "caixa_pct": _round(state.caixa / equity * 100, 2) if equity > 0 else SEM_DADO,
        "posicoes": len(state.posicoes), "pesos_por_ativo_pct": {k: round(v, 2) for k, v in exp["ativo"].items()},
        "pesos_por_setor_pct": {k: round(v, 2) for k, v in exp["setor"].items()},
        "drawdown_pct": _round(-hist["drawdown"] * 100, 2) if hist else SEM_DADO,
        "somente_reduzir": bool(pst.get("somente_reduzir")), "pausado": bool(pst.get("pausado")),
        "limites": lim.model_dump(), "perfil": {"id": profile.id, "status_parametros": profile.status_parametros},
    }


def macro_block(db: Database, pregao: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for nome, code in (("selic_aa", SELIC), ("cdi_dia_pct", CDI), ("ipca_mes_pct", IPCA), ("dolar", DOLAR)):
        row = macro_latest(db, code, pregao)
        if row:
            out[nome] = {"valor": row["valor"], "data": row["data"], "is_mock": bool(row["is_mock"])}
            refs.append({"tabela": "macro_series", "codigo": code, "data": row["data"]})
        else:
            out[nome] = SEM_DADO
    return out


def build_dossier(
    db: Database, cfg: Config, cal: Calendar, profile_id: str, pregao: str, alerts: list[dict[str, Any]], *,
    tipo: str = "ALERTA", budget: dict[str, Any] | None = None, enrichment: dict[str, Any] | None = None, is_mock: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Monta o dossiê de um (ou de vários) alerta(s) do mesmo ativo. Devolve (conteúdo, referências)."""
    refs: list[dict[str, Any]] = []
    ticker = alerts[0]["ticker"] if alerts else None
    real_ticker = ticker if ticker and ticker != "*" else None
    dossier: dict[str, Any] = {
        "identificacao": {
            "perfil": profile_id, "tipo": tipo, "ticker": ticker, "pregao": pregao, "is_mock": is_mock,
            "alertas": [{"id": a["id"], "tipo": a["tipo"], "severidade": a["severidade"], "medido": a["medido"],
                         "limiar": a["limiar"], "detalhe": a.get("detalhe") or loads(a.get("detalhe_json"), {})} for a in alerts],
        },
        "carteira": portfolio_block(db, cfg, profile_id, pregao, refs),
        "macro": macro_block(db, pregao, refs),
        "planos_pendentes": [],
        "decisoes_recentes": memory.recent_decisions(db, profile_id, ticker=real_ticker, before=pregao, limit=3),
        "licoes_relevantes": memory.lessons(db, profile_id, limit=3),
        "orcamento": budget or {},
        "enriquecimento": enrichment or {"status": "nao_realizado"},
    }
    for a in alerts:
        refs.append({"tabela": "alerts", "id": a["id"], **(a.get("refs") or loads(a.get("refs_json"), {}))})
    if real_ticker:
        dossier["ativo"] = asset_block(db, cfg, profile_id, real_ticker, pregao, refs)
        dossier["posicao"] = position_block(db, profile_id, real_ticker, pregao, cal, refs)
        docs = db.query("SELECT protocolo, categoria, assunto, data_entrega, link, source FROM filings WHERE ticker = ? AND data_entrega <= ? "
                        "ORDER BY data_entrega DESC LIMIT 5", (real_ticker, pregao))
        dossier["documentos_cvm"] = docs
        refs += [{"tabela": "filings", "protocolo": d["protocolo"], "fonte": d["source"]} for d in docs]
        events = db.query("SELECT tipo, data_ex, valor, fator, source FROM corporate_events WHERE ticker = ? AND data_ex >= ? ORDER BY data_ex LIMIT 5",
                          (real_ticker, pregao))
        dossier["eventos_corporativos_proximos"] = events
        dossier["planos_pendentes"] = [
            {"trigger_id": t["id"], "descricao": t["descricao"], "condicao": t["condicao"], "acao": t["acao"], "ultima_avaliacao": t["avaliacao"]}
            for t in memory.pending_triggers(db, profile_id, real_ticker)
        ]
    else:
        dossier["ativo"] = None
        dossier["posicao"] = None
        dossier["documentos_cvm"] = []
        dossier["eventos_corporativos_proximos"] = []
        dossier["planos_pendentes"] = [
            {"trigger_id": t["id"], "descricao": t["descricao"], "condicao": t["condicao"], "acao": t["acao"], "ultima_avaliacao": t["avaliacao"]}
            for t in memory.pending_triggers(db, profile_id)
        ]
    if tipo == "REVISAO":
        state = load_state(db, profile_id)
        dossier["posicoes_detalhe"] = [
            {"ticker": t, "posicao": position_block(db, profile_id, t, pregao, cal, refs),
             "indicadores": asset_block(db, cfg, profile_id, t, pregao, refs)["indicadores"]}
            for t in sorted(state.posicoes)
        ]
    return dossier, refs


def save_dossier(db: Database, profile_id: str, ticker: str | None, pregao: str, tipo: str, content: dict[str, Any],
                 refs: list[dict[str, Any]], *, is_mock: bool) -> int:
    return db.insert("dossiers", {
        "profile_id": profile_id, "ticker": ticker if ticker != "*" else None, "pregao": pregao, "tipo": tipo,
        "conteudo_json": dumps(content), "refs_json": dumps(refs), "is_mock": int(is_mock), "created_at": now_iso(),
    })
