"""Consultas de leitura do dashboard. Funções puras sobre o banco: nada aqui escreve. Sem dado → 'sem dado' / None (R2)."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from ..bootstrap import App
from ..config import Config
from ..db import Database, loads
from ..escalation.budget import BudgetManager
from ..marketdata import available_until, bars_adj
from ..memory import store as memory
from ..portfolio import metrics as pm
from ..simulation.state import exposure, last_prices, load_state

SEM_DADO = "sem dado"


def _loads_cols(row: dict[str, Any], *cols: str) -> dict[str, Any]:
    for c in cols:
        key = c[:-5] if c.endswith("_json") else c
        row[key] = loads(row.get(c), None)
    return row


def data_day(db: Database) -> str | None:
    return db.scalar("SELECT MAX(pregao) FROM portfolio_history") or available_until(db)


def rebase(levels: list[float | None], base: float | None = None) -> list[float | None]:
    ref = base if base is not None else next((x for x in levels if x), None)
    return [round(x / ref * 100, 4) if x is not None and ref else None for x in levels]


# ---------------------------------------------------------------------------------------------- meta e visão geral
def meta(app: App) -> dict[str, Any]:
    db = app.db
    ate = data_day(db)
    ok = db.scalar("SELECT MAX(ultimo_ok) FROM data_sources")
    last_run = db.one("SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT 1")
    nxt = app.cal.next_day(date.fromisoformat(ate)).isoformat() if ate else None
    return {
        "modo": app.mode, "is_mock": app.is_mock, "agente": {"nome": app.agent.nome, "origem": app.agent.origem, "is_mock": app.agent.is_mock},
        "perfis": [{"id": p.id, "nome": p.nome, "descricao": p.descricao, "status_parametros": p.status_parametros,
                    "provisorio": p.provisorio, "capital_inicial": p.capital_inicial,
                    "iniciada": bool(db.scalar("SELECT iniciado_em FROM profile_state WHERE profile_id = ?", (p.id,)))}
                   for p in app.cfg.profiles.values()],
        "dados_ate": ate, "coletado_em": ok, "proximo_pregao": nxt, "hoje": date.today().isoformat(),
        "hoje_e_pregao": app.cal.is_trading_day(date.today()),
        "ultima_rodada": last_run,
        "proximo_ciclo": "após o fechamento do próximo pregão, quando houver dados novos (rodada manual: aba Controles)",
        "universo": {"indice": app.cfg.universe.indice.codigo, "tickers": sorted(app.cfg.universe.all_tickers())},
    }


def _month_start_level(db: Database, pid: str, until: str) -> float | None:
    """Patrimônio do último pregão do mês anterior (ou o capital inicial, se a carteira nasceu neste mês)."""
    prev = db.scalar("SELECT patrimonio FROM portfolio_history WHERE profile_id = ? AND pregao < ? ORDER BY pregao DESC LIMIT 1",
                     (pid, until[:8] + "01"))
    if prev is not None:
        return float(prev)
    return db.scalar("SELECT patrimonio FROM portfolio_history WHERE profile_id = ? ORDER BY pregao LIMIT 1", (pid,))


def overview(app: App) -> dict[str, Any]:
    db = app.db
    out: dict[str, Any] = {"dados_ate": data_day(db), "perfis": {}}
    for pid in app.cfg.profile_ids():
        h = db.one("SELECT * FROM portfolio_history WHERE profile_id = ? ORDER BY pregao DESC LIMIT 1", (pid,))
        if not h:
            out["perfis"][pid] = {"iniciada": False}
            continue
        st = db.one("SELECT * FROM profile_state WHERE profile_id = ?", (pid,)) or {}
        met = pm.profile_metrics(db, pid)
        cap = float(st.get("capital_inicial") or app.cfg.profiles[pid].capital_inicial)
        m0 = _month_start_level(db, pid, h["pregao"])
        last_alert = db.one("SELECT id, tipo, ticker, severidade, pregao, is_mock FROM alerts WHERE profile_id = ? AND cooldown = 0 "
                            "ORDER BY pregao DESC, id DESC LIMIT 1", (pid,))
        last_dec = db.one("SELECT id, tipo, ticker, origem, pregao, is_mock FROM decisions WHERE profile_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        out["perfis"][pid] = {
            "iniciada": True, "patrimonio": h["patrimonio"], "caixa": h["caixa"], "posicoes_valor": h["posicoes_valor"],
            "pnl_dia_pct": (h["retorno_dia"] or 0.0) * 100, "pnl_mes_pct": (h["patrimonio"] / m0 - 1) * 100 if m0 else None,
            "pnl_total_pct": (h["patrimonio"] / cap - 1) * 100, "pnl_total_brl": h["patrimonio"] - cap,
            "drawdown_pct": -h["drawdown"] * 100, "pico": h["pico"], "defasados": h["defasados"],
            "estado": "somente_reduzir" if st.get("somente_reduzir") else "pausada" if st.get("pausado") else "normal",
            "ibov_total_pct": (met["ibov"]["retorno_total"] * 100) if met["ibov"] and met["ibov"]["retorno_total"] is not None else None,
            "cdi_total_pct": (met["cdi"]["retorno_total"] * 100) if met["cdi"] and met["cdi"]["retorno_total"] is not None else None,
            "ultimo_alerta": last_alert, "ultima_decisao": last_dec, "pregao": h["pregao"], "is_mock": h["is_mock"],
            "ultimo_pregao_processado": st.get("ultimo_pregao"),
        }
    return out


# ---------------------------------------------------------------------------------------------- carteiras
def series(db: Database, pids: list[str]) -> dict[str, Any]:
    """Curvas de patrimônio das carteiras, IBOV e CDI, na janela comum, rebaseadas em 100."""
    per = {pid: {r["pregao"]: r["patrimonio"] for r in db.query("SELECT pregao, patrimonio FROM portfolio_history WHERE profile_id = ? ORDER BY pregao", (pid,))}
           for pid in pids}
    days = sorted(set.intersection(*[set(v) for v in per.values()])) if per and all(per.values()) else []
    out: dict[str, Any] = {"pregoes": days}
    if not days:
        return out
    bench = pm.benchmark_levels(db, days)
    for pid in pids:
        out[pid] = rebase([per[pid][d] for d in days])
    out["IBOV"] = rebase(bench["ibov"]) if any(x is not None for x in bench["ibov"]) else None
    out["CDI"] = rebase(bench["cdi"]) if any(x is not None for x in bench["cdi"]) else None
    return out


def portfolio(app: App, pid: str) -> dict[str, Any]:
    db, cfg = app.db, app.cfg
    until = data_day(db)
    state = load_state(db, pid)
    prices = last_prices(db, list(state.posicoes), until or "9999-12-31")
    equity = state.patrimonio(prices)
    rows = []
    for t, p in sorted(state.posicoes.items()):
        bar = db.one("SELECT pregao, close FROM prices_raw WHERE ticker = ? AND pregao <= ? ORDER BY pregao DESC LIMIT 1", (t, until))
        px = bar["close"] if bar else None
        val = p.quantidade * px if px else None
        rows.append({
            "ticker": t, "setor": cfg.universe.setor(t) if t in cfg.universe.ativos else SEM_DADO, "quantidade": p.quantidade,
            "entrada": p.custo_medio, "atual": px, "valor": val, "peso_pct": val / equity * 100 if val and equity else None,
            "pnl_brl": p.quantidade * (px - p.custo_medio) if px else None, "pnl_pct": (px / p.custo_medio - 1) * 100 if px else None,
            "stop": p.stop, "alvo": p.alvo, "aberta_em": p.aberta_em,
            "pregoes_em_posicao": len(app.cal.sessions_between(date.fromisoformat(p.aberta_em), date.fromisoformat(until))) if until else None,
            "defasado": bool(bar and until and bar["pregao"] < until), "preco_em": bar["pregao"] if bar else None,
        })
    exp = exposure(state, prices, cfg.universe) if state.posicoes and equity > 0 else {"ativo": {}, "setor": {}}
    caixa_pct = state.caixa / equity * 100 if equity > 0 else None
    return {
        "perfil": pid, "dados_ate": until, "patrimonio": equity, "caixa": state.caixa, "caixa_pct": caixa_pct, "posicoes": rows,
        "alocacao_ativo": [{"nome": k, "pct": v} for k, v in sorted(exp["ativo"].items(), key=lambda kv: -kv[1])] + ([{"nome": "Caixa", "pct": caixa_pct}] if caixa_pct is not None else []),
        "alocacao_setor": [{"nome": k, "pct": v} for k, v in sorted(exp["setor"].items(), key=lambda kv: -kv[1])] + ([{"nome": "Caixa", "pct": caixa_pct}] if caixa_pct is not None else []),
        "curva": series(db, [pid]),
    }


def compare(app: App) -> dict[str, Any]:
    ids = app.cfg.profile_ids()
    data = pm.compare_profiles(app.db, app.cfg)
    return {"metricas": data["perfis"], "correlacao_a_b": data["correlacao_a_b"], "curva": series(app.db, ids), "perfis": ids}


# ---------------------------------------------------------------------------------------------- radar
def radar(app: App, pid: str, severidade: str | None = None, limit: int = 400) -> dict[str, Any]:
    db = app.db
    sql = ("SELECT a.*, e.status AS esc_status, e.motivo AS esc_motivo, e.decision_id, e.dossier_id FROM alerts a "
           "LEFT JOIN escalations e ON e.alert_id = a.id WHERE a.profile_id = ?")
    params: list[Any] = [pid]
    if severidade:
        sql += " AND a.severidade = ?"
        params.append(severidade)
    rows = db.query(sql + " ORDER BY a.pregao DESC, a.id DESC LIMIT ?", (*params, limit))
    for r in rows:
        _loads_cols(r, "detalhe_json", "refs_json")
    until = data_day(db)
    pending = db.query("SELECT e.ticker, e.motivo, e.pregao FROM escalations e WHERE e.profile_id = ? AND e.status = 'ADIADO_ORCAMENTO'", (pid,))
    counts = db.query("SELECT severidade, COUNT(*) n FROM alerts WHERE profile_id = ? AND cooldown = 0 GROUP BY severidade", (pid,))
    esc = db.query("SELECT status, COUNT(*) n FROM escalations WHERE profile_id = ? GROUP BY status", (pid,))
    budget = BudgetManager(db, app.cfg, today=date.fromisoformat(until) if until else None).snapshot()
    return {"perfil": pid, "alertas": rows, "contagem_severidade": counts, "escalonamentos": esc, "adiados_pendentes": pending,
            "cooldown_pregoes": app.cfg.profiles[pid].radar.cooldown_pregoes, "orcamento": budget,
            "limiares": app.cfg.profiles[pid].radar.model_dump()}


# ---------------------------------------------------------------------------------------------- mercado
def _sma(values: list[float], n: int) -> list[float | None]:
    return [sum(values[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(values))]


def _vol(rets: list[float], n: int = 20) -> float | None:
    if len(rets) < n:
        return None
    sd = pm.stdev(rets[-n:])
    return sd * math.sqrt(252) * 100 if sd is not None else None


def market(app: App, ticker: str, pid: str | None = None, n: int = 120) -> dict[str, Any]:
    db = app.db
    until = data_day(db) or "9999-12-31"
    adj = bars_adj(db, ticker, until, n)
    closes = [b["close"] for b in adj]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    first = adj[0]["pregao"] if adj else "0000-00-00"
    markers: list[dict[str, Any]] = []
    for a in db.query("SELECT id, pregao, tipo, severidade, profile_id FROM alerts WHERE ticker = ? AND cooldown = 0 AND pregao >= ?" +
                      (" AND profile_id = ?" if pid else ""), (ticker, first, *([pid] if pid else []))):
        markers.append({"tipo": "alerta", "pregao": a["pregao"], "rotulo": f"{a['tipo']} ({a['severidade']})", "id": a["id"], "perfil": a["profile_id"]})
    for d in db.query("SELECT id, pregao, tipo, origem, profile_id FROM decisions WHERE ticker = ? AND pregao >= ?" +
                      (" AND profile_id = ?" if pid else ""), (ticker, first, *([pid] if pid else []))):
        markers.append({"tipo": "decisao", "pregao": d["pregao"], "rotulo": f"decisão {d['tipo']} ({d['origem']})", "id": d["id"], "perfil": d["profile_id"]})
    for f in db.query("SELECT id, pregao, lado, quantidade, preco, profile_id FROM fills WHERE ticker = ? AND pregao >= ?" +
                      (" AND profile_id = ?" if pid else ""), (ticker, first, *([pid] if pid else []))):
        markers.append({"tipo": "ordem", "pregao": f["pregao"], "rotulo": f"{f['lado']} {f['quantidade']} a {f['preco']:.2f}", "id": f["id"], "perfil": f["profile_id"]})
    rets = pm.returns_from_levels(closes)
    ibov = bars_adj(db, "IBOV", until, 60)
    ibov_closes = [b["close"] for b in ibov]
    ibov_sma50 = _sma(ibov_closes, 50)[-1] if len(ibov_closes) >= 50 else None
    regime = SEM_DADO
    if ibov_sma50 and ibov_closes:
        gap = ibov_closes[-1] / ibov_sma50 - 1
        regime = "alta" if gap > 0.02 else "baixa" if gap < -0.02 else "lateral"
    macro: dict[str, Any] = {}
    for nome, cod in (("cdi_dia_pct", 12), ("selic_aa", 432), ("ipca_mes_pct", 433), ("dolar", 1)):
        row = db.one("SELECT data, valor, is_mock FROM macro_series WHERE codigo = ? ORDER BY data DESC LIMIT 1", (cod,))
        macro[nome] = row or SEM_DADO
    return {
        "ticker": ticker, "dados_ate": until, "setor": app.cfg.universe.setor(ticker) if ticker in app.cfg.universe.ativos else None,
        "candles": [{**{k: b[k] for k in ("pregao", "open", "high", "low", "close", "volume_fin")}, "sma20": sma20[i], "sma50": sma50[i]}
                    for i, b in enumerate(adj)],
        "marcadores": sorted(markers, key=lambda m: m["pregao"]),
        "volatilidade_20d_pct": _vol(rets), "volatilidade_ibov_20d_pct": _vol(pm.returns_from_levels(ibov_closes)), "regime_mercado": regime,
        "documentos_cvm": db.query("SELECT id, protocolo, categoria, assunto, data_entrega, link, is_mock FROM filings WHERE ticker = ? ORDER BY data_entrega DESC LIMIT 15", (ticker,)),
        "eventos_corporativos": db.query("SELECT id, tipo, data_ex, valor, fator, source, is_mock FROM corporate_events WHERE ticker = ? ORDER BY data_ex DESC LIMIT 15", (ticker,)),
        "macro": macro,
        "noticias": {"status": "encaixe", "mensagem": "As notícias (RSS filtrado por emissor) entram na etapa do agente de IA. Nada é inventado aqui."},
    }


# ---------------------------------------------------------------------------------------------- decisões, mente, planos, memória
def _decision_summary(r: dict[str, Any]) -> dict[str, Any]:
    reg = loads(r["registro_json"], {})
    return {"id": r["id"], "pregao": r["pregao"], "ticker": r["ticker"], "tipo": r["tipo"], "origem": r["origem"], "agente": r["agente"],
            "is_mock": r["is_mock"], "confianca": (reg.get("confianca") or {}).get("valor"),
            "resumo": (reg.get("leitura_de_mercado") or "")[:220],
            "acoes": [{"ticker": d.get("ticker"), "acao": d.get("acao")} for d in reg.get("decisao", [])]}


def decisions(app: App, pid: str, tipo: str | None = None, ticker: str | None = None, origem: str | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM decisions WHERE profile_id = ?", [pid]
    for col, val in (("tipo", tipo), ("ticker", ticker), ("origem", origem)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    return [_decision_summary(r) for r in app.db.query(sql + " ORDER BY pregao DESC, id DESC LIMIT ?", (*params, limit))]


def decision_detail(app: App, decision_id: int) -> dict[str, Any] | None:
    db = app.db
    d = db.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
    if not d:
        return None
    dossier = db.one("SELECT * FROM dossiers WHERE id = ?", (d["dossier_id"],)) if d["dossier_id"] else None
    orders = db.query("SELECT * FROM orders WHERE decision_id = ? ORDER BY id", (decision_id,))
    for o in orders:
        o["fills"] = db.query("SELECT * FROM fills WHERE order_id = ? ORDER BY id", (o["id"],))
        o["regras"] = db.query("SELECT regra, resultado, motivo FROM rule_events WHERE order_id = ?", (o["id"],))
    positions = {p["ticker"]: p for p in db.query("SELECT ticker, quantidade, custo_medio FROM positions WHERE profile_id = ?", (d["profile_id"],))}
    resultado = []
    for o in orders:
        pos = positions.get(o["ticker"])
        px = db.scalar("SELECT close FROM prices_raw WHERE ticker = ? ORDER BY pregao DESC LIMIT 1", (o["ticker"],))
        resultado.append({"ticker": o["ticker"], "posicao_atual": pos["quantidade"] if pos else 0,
                          "pnl_aberto_pct": (px / pos["custo_medio"] - 1) * 100 if pos and px else None})
    return {
        "decisao": {**d, "registro": loads(d["registro_json"], {}), "parecer_regras": loads(d["parecer_regras_json"], None)},
        "dossie": ({**dossier, "conteudo": loads(dossier["conteudo_json"], {}), "refs": loads(dossier["refs_json"], [])} if dossier else None),
        "ordens": orders, "resultado": resultado,
    }


def mind(app: App, pid: str) -> dict[str, Any]:
    d = app.db.one("SELECT id FROM decisions WHERE profile_id = ? AND tipo != 'INICIAL' ORDER BY id DESC LIMIT 1", (pid,)) \
        or app.db.one("SELECT id FROM decisions WHERE profile_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    detail = decision_detail(app, d["id"]) if d else None
    return {"perfil": pid, "atual": detail, "agente": {"nome": app.agent.nome, "is_mock": app.agent.is_mock},
            "aviso": ("Parecer produzido pelo MockAgent (regras fixas). Quando o agente de IA estiver ativo, aqui aparece o raciocínio dele: hipóteses, "
                      "confiança, alternativas, riscos e o que mudaria a opinião." if app.agent.is_mock else None)}


def plans(app: App, pid: str) -> dict[str, Any]:
    db = app.db
    rows = db.query("SELECT t.*, p.descricao AS plano FROM triggers t LEFT JOIN plans p ON p.id = t.plan_id WHERE t.profile_id = ? ORDER BY t.id DESC LIMIT 300", (pid,))
    until = data_day(db)
    for t in rows:
        _loads_cols(t, "condicao_json", "invalida_json", "acao_json", "ultima_avaliacao_json")
        t["distancia_pct"] = (t.get("ultima_avaliacao") or {}).get("distancia_pct")
    timeline = db.query("SELECT id, ts, tipo, pregao, motivo, is_mock FROM event_log WHERE profile_id = ? AND tipo IN "
                        "('GATILHO_DISPARADO','GATILHO_INVALIDADO','DECISAO_REGISTRADA') ORDER BY id DESC LIMIT 100", (pid,))
    return {"perfil": pid, "dados_ate": until, "gatilhos": rows, "linha_do_tempo": timeline,
            "planos": db.query("SELECT * FROM plans WHERE profile_id = ? ORDER BY id DESC LIMIT 100", (pid,))}


def memory_view(app: App, pid: str) -> dict[str, Any]:
    db = app.db
    refs: dict[int, int] = {}
    for r in db.query("SELECT registro_json FROM decisions WHERE profile_id = ? ORDER BY id DESC LIMIT 300", (pid,)):
        for i in (loads(r["registro_json"], {}).get("influencia_do_passado") or {}).get("decisoes_ids", []):
            refs[i] = refs.get(i, 0) + 1
    top = sorted(refs.items(), key=lambda kv: -kv[1])[:10]
    infl = []
    for did, n in top:
        row = db.one("SELECT * FROM decisions WHERE id = ?", (did,))
        if row:
            infl.append({**_decision_summary(row), "citada_vezes": n})
    return {"perfil": pid, "licoes": db.query("SELECT * FROM lessons WHERE profile_id = ? ORDER BY id", (pid,)),
            "calibracao": memory.calibration(db, pid), "erros": memory.error_categories(db, pid), "decisoes_influentes": infl,
            "aviso": "Lições, calibração e categorias de erro desta tela são MOCK (encaixes). A reflexão real entra com o agente de IA."}


# ---------------------------------------------------------------------------------------------- risco, ordens, desempenho
def _pair_corr(db: Database, tickers: list[str], until: str, n: int = 60) -> dict[str, Any]:
    series_: dict[str, list[float]] = {}
    for t in tickers:
        bars = bars_adj(db, t, until, n + 1)
        series_[t] = pm.returns_from_levels([b["close"] for b in bars]) if len(bars) >= pm.MIN_OBS_RISK + 1 else []
    matrix = [[(pm.correlation(series_[a], series_[b]) if series_[a] and series_[b] and len(series_[a]) == len(series_[b]) else None)
               for b in tickers] for a in tickers]
    return {"tickers": tickers, "matriz": matrix, "janela_pregoes": n}


def risk(app: App, pid: str) -> dict[str, Any]:
    db, cfg = app.db, app.cfg
    prof = cfg.profiles[pid]
    lim = prof.limites
    until = data_day(db)
    state = load_state(db, pid)
    prices = last_prices(db, list(state.posicoes), until or "9999-12-31")
    equity = state.patrimonio(prices)
    exp = exposure(state, prices, cfg.universe) if state.posicoes and equity > 0 else {"ativo": {}, "setor": {}}
    h = db.one("SELECT drawdown FROM portfolio_history WHERE profile_id = ? ORDER BY pregao DESC LIMIT 1", (pid,))
    turnover = float(db.scalar("SELECT COALESCE(SUM(valor), 0) FROM fills WHERE profile_id = ? AND lado = 'COMPRA' AND pregao >= ? AND pregao <= ? "
                               "AND COALESCE(tipo, '') != 'CONSTRUCAO_INICIAL'", (pid, (until or "")[:8] + "01", until or ""), 0.0))
    st = db.one("SELECT somente_reduzir FROM profile_state WHERE profile_id = ?", (pid,)) or {}

    def item(nome: str, atual: float | None, limite: float, tipo: str, unidade: str = "%") -> dict[str, Any]:
        uso = (atual / limite * 100) if atual is not None and limite else None
        return {"nome": nome, "atual": atual, "limite": limite, "tipo": tipo, "unidade": unidade, "uso_pct": uso}

    limites = [
        item("Maior posição", max(exp["ativo"].values(), default=0.0) if equity > 0 else None, lim.max_por_posicao_pct, "max"),
        item("Maior setor", max(exp["setor"].values(), default=0.0) if equity > 0 else None, lim.max_por_setor_pct, "max"),
        item("Caixa", state.caixa / equity * 100 if equity > 0 else None, lim.caixa_minimo_pct, "min"),
        item("Número de posições", float(len(state.posicoes)), float(lim.posicoes_max), "max", "un"),
        item("Drawdown da carteira", -h["drawdown"] * 100 if h else None, lim.drawdown_max_carteira_pct, "max"),
        item("Giro mensal (compras)", turnover / equity * 100 if equity > 0 else None, lim.giro_max_mensal_pct, "max"),
    ]
    vetos = db.query("SELECT r.*, o.ticker, o.lado, o.origem FROM rule_events r LEFT JOIN orders o ON o.id = r.order_id "
                     "WHERE r.profile_id = ? ORDER BY r.id DESC LIMIT 200", (pid,))
    for v in vetos:
        _loads_cols(v, "detalhe_json")
    return {"perfil": pid, "dados_ate": until, "limites": limites, "minimo_posicoes": lim.posicoes_min, "somente_reduzir": bool(st.get("somente_reduzir")),
            "eventos_regras": vetos, "correlacao": _pair_corr(db, sorted(state.posicoes), until) if until and len(state.posicoes) > 1 else None,
            "stop_padrao_pct": lim.stop_padrao_pct}


def orders_view(app: App, pid: str) -> dict[str, Any]:
    db = app.db
    orders = db.query("SELECT * FROM orders WHERE profile_id = ? ORDER BY pregao_decisao DESC, id DESC LIMIT 300", (pid,))
    for o in orders:
        o["fills"] = db.query("SELECT * FROM fills WHERE order_id = ? ORDER BY id", (o["id"],))
    proventos = db.query("SELECT c.pregao, c.valor, c.ref_json, c.id FROM cash_ledger c WHERE c.profile_id = ? AND c.tipo = 'PROVENTO' ORDER BY c.pregao DESC LIMIT 200", (pid,))
    for p in proventos:
        _loads_cols(p, "ref_json")
    a_receber = db.query("SELECT ticker, quantidade, valor_total, pregao_credito FROM dividend_accruals WHERE profile_id = ? AND creditado = 0 ORDER BY pregao_credito", (pid,))
    tot = db.one("SELECT COALESCE(SUM(custos),0) custos, COALESCE(SUM(slippage_brl),0) slippage, COUNT(*) n FROM fills WHERE profile_id = ?", (pid,)) or {}
    return {"perfil": pid, "ordens": orders, "proventos": proventos, "proventos_a_receber": a_receber,
            "totais": {**tot, "proventos": sum(p["valor"] for p in proventos)}}


def performance(app: App, pid: str) -> dict[str, Any]:
    db = app.db
    monthly: dict[str, list[float]] = {}
    for r in db.query("SELECT pregao, patrimonio FROM portfolio_history WHERE profile_id = ? ORDER BY pregao", (pid,)):
        monthly.setdefault(r["pregao"][:7], []).append(r["patrimonio"])
    meses, prev = [], None
    for m, lv in sorted(monthly.items()):
        base = prev if prev is not None else lv[0]
        meses.append({"mes": m, "retorno_pct": (lv[-1] / base - 1) * 100 if base else None})
        prev = lv[-1]
    return {"perfil": pid, "metricas": pm.profile_metrics(db, pid), "atribuicao": pm.attribution(db, app.cfg, pid), "meses": meses,
            "trades": db.query("SELECT * FROM trades WHERE profile_id = ? ORDER BY saida DESC, id DESC LIMIT 200", (pid,))}


# ---------------------------------------------------------------------------------------------- sistema
def system(app: App, nivel: str | None = None, modulo: str | None = None, tipo: str | None = None, limit: int = 200) -> dict[str, Any]:
    db = app.db
    until = data_day(db)
    sources = db.query("SELECT * FROM data_sources ORDER BY name")
    logs_sql, p = "SELECT * FROM system_logs WHERE 1=1", []
    if nivel:
        logs_sql += " AND nivel = ?"
        p.append(nivel)
    if modulo:
        logs_sql += " AND modulo = ?"
        p.append(modulo)
    ev_sql, ep = "SELECT * FROM event_log WHERE 1=1", []
    if tipo:
        ev_sql += " AND tipo = ?"
        ep.append(tipo)
    logs = db.query(logs_sql + " ORDER BY id DESC LIMIT ?", (*p, limit))
    for lg in logs:
        _loads_cols(lg, "extra_json")
    events = db.query(ev_sql + " ORDER BY id DESC LIMIT ?", (*ep, limit))
    for e in events:
        _loads_cols(e, "ref_json", "payload_json")
    pend = db.query("SELECT profile_id, ticker, pregao, motivo FROM escalations WHERE status = 'ADIADO_ORCAMENTO'")
    avisos = []
    if pend:
        avisos.append(f"{len(pend)} escalonamento(s) adiado(s) por orçamento, aguardando o próximo ciclo.")
    for s in sources:
        if s["status"] in ("falhou", "atrasada"):
            avisos.append(f"Fonte {s['name']}: {s['status']} ({s['detalhe']})")
    failed = db.query("SELECT * FROM checkpoints WHERE status != 'ok' ORDER BY ts DESC LIMIT 20")
    tables = ["alerts", "escalations", "dossiers", "decisions", "orders", "fills", "trades", "rule_events", "triggers", "event_log", "prices_raw", "prices_adj"]
    return {
        "modo": app.mode, "dados_ate": until, "fontes": sources, "rodadas": db.query("SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT 20"),
        "orcamento": BudgetManager(db, app.cfg, today=date.fromisoformat(until) if until else None).snapshot(),
        "consumo_diario": db.query("SELECT * FROM budget_usage ORDER BY dia DESC, profile_id, recurso LIMIT 120"),
        "etapas_com_falha": failed, "logs": logs, "eventos": events, "avisos": avisos,
        "tipos_evento": [r["tipo"] for r in db.query("SELECT DISTINCT tipo FROM event_log ORDER BY tipo")],
        "contagens": {t: db.scalar(f"SELECT COUNT(*) FROM {t}", (), 0) for t in tables},
    }


TRACE_TABLES: dict[str, tuple[str, ...]] = {
    "alerts": ("id",), "decisions": ("id",), "dossiers": ("id",), "orders": ("id",), "fills": ("id",), "trades": ("id",), "triggers": ("id",),
    "rule_events": ("id",), "filings": ("id",), "corporate_events": ("id",), "event_log": ("id",), "escalations": ("id",), "plans": ("id",),
    "lessons": ("id",), "prices_raw": ("ticker", "pregao"), "prices_adj": ("ticker", "pregao"), "macro_series": ("codigo", "data"),
    "positions": ("profile_id", "ticker"), "portfolio_history": ("profile_id", "pregao"), "cash_ledger": ("id",),
}


def trace(app: App, tabela: str, keys: dict[str, str]) -> dict[str, Any] | None:
    """R8: devolve o registro de origem de um número. Só tabelas e colunas conhecidas (lista fechada)."""
    cols = TRACE_TABLES.get(tabela)
    if not cols or any(c not in keys for c in cols):
        return None
    row = app.db.one(f"SELECT * FROM {tabela} WHERE " + " AND ".join(f"{c} = ?" for c in cols), tuple(keys[c] for c in cols))
    if row is None:
        return None
    for k in list(row):
        if k.endswith("_json"):
            row[k[:-5]] = loads(row[k], None)
            del row[k]
    return {"tabela": tabela, "chave": {c: keys[c] for c in cols}, "registro": row}


def config_view(cfg: Config) -> dict[str, Any]:
    return {"perfis": {pid: p.model_dump() for pid, p in cfg.profiles.items()},
            "orcamentos": cfg.budgets.model_dump(), "aviso": "Somente leitura. Para mudar parâmetros, edite config/profiles.yaml."}
