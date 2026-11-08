"""Radar de alertas: roda todo dia sobre carteira + watchlist, com limiares vindos do perfil.

Cada alerta guarda o valor medido, o limiar e a referência do dado (R8). Cooldown evita reescalar o mesmo
(ativo, tipo) dentro de N pregões, a menos que a severidade suba.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..calendar_b3 import Calendar
from ..config import Config, Profile
from ..db import Database, dumps, loads, now_iso
from ..marketdata import bar_raw, bars_adj, bars_raw, last_raw_before
from ..simulation.state import PortfolioState, exposure, last_prices, load_state
from .indicators import atr, avg_volume, highest_high, lowest_low, pct_change, window_return

SEV_RANK = {"INFO": 0, "ATENCAO": 1, "CRITICO": 2}


@dataclass
class AlertDraft:
    ticker: str
    tipo: str
    severidade: str
    medido: float | None
    limiar: float | None
    detalhe: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)


def severity(value: float, attention: float, critical: float | None, mult: float) -> str | None:
    """ATENCAO ao passar do limiar de atenção; CRITICO ao passar do crítico (ou de atenção x mult_critico)."""
    crit = critical if critical is not None else attention * mult
    if value >= crit:
        return "CRITICO"
    if value >= attention:
        return "ATENCAO"
    return None


def _ref(ticker: str, pregao: str, fonte: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"tabela": "prices_adj", "ticker": ticker, "pregao": pregao, "fonte": fonte, **(extra or {})}


def ticker_alerts(
    profile: Profile, ticker: str, pregao: str, adj: list[dict[str, Any]], raw_today: dict[str, Any] | None,
    ibov_ret: float | None, position: Any, long_history_ok: bool,
) -> tuple[list[AlertDraft], list[str]]:
    """Alertas de preço e volume de um ativo. Devolve (alertas, indicadores sem dado)."""
    r = profile.radar
    out: list[AlertDraft] = []
    missing: list[str] = []
    if len(adj) < 2 or adj[-1]["pregao"] != pregao:
        return out, ["historico_curto"]
    cur, prev = adj[-1], adj[-2]
    src = (raw_today or {}).get("source", "?")
    ref = _ref(ticker, pregao, src)

    ret = pct_change(cur["close"], prev["close"])
    if ret is not None:
        sev = severity(abs(ret), r.variacao_diaria_atencao_pct, r.variacao_diaria_critico_pct, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "VARIACAO_DIARIA", sev, ret, r.variacao_diaria_atencao_pct,
                                  {"direcao": "queda" if ret < 0 else "alta", "variacao_pct": round(ret, 2)}, ref))

    w = window_return(adj, r.janela_variacao_dias)
    if w is not None:
        sev = severity(abs(w), r.variacao_janela_atencao_pct, None, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "VARIACAO_JANELA", sev, w, r.variacao_janela_atencao_pct,
                                  {"janela_pregoes": r.janela_variacao_dias, "direcao": "queda" if w < 0 else "alta"}, ref))

    avg_v = avg_volume(adj, r.janela_volume_dias)
    if avg_v:
        rel = (cur["volume_fin"] or 0) / avg_v
        sev = severity(rel, r.volume_relativo_atencao, r.volume_relativo_critico, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "VOLUME_ANORMAL", sev, rel, r.volume_relativo_atencao,
                                  {"volume_medio_20d": round(avg_v, 2), "variacao_dia_pct": round(ret or 0, 2)}, ref))
    else:
        missing.append("volume_medio")

    gap = pct_change(cur["open"], prev["close"])
    if gap is not None:
        sev = severity(abs(gap), r.gap_abertura_atencao_pct, None, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "GAP_ABERTURA", sev, gap, r.gap_abertura_atencao_pct,
                                  {"direcao": "queda" if gap < 0 else "alta"}, ref))

    a = atr(adj[:-1], r.janela_atr_dias)
    if a:
        rng = (cur["high"] - cur["low"]) / a
        if rng >= r.range_vs_atr_atencao:
            out.append(AlertDraft(ticker, "RANGE_ANORMAL", "ATENCAO", rng, r.range_vs_atr_atencao, {"atr_14": round(a, 4)}, ref))
    else:
        missing.append("atr")

    if ret is not None and ibov_ret is not None:
        rel_ret = ret - ibov_ret
        sev = severity(-rel_ret, r.queda_vs_ibov_atencao_pct, None, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "QUEDA_VS_IBOV", sev, rel_ret, -r.queda_vs_ibov_atencao_pct,
                                  {"variacao_ativo_pct": round(ret, 2), "variacao_ibov_pct": round(ibov_ret, 2)}, ref))

    short = highest_high(adj, r.janela_rompimento_curta_dias)
    if short is not None and cur["close"] > short:
        out.append(AlertDraft(ticker, "ROMPIMENTO_ALTA_20", "ATENCAO", cur["close"], short, {"janela_pregoes": r.janela_rompimento_curta_dias}, ref))
    low_s = lowest_low(adj, r.janela_rompimento_curta_dias)
    if low_s is not None and cur["close"] < low_s:
        out.append(AlertDraft(ticker, "ROMPIMENTO_BAIXA_20", "ATENCAO", cur["close"], low_s, {"janela_pregoes": r.janela_rompimento_curta_dias}, ref))
    if long_history_ok:
        hi = highest_high(adj, r.janela_rompimento_longa_dias)
        if hi is not None and cur["close"] > hi:
            out.append(AlertDraft(ticker, "MAXIMA_52S", "ATENCAO", cur["close"], hi, {"janela_pregoes": r.janela_rompimento_longa_dias}, ref))
        lo = lowest_low(adj, r.janela_rompimento_longa_dias)
        if lo is not None and cur["close"] < lo:
            out.append(AlertDraft(ticker, "MINIMA_52S", "ATENCAO", cur["close"], lo, {"janela_pregoes": r.janela_rompimento_longa_dias}, ref))
    else:
        missing.append("janela_52_semanas")

    if position is not None and raw_today is not None:
        close = raw_today["close"]
        if position.stop:
            dist = (close - position.stop) / close * 100
            if dist <= r.proximidade_stop_atencao_pct:
                sev = "CRITICO" if dist <= r.proximidade_stop_atencao_pct / 2 else "ATENCAO"
                out.append(AlertDraft(ticker, "PROXIMIDADE_STOP", sev, dist, r.proximidade_stop_atencao_pct,
                                      {"stop": position.stop, "fechamento": close}, _ref(ticker, pregao, src) | {"tabela": "prices_raw"}))
        if position.alvo:
            dist = (position.alvo - close) / close * 100
            if 0 <= dist <= r.proximidade_alvo_atencao_pct:
                out.append(AlertDraft(ticker, "PROXIMIDADE_ALVO", "ATENCAO", dist, r.proximidade_alvo_atencao_pct,
                                      {"alvo": position.alvo, "fechamento": close}, _ref(ticker, pregao, src) | {"tabela": "prices_raw"}))
        loss = (position.custo_medio - close) / position.custo_medio * 100
        sev = severity(loss, r.drawdown_posicao_atencao_pct, None, r.mult_critico)
        if sev:
            out.append(AlertDraft(ticker, "DRAWDOWN_POSICAO", sev, loss, r.drawdown_posicao_atencao_pct,
                                  {"custo_medio": round(position.custo_medio, 4), "fechamento": close}, _ref(ticker, pregao, src) | {"tabela": "prices_raw"}))
    return out, missing


def _rank_ok(new: str, old: str) -> bool:
    return SEV_RANK[new] > SEV_RANK[old]


def run_radar(db: Database, cfg: Config, cal: Calendar, profile_id: str, pregao: str, *, is_mock: bool) -> list[dict[str, Any]]:
    """Calcula e grava os alertas do pregão. Idempotente: reprocessar o mesmo pregão não duplica."""
    profile = cfg.profiles[profile_id]
    r = profile.radar
    state = load_state(db, profile_id)
    watch = [t for t in profile.universo.tickers if t not in state.posicoes]
    tickers = list(state.posicoes) + watch
    drafts: list[AlertDraft] = []
    missing: dict[str, list[str]] = {}

    ibov = bars_raw(db, "IBOV", pregao, 3)
    ibov_ret = pct_change(ibov[-1]["close"], ibov[-2]["close"]) if len(ibov) >= 2 and ibov[-1]["pregao"] == pregao else None

    falling = counted = 0
    for t in tickers:
        adj = bars_adj(db, t, pregao, r.janela_rompimento_longa_dias + 2)
        raw = bar_raw(db, t, pregao)
        held = t in state.posicoes
        # Qualidade do dado primeiro: sem dado de um ativo em carteira, nada mais é confiável (R2)
        if held and (raw is None or not raw["close"] or raw["close"] <= 0 or raw["high"] < raw["low"]):
            last = last_raw_before(db, t, pregao)
            drafts.append(AlertDraft(t, "DADO_AUSENTE" if raw is None else "DADO_ANOMALO", "CRITICO", None, None,
                                     {"ultimo_dado": last["pregao"] if last else None},
                                     {"tabela": "prices_raw", "ticker": t, "pregao": pregao, "fonte": "ausente"}))
            continue
        if raw is None:
            continue
        long_ok = len(adj) >= r.janela_rompimento_longa_dias + 1
        found, miss = ticker_alerts(profile, t, pregao, adj, raw, ibov_ret, state.posicoes.get(t), long_ok)
        drafts += found
        for m in miss:
            missing.setdefault(m, []).append(t)
        if len(adj) >= 2:
            counted += 1
            chg = pct_change(adj[-1]["close"], adj[-2]["close"])
            if chg is not None and chg <= -r.mercado_queda_forte_pct:
                falling += 1
        # salto sem evento corporativo explicando (série bruta)
        prev_raw = last_raw_before(db, t, pregao)
        if prev_raw and prev_raw["close"]:
            jump = pct_change(raw["close"], prev_raw["close"])
            if jump is not None and abs(jump) >= r.salto_sem_evento_pct:
                has_event = db.scalar("SELECT COUNT(*) FROM corporate_events WHERE ticker = ? AND data_ex BETWEEN ? AND ?",
                                      (t, prev_raw["pregao"], pregao), 0)
                if not has_event:
                    drafts.append(AlertDraft(t, "EVENTO_CORPORATIVO", "CRITICO", jump, r.salto_sem_evento_pct,
                                             {"motivo": "salto de preço sem evento corporativo registrado; verificar ajuste"},
                                             {"tabela": "prices_raw", "ticker": t, "pregao": pregao, "fonte": raw["source"]}))
        # documentos da CVM do dia
        for f in db.query("SELECT * FROM filings WHERE ticker = ? AND data_entrega = ?", (t, pregao)):
            drafts.append(AlertDraft(t, "DOCUMENTO_CVM", "ATENCAO" if held else "INFO", None, None,
                                     {"categoria": f["categoria"], "assunto": f["assunto"], "link": f["link"]},
                                     {"tabela": "filings", "protocolo": f["protocolo"], "fonte": f["source"]}))
        # data-com próxima em ativo da carteira
        if held:
            limit = cal.add_sessions(date.fromisoformat(pregao), r.dias_para_data_com_atencao).isoformat()
            for ev in db.query("SELECT * FROM corporate_events WHERE ticker = ? AND data_ex > ? AND data_ex <= ?", (t, pregao, limit)):
                drafts.append(AlertDraft(t, "EVENTO_AGENDADO", "INFO", ev["valor"], None,
                                         {"tipo_evento": ev["tipo"], "data_ex": ev["data_ex"], "valor": ev["valor"], "fator": ev["fator"]},
                                         {"tabela": "corporate_events", "ticker": t, "data_ex": ev["data_ex"], "fonte": ev["source"]}))

    # mercado
    if ibov_ret is not None and abs(ibov_ret) >= r.ibov_variacao_critica_pct:
        drafts.append(AlertDraft("*", "MERCADO_IBOV", "CRITICO", ibov_ret, r.ibov_variacao_critica_pct, {"variacao_ibov_pct": round(ibov_ret, 2)},
                                 {"tabela": "prices_raw", "ticker": "IBOV", "pregao": pregao, "fonte": ibov[-1]["source"]}))
    if counted and falling / counted * 100 >= r.mercado_fracao_universo_pct:
        drafts.append(AlertDraft("*", "MERCADO_AMPLITUDE", "CRITICO", falling / counted * 100, r.mercado_fracao_universo_pct,
                                 {"ativos_em_queda_forte": falling, "ativos_analisados": counted, "queda_forte_pct": r.mercado_queda_forte_pct},
                                 {"tabela": "prices_adj", "pregao": pregao, "fonte": "universo do perfil"}))

    # limites da carteira
    prices = last_prices(db, list(state.posicoes), pregao)
    drafts += _limit_alerts(db, cfg, profile, state, prices, pregao)

    if missing:
        from ..eventlog import log_event
        log_event(db, origem="sistema", tipo="RADAR_SEM_DADO", profile_id=profile_id, pregao=pregao,
                  motivo="indicadores sem histórico suficiente; alertas correspondentes não foram gerados",
                  payload={k: len(v) for k, v in missing.items()}, is_mock=is_mock)
    return _persist(db, cal, profile, state, drafts, pregao, is_mock)


def _limit_alerts(db: Database, cfg: Config, profile: Profile, state: PortfolioState, prices: dict[str, float], pregao: str) -> list[AlertDraft]:
    out: list[AlertDraft] = []
    lim = profile.limites
    equity = state.patrimonio(prices)
    if equity <= 0 or not state.posicoes:
        return out
    exp = exposure(state, prices, cfg.universe)
    ref = {"tabela": "positions", "pregao": pregao, "fonte": "estado da carteira"}
    for t, w in exp["ativo"].items():
        if w > lim.max_por_posicao_pct + 0.01:
            out.append(AlertDraft(t, "LIMITE_POSICAO", "ATENCAO", w, lim.max_por_posicao_pct, {"peso_pct": round(w, 2)}, ref))
    for s, w in exp["setor"].items():
        if w > lim.max_por_setor_pct + 0.01:
            out.append(AlertDraft("*", f"LIMITE_SETOR:{s}", "ATENCAO", w, lim.max_por_setor_pct, {"setor": s, "peso_pct": round(w, 2)}, ref))
    cash_pct = state.caixa / equity * 100
    if cash_pct < lim.caixa_minimo_pct - 0.01:
        out.append(AlertDraft("*", "LIMITE_CAIXA", "ATENCAO", cash_pct, lim.caixa_minimo_pct, {"caixa_pct": round(cash_pct, 2)}, ref))
    h = db.one("SELECT drawdown FROM portfolio_history WHERE profile_id = ? AND pregao = ?", (profile.id, pregao))
    if h:
        dd = -h["drawdown"] * 100
        sev = "CRITICO" if dd >= lim.drawdown_max_carteira_pct else "ATENCAO" if dd >= profile.radar.drawdown_carteira_atencao_pct else None
        if sev:
            out.append(AlertDraft("*", "DRAWDOWN_CARTEIRA", sev, dd, profile.radar.drawdown_carteira_atencao_pct,
                                  {"drawdown_pct": round(dd, 2), "maximo_pct": lim.drawdown_max_carteira_pct},
                                  {"tabela": "portfolio_history", "pregao": pregao, "fonte": "marcação a mercado"}))
    return out


def _persist(db: Database, cal: Calendar, profile: Profile, state: PortfolioState, drafts: list[AlertDraft],
             pregao: str, is_mock: bool) -> list[dict[str, Any]]:
    cooldown = profile.radar.cooldown_pregoes
    with db.transaction():
        for d in drafts:
            prior = db.one(
                "SELECT pregao, severidade FROM alerts WHERE profile_id = ? AND ticker = ? AND tipo = ? AND pregao < ? "
                "AND cooldown = 0 ORDER BY pregao DESC LIMIT 1", (profile.id, d.ticker, d.tipo, pregao))
            suppressed = 0
            if prior:
                gap = len(cal.sessions_between(date.fromisoformat(prior["pregao"]), date.fromisoformat(pregao)))
                if gap <= cooldown and not _rank_ok(d.severidade, prior["severidade"]):
                    suppressed = 1
            db.insert("alerts", {
                "profile_id": profile.id, "ticker": d.ticker, "tipo": d.tipo, "severidade": d.severidade, "pregao": pregao,
                "medido": d.medido, "limiar": d.limiar, "detalhe_json": dumps(d.detalhe), "refs_json": dumps(d.refs),
                "cooldown": suppressed, "em_carteira": int(d.ticker in state.posicoes), "is_mock": int(is_mock),
                "created_at": now_iso(),
            }, or_ignore=True)
    rows = db.query("SELECT * FROM alerts WHERE profile_id = ? AND pregao = ? ORDER BY id", (profile.id, pregao))
    for row in rows:
        row["detalhe"] = loads(row["detalhe_json"], {})
        row["refs"] = loads(row["refs_json"], {})
    return rows
