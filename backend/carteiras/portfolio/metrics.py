"""Métricas de desempenho e risco. Funções puras sobre séries + leitura do banco.

Regra R2: quando não há observações suficientes, a métrica é None (a tela mostra 'sem dado'), nunca um número inventado.
"""

from __future__ import annotations

import math
from typing import Any

from ..config import Config
from ..db import Database

TRADING_DAYS = 252
MIN_OBS_RISK = 20          # observações mínimas para volatilidade, Sharpe, Sortino e correlação
CDI_CODE = 12              # série SGS 12: CDI diário, em % ao dia


def returns_from_levels(levels: list[float]) -> list[float]:
    return [levels[i] / levels[i - 1] - 1 for i in range(1, len(levels)) if levels[i - 1]]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def stdev(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown(levels: list[float]) -> float | None:
    """Maior queda pico-a-vale, em fração negativa (-0.12 = -12%)."""
    if len(levels) < 2:
        return None
    peak, worst = levels[0], 0.0
    for v in levels:
        peak = max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < MIN_OBS_RISK:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (sa * sb)


def series_metrics(levels: list[float], rf_daily: list[float] | None = None) -> dict[str, Any]:
    """Métricas de uma série de níveis (patrimônio, índice). `rf_daily`: retorno diário livre de risco alinhado ao fim da série."""
    rets = returns_from_levels(levels)
    out: dict[str, Any] = {
        "observacoes": len(rets), "retorno_total": None, "retorno_anualizado": None, "volatilidade_anual": None,
        "sharpe": None, "sortino": None, "max_drawdown": None, "melhor_dia": None, "pior_dia": None,
    }
    if len(levels) < 2 or not levels[0]:
        return out
    out["retorno_total"] = levels[-1] / levels[0] - 1
    out["max_drawdown"] = max_drawdown(levels)
    if rets:
        out["melhor_dia"], out["pior_dia"] = max(rets), min(rets)
    if len(rets) < MIN_OBS_RISK:
        return out            # período curto demais para anualizar ou medir risco com honestidade
    out["retorno_anualizado"] = (levels[-1] / levels[0]) ** (TRADING_DAYS / len(rets)) - 1
    sd = stdev(rets)
    out["volatilidade_anual"] = sd * math.sqrt(TRADING_DAYS) if sd is not None else None
    rf = (rf_daily or [])[-len(rets):]
    if len(rf) == len(rets) and sd:
        excess = [r - f for r, f in zip(rets, rf, strict=True)]
        out["sharpe"] = mean(excess) / sd * math.sqrt(TRADING_DAYS)
        downside = [min(0.0, e) for e in excess]
        dd = math.sqrt(sum(d * d for d in downside) / len(downside))
        out["sortino"] = mean(excess) / dd * math.sqrt(TRADING_DAYS) if dd else None
    return out


def trade_stats(db: Database, profile_id: str, until: str | None = None) -> dict[str, Any]:
    where, params = "profile_id = ?", [profile_id]
    if until:
        where += " AND saida <= ?"
        params.append(until)
    rows = db.query(f"SELECT pnl, pnl_pct FROM trades WHERE {where}", params)
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    out: dict[str, Any] = {"operacoes": len(rows), "win_rate": None, "payoff": None, "pnl_realizado": sum(r["pnl"] for r in rows),
                           "ganho_medio": None, "perda_media": None, "expectativa": None}
    if not rows:
        return out
    out["win_rate"] = len(wins) / len(rows)
    if wins:
        out["ganho_medio"] = mean(wins)
    if losses:
        out["perda_media"] = mean(losses)
    if wins and losses:
        out["payoff"] = mean(wins) / abs(mean(losses))
    out["expectativa"] = sum(r["pnl"] for r in rows) / len(rows)
    return out


def benchmark_levels(db: Database, pregoes: list[str]) -> dict[str, list[float | None]]:
    """IBOV (fechamento) e CDI acumulado (base 1 no primeiro pregão), alinhados às datas dadas. Lacuna = None."""
    if not pregoes:
        return {"ibov": [], "cdi": []}
    ibov_map = {r["pregao"]: r["close"] for r in db.query(
        "SELECT pregao, close FROM prices_raw WHERE ticker = 'IBOV' AND pregao BETWEEN ? AND ?", (pregoes[0], pregoes[-1]))}
    cdi_map = {r["data"]: r["valor"] for r in db.query(
        "SELECT data, valor FROM macro_series WHERE codigo = ? AND data BETWEEN ? AND ?", (CDI_CODE, pregoes[0], pregoes[-1]))}
    ibov: list[float | None] = []
    cdi: list[float | None] = []
    base_ibov = ibov_map.get(pregoes[0])
    acc: float | None = 1.0 if cdi_map else None
    for i, p in enumerate(pregoes):
        v = ibov_map.get(p)
        ibov.append(v / base_ibov if v and base_ibov else None)
        if acc is not None and i > 0:
            # CDI do dia entra no acumulado a partir do segundo pregão da janela; sem valor naquele dia, não há dado
            day = cdi_map.get(p)
            acc = acc * (1 + day / 100) if day is not None else None
        cdi.append(acc)
    return {"ibov": ibov, "cdi": cdi}


def _full(xs: list[float | None]) -> list[float] | None:
    return None if any(x is None for x in xs) or not xs else [float(x) for x in xs if x is not None]


def profile_metrics(db: Database, profile_id: str, until: str | None = None) -> dict[str, Any]:
    """Métricas da carteira, do IBOV e do CDI na mesma janela, mais correlações e estatística de operações."""
    sql = "SELECT pregao, patrimonio, retorno_dia, drawdown FROM portfolio_history WHERE profile_id = ?"
    params: list[Any] = [profile_id]
    if until:
        sql += " AND pregao <= ?"
        params.append(until)
    hist = db.query(sql + " ORDER BY pregao", params)
    pregoes = [h["pregao"] for h in hist]
    levels = [h["patrimonio"] for h in hist]
    bench = benchmark_levels(db, pregoes)
    cdi_lv = _full(bench["cdi"])
    cdi_daily = returns_from_levels(cdi_lv) if cdi_lv else None
    ibov_lv = _full(bench["ibov"])
    out: dict[str, Any] = {
        "perfil": profile_id, "de": pregoes[0] if pregoes else None, "ate": pregoes[-1] if pregoes else None,
        "carteira": series_metrics(levels, cdi_daily),
        "ibov": series_metrics(ibov_lv, cdi_daily) if ibov_lv else None,
        "cdi": series_metrics(cdi_lv) if cdi_lv else None,
        "operacoes": trade_stats(db, profile_id, until),
        "correlacao_ibov": None, "correlacao_cdi": None,
    }
    if ibov_lv and len(ibov_lv) == len(levels):
        out["correlacao_ibov"] = correlation(returns_from_levels(levels), returns_from_levels(ibov_lv))
    if cdi_lv and len(cdi_lv) == len(levels):
        out["correlacao_cdi"] = correlation(returns_from_levels(levels), returns_from_levels(cdi_lv))
    return out


def compare_profiles(db: Database, cfg: Config, until: str | None = None) -> dict[str, Any]:
    """A contra B (mesma janela comum), com correlação entre as carteiras."""
    series: dict[str, dict[str, float]] = {}
    for pid in cfg.profile_ids():
        rows = db.query("SELECT pregao, patrimonio FROM portfolio_history WHERE profile_id = ?" + (" AND pregao <= ?" if until else "")
                        + " ORDER BY pregao", (pid, until) if until else (pid,))
        series[pid] = {r["pregao"]: r["patrimonio"] for r in rows}
    out: dict[str, Any] = {"perfis": {pid: profile_metrics(db, pid, until) for pid in cfg.profile_ids()}, "correlacao_a_b": None}
    ids = list(cfg.profile_ids())
    if len(ids) == 2:
        common = sorted(set(series[ids[0]]) & set(series[ids[1]]))
        if len(common) > MIN_OBS_RISK:
            out["correlacao_a_b"] = correlation(returns_from_levels([series[ids[0]][d] for d in common]),
                                                returns_from_levels([series[ids[1]][d] for d in common]))
    return out


def attribution(db: Database, cfg: Config, profile_id: str, pregao: str | None = None) -> dict[str, Any]:
    """Atribuição de resultado por ativo e por setor: realizado + não realizado + proventos − custos. Valores em R$."""
    until = pregao or db.scalar("SELECT MAX(pregao) FROM portfolio_history WHERE profile_id = ?", (profile_id,))
    by_ticker: dict[str, dict[str, float]] = {}

    def slot(t: str) -> dict[str, float]:
        return by_ticker.setdefault(t, {"realizado": 0.0, "nao_realizado": 0.0, "proventos": 0.0, "custos": 0.0})

    for r in db.query("SELECT ticker, SUM(pnl) AS v FROM trades WHERE profile_id = ? GROUP BY ticker", (profile_id,)):
        slot(r["ticker"])["realizado"] = r["v"] or 0.0
    for r in db.query("SELECT ticker, quantidade, custo_medio FROM positions WHERE profile_id = ? AND quantidade > 0", (profile_id,)):
        px = db.scalar("SELECT close FROM prices_raw WHERE ticker = ? AND pregao <= ? ORDER BY pregao DESC LIMIT 1", (r["ticker"], until))
        if px:
            slot(r["ticker"])["nao_realizado"] = r["quantidade"] * (px - r["custo_medio"])
    for r in db.query("SELECT ref_json, valor FROM cash_ledger WHERE profile_id = ? AND tipo = 'PROVENTO'", (profile_id,)):
        import json
        t = (json.loads(r["ref_json"]) if r["ref_json"] else {}).get("ticker")
        if t:
            slot(t)["proventos"] += r["valor"]
    for r in db.query("SELECT ticker, SUM(custos) AS v FROM fills WHERE profile_id = ? GROUP BY ticker", (profile_id,)):
        slot(r["ticker"])["custos"] = -(r["v"] or 0.0)
    by_sector: dict[str, float] = {}
    rows = []
    for t, v in sorted(by_ticker.items()):
        total = v["realizado"] + v["nao_realizado"] + v["proventos"]
        rows.append({"ticker": t, "setor": cfg.universe.setor(t) if t in cfg.universe.ativos else "sem dado", **v, "total": total})
        by_sector[rows[-1]["setor"]] = by_sector.get(rows[-1]["setor"], 0.0) + total
    return {"ate": until, "por_ativo": rows, "por_setor": [{"setor": s, "total": v} for s, v in sorted(by_sector.items())],
            "nota": "Realizado e não realizado já incluem os custos de compra no custo médio e os de venda no preço líquido; "
                    "a coluna 'custos' é informativa e não deve ser somada ao total."}
