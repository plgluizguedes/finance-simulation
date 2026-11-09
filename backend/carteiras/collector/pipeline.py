"""Coleta diária em modo real. Cada fonte falha sozinha: uma fonte fora do ar degrada com aviso e não derruba a rodada.

Pedido da B3 não é automatizado (COTAHIST é importação manual, R7). A coleta automática de preços é por ativo, via brapi,
dentro da cota. Nada aqui foi validado contra respostas reais da brapi (ver docs/DATA_SOURCES.md).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..adjust.adjust import rebuild_all
from ..config import Config
from ..db import Database
from ..escalation.budget import BudgetManager
from ..eventlog import log_event, log_system
from .base import BudgetExceeded, NotAvailable, SourceUnavailable
from .bcb import BcbClient, collect_macro
from .brapi import BrapiProvider
from .cotahist import import_inbox
from .cvm import collect_ipe
from .store import set_source_status, upsert_events, upsert_prices

MACRO_CODES = [12, 432, 433, 1]
EVENTS_EVERY_DAYS = 5          # proventos custam 1 requisição por ativo; pede-se a cada N dias, não todo dia


def tickers_to_collect(db: Database, cfg: Config) -> list[str]:
    """Carteira e watchlist de todos os perfis (a carteira primeiro, para o orçamento curto atender o essencial)."""
    held = [r["ticker"] for r in db.query("SELECT DISTINCT ticker FROM positions WHERE quantidade > 0 ORDER BY ticker")]
    rest = sorted({t for p in cfg.profiles.values() for t in p.universo.tickers} - set(held))
    return held + rest


def collect_brapi(db: Database, cfg: Config, provider: BrapiProvider, today: date) -> dict[str, Any]:
    index_symbol, index_code = cfg.universe.indice.simbolo_brapi, cfg.universe.indice.codigo
    ok = fail = 0
    stopped: str | None = None
    last_day: str | None = None
    do_events = (db.kv_get("brapi_events_last") or "") <= (today - timedelta(days=EVENTS_EVERY_DAYS)).isoformat()
    for t in [index_symbol, *tickers_to_collect(db, cfg)]:
        try:
            bars = provider.ticker_quote(t)
            if t == index_symbol:
                bars = [replace(b, ticker=index_code) for b in bars]
            upsert_prices(db, bars)
            if bars:
                last_day = max([last_day or "", *(b.pregao for b in bars)])
            if do_events and t != index_symbol:
                start = (today - timedelta(days=45)).isoformat()
                upsert_events(db, provider.corporate_events(t, start, (today + timedelta(days=30)).isoformat()))
            ok += 1
        except BudgetExceeded as exc:
            stopped = str(exc)
            break
        except (NotAvailable, SourceUnavailable) as exc:
            fail += 1
            log_system(db, "WARNING", "brapi", f"{t}: {exc}")
    if do_events and not stopped:
        db.kv_set("brapi_events_last", today.isoformat())
    detalhe = f"{ok} ativo(s) ok, {fail} falha(s)" + (f"; parou por orçamento: {stopped}" if stopped else "")
    status = "atrasada" if stopped else ("falhou" if fail and not ok else ("atrasada" if fail else "ok"))
    set_source_status(db, "brapi", status, detalhe, last_day)
    return {"ok": ok, "falhas": fail, "parou_por_orcamento": stopped, "dados_ate": last_day}


def collect_real(
    db: Database, cfg: Config, budget: BudgetManager, *, inbox: Path, provider: BrapiProvider | None = None,
    bcb: BcbClient | None = None, http: Any = None, today: date | None = None, with_cvm: bool = True,
) -> dict[str, Any]:
    """Uma coleta: COTAHIST da caixa de entrada, brapi por ativo, macro do BCB, IPE da CVM, e reconstrução das séries ajustadas."""
    today = today or date.today()
    out: dict[str, Any] = {}
    universe_tickers = set(cfg.universe.all_tickers())
    out["cotahist"] = import_inbox(db, inbox, universe_tickers, index_codes={cfg.universe.indice.codigo})
    provider = provider or BrapiProvider(on_request=budget.spend_brapi)
    out["brapi"] = collect_brapi(db, cfg, provider, today)
    out["bcb"] = collect_macro(db, bcb or BcbClient(), MACRO_CODES)
    if with_cvm and (db.kv_get("cvm_last") or "") < today.isoformat():
        out["cvm"] = collect_ipe(db, cfg.universe, today.year, http)
        db.kv_set("cvm_last", today.isoformat())
    out["ajuste"] = rebuild_all(db, sorted(universe_tickers | {cfg.universe.indice.codigo}))
    log_event(db, origem="sistema", tipo="COLETA_CONCLUIDA", motivo="coleta diária", payload={k: v for k, v in out.items() if k != "cvm"})
    return out
