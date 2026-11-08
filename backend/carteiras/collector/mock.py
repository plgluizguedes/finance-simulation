"""Gerador de dados SINTÉTICOS (MOCK). Tudo que sai daqui tem is_mock = 1 e source = 'mock'.

Determinístico (semente fixa). Os preços não são cotações reais e não representam nenhum ativo de verdade.
Gera um mercado, um índice, séries macro, proventos, um desdobramento, um salto sem evento e alguns documentos,
para que o pipeline completo (radar, escalonamento, simulação, dashboard) tenha o que processar.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date
from typing import Any

from ..calendar_b3 import Calendar
from ..config import Config
from ..db import Database
from .base import CorpEvent, Filing, MacroPoint, PriceBar
from .store import set_source_status, upsert_events, upsert_filings, upsert_macro, upsert_prices

SOURCE = "mock"
INDEX_CODE = "IBOV"

# Choques roteirizados, em pregões contados a partir do fim (-1 = último pregão).
MARKET_SHOCKS = {-55: -0.025, -54: -0.034, -53: -0.018, -52: 0.015}
TICKER_SHOCKS: dict[str, dict[int, dict[str, float]]] = {
    "PETR4": {-40: {"ret": -0.095, "vol": 4.0}},
    "VALE3": {-30: {"ret": -0.028, "gap": -0.04, "vol": 2.5}},
    "TIMS3": {-12: {"ret": math.log(0.5), "vol": 3.0}},  # salto SEM evento registrado, para testar o radar
}
SPLITS = {"RADL3": (-20, 2.0)}  # desdobramento 1:2 registrado


def _h(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _round_bar(o: float, h: float, lo: float, c: float) -> tuple[float, float, float, float]:
    o, h, lo, c = (round(x, 2) for x in (o, h, lo, c))
    return o, max(h, o, c), min(lo, o, c), c


def _session_list(cal: Calendar, end: date, n: int) -> list[date]:
    days = [end]
    while len(days) < n:
        days.append(cal.prev_day(days[-1]))
    return list(reversed(days))


def generate(db: Database, cfg: Config, cal: Calendar, *, end: date, sessions: int = 300, seed: int = 20261108) -> dict[str, Any]:
    days = _session_list(cal, end, sessions)
    n = len(days)
    iso = [d.isoformat() for d in days]

    # Mercado
    rng_m = random.Random(f"{seed}:mercado")
    market = [rng_m.gauss(0.0003, 0.0095) for _ in range(n)]
    for off, ret in MARKET_SHOCKS.items():
        market[n + off] = ret

    bars: list[PriceBar] = []
    events: list[CorpEvent] = []
    filings: list[Filing] = []

    # Índice sintético
    level = 130000.0
    prev = level
    for i in range(n):
        c = prev * math.exp(market[i])
        o = prev * math.exp(market[i] * 0.2)
        hi = max(o, c) * 1.003
        lo = min(o, c) * 0.997
        oo, hh, ll, cc = _round_bar(o, hi, lo, c)
        bars.append(PriceBar(INDEX_CODE, iso[i], oo, hh, ll, cc, 0.0, 0.0, 0, SOURCE))
        prev = c

    for ticker in cfg.universe.all_tickers():
        h = _h(ticker)
        rng = random.Random(f"{seed}:{ticker}")
        beta = 0.7 + (h % 60) / 100.0
        idio = 0.011 + (h % 12) / 1000.0
        base_vol = (40 + (h % 360)) * 1_000_000.0
        price = 10.0 + (h % 80) + (h % 100) / 100.0
        shocks = {n + off: s for off, s in TICKER_SHOCKS.get(ticker, {}).items()}

        # Eventos: dividendos em ciclos de 60 pregões; desdobramento roteirizado.
        ex_idx: dict[int, tuple[str, float, float]] = {}  # idx -> (tipo, fator de preço, y ou fator)
        off = -(5 + h % 55)
        while n + off >= 2:
            y = 0.008 + (h % 7) / 1000.0
            ex_idx[n + off] = ("JCP" if h % 3 == 0 else "DIVIDENDO", 1.0 - y, y)
            off -= 60
        if ticker in SPLITS:
            soff, fator = SPLITS[ticker]
            ex_idx[n + soff] = ("DESDOBRAMENTO", 1.0 / fator, fator)

        # Fator acumulado F_t (produto dos fatores de eventos com ex > t): bruto = ajustado / F_t
        F = [1.0] * n
        acc = 1.0
        for t in range(n - 1, -1, -1):
            F[t] = acc
            if t in ex_idx:
                acc *= ex_idx[t][1]

        prev_close = price
        raw: list[tuple[float, float, float, float, float]] = []
        for i in range(n):
            shock = shocks.get(i, {})
            gap = rng.gauss(0, idio * 0.3) + shock.get("gap", 0.0)
            r = shock["ret"] if "ret" in shock else beta * market[i] + rng.gauss(0, idio)
            o = prev_close * math.exp(gap)
            c = prev_close * math.exp(r)
            hi = max(o, c) * (1 + abs(rng.gauss(0, idio * 0.5)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, idio * 0.5)))
            vol = base_vol * math.exp(rng.gauss(0, 0.3)) * (1 + 2 * min(abs(r) / 0.012, 3)) * shock.get("vol", 1.0)
            raw.append((o / F[i], hi / F[i], lo / F[i], c / F[i], vol))
            prev_close = c

        for i, (o, hi, lo, c, vol) in enumerate(raw):
            oo, hh, ll, cc = _round_bar(o, hi, lo, c)
            bars.append(PriceBar(ticker, iso[i], oo, hh, ll, cc, round(vol, 2), round(vol / cc), 200 + h % 300, SOURCE))

        for i, (tipo, f_price, extra) in ex_idx.items():
            pay = cal.add_sessions(days[i], 8).isoformat()
            if tipo == "DESDOBRAMENTO":
                events.append(CorpEvent(ticker, tipo, iso[i], SOURCE, data_com=iso[i - 1], fator=extra))
            else:
                p_prev = round(raw[i - 1][3], 2)
                events.append(CorpEvent(ticker, tipo, iso[i], SOURCE, data_com=iso[i - 1], data_pagamento=pay,
                                        valor=round(extra * p_prev, 2)))

        # Documentos MOCK nos dias de choque
        for idx in shocks:
            filings.append(Filing(
                protocolo=f"MOCK-{ticker}-{iso[idx]}", cnpj="00.000.000/0001-00", nome_emissor=cfg.universe.ativos[ticker].nome,
                categoria="Fato Relevante", assunto="Documento SINTÉTICO (MOCK) associado a um choque de preço",
                data_referencia=iso[idx], data_entrega=iso[idx], link="", ticker=ticker,
            ))

    # Macro: CDI (0,050788% ao dia, valor real observado em 08/11/2026), Selic meta, IPCA e dólar, tudo MOCK
    macro: dict[int, list[MacroPoint]] = {12: [], 432: [], 433: [], 1: []}
    fx = 5.20
    rng_fx = random.Random(f"{seed}:fx")
    for i in range(n):
        macro[12].append(MacroPoint(12, iso[i], 0.050788))
        macro[432].append(MacroPoint(432, iso[i], 13.75))
        fx *= math.exp(rng_fx.gauss(0, 0.005))
        macro[1].append(MacroPoint(1, iso[i], round(fx, 4)))
        if i == 0 or iso[i][:7] != iso[i - 1][:7]:
            macro[433].append(MacroPoint(433, iso[i], round(0.30 + (i % 7) * 0.03, 2)))

    written, _ = upsert_prices(db, bars, is_mock=True)
    upsert_events(db, events, is_mock=True)
    names = {12: "CDI diário", 432: "Selic meta", 433: "IPCA", 1: "Dólar comercial (venda)"}
    for codigo, pts in macro.items():
        upsert_macro(db, pts, nome=names[codigo], source=SOURCE, is_mock=True)
    upsert_filings(db, filings, source=SOURCE, is_mock=True)
    for ticker, a in cfg.universe.ativos.items():
        db.insert("assets", {"ticker": ticker, "nome": a.nome, "emissor": a.nome, "setor": a.setor,
                             "tipo": "acao", "lote": 100, "ativo": 1, "is_mock": 1}, or_replace=True)
    db.insert("assets", {"ticker": INDEX_CODE, "nome": cfg.universe.indice.nome, "emissor": None, "setor": None,
                         "tipo": "indice", "lote": 1, "ativo": 1, "is_mock": 1}, or_replace=True)
    for src in ("cotahist", "brapi", "bcb_sgs", "cvm_ipe"):
        set_source_status(db, src, "mock", "dados sintéticos (MOCK); a fonte real não foi consultada", iso[-1], is_mock=True)
    return {"pregoes": n, "primeiro": iso[0], "ultimo": iso[-1], "barras": written, "eventos": len(events), "documentos": len(filings)}


class MockProvider:
    """Provedor que lê do banco os dados sintéticos já gerados."""

    name = "mock"

    def __init__(self, db: Database):
        self.db = db

    def bulk_prices(self, pregao: str) -> list[PriceBar]:
        rows = self.db.query("SELECT * FROM prices_raw WHERE pregao = ?", (pregao,))
        return [PriceBar(r["ticker"], r["pregao"], r["open"], r["high"], r["low"], r["close"], r["volume_fin"],
                         r["quantidade"], r["negocios"], r["source"]) for r in rows]

    def ticker_quote(self, ticker: str) -> list[PriceBar]:
        rows = self.db.query("SELECT * FROM prices_raw WHERE ticker = ? ORDER BY pregao DESC LIMIT 5", (ticker,))
        return [PriceBar(r["ticker"], r["pregao"], r["open"], r["high"], r["low"], r["close"], r["volume_fin"],
                         r["quantidade"], r["negocios"], r["source"]) for r in reversed(rows)]

    def corporate_events(self, ticker: str, inicio: str, fim: str) -> list[CorpEvent]:
        rows = self.db.query(
            "SELECT * FROM corporate_events WHERE ticker = ? AND data_ex BETWEEN ? AND ?", (ticker, inicio, fim)
        )
        return [CorpEvent(r["ticker"], r["tipo"], r["data_ex"], r["source"], r["data_com"], r["data_pagamento"],
                          r["valor"], r["fator"]) for r in rows]

    def macro_series(self, codigo: int, inicio: str | None, fim: str | None) -> list[MacroPoint]:
        rows = self.db.query("SELECT * FROM macro_series WHERE codigo = ? ORDER BY data", (codigo,))
        return [MacroPoint(r["codigo"], r["data"], r["valor"]) for r in rows
                if (not inicio or r["data"] >= inicio) and (not fim or r["data"] <= fim)]

    def filings(self, emissor: str, inicio: str, fim: str) -> list[Filing]:
        return []


def seed_window(end: date, cal: Calendar, sessions: int = 70) -> date:
    """Pregão em que as carteiras MOCK começam (cerca de `sessions` pregões antes do fim)."""
    return cal.add_sessions(end, -sessions)


__all__ = ["MockProvider", "generate", "seed_window"]
