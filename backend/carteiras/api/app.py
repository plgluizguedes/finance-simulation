"""Aplicação Starlette: leitura (/api/*), controles (/api/control/*), eventos em tempo real (SSE) e o dashboard estático.

Segurança: escuta só em 127.0.0.1 por padrão; TrustedHost recusa Host estranho (rebinding); controles que mudam estado
exigem o cabeçalho X-Carteiras: 1 (um formulário de outro site não consegue enviá-lo).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import settings
from ..bootstrap import App
from ..collector import mock as mock_collector
from ..db import dumps, now_iso
from ..eventlog import log_event
from ..simulation.broker import OrderRequest
from . import queries as q

CONTROL_HEADER = "x-carteiras"
EXPORTS: dict[str, tuple[str, str]] = {
    "ordens": ("orders", "profile_id = ?"), "execucoes": ("fills", "profile_id = ?"), "operacoes": ("trades", "profile_id = ?"),
    "decisoes": ("decisions", "profile_id = ?"), "alertas": ("alerts", "profile_id = ?"), "historico": ("portfolio_history", "profile_id = ?"),
    "posicoes": ("positions", "profile_id = ?"), "eventos": ("event_log", "(profile_id = ? OR profile_id IS NULL)"),
}
PROFILE_TABLES = ["positions", "orders", "fills", "cash_ledger", "portfolio_history", "position_snapshots", "trades", "dividend_accruals",
                  "event_applications", "triggers", "plans", "rule_events", "escalations", "dossiers", "decisions", "alerts", "lessons",
                  "calibration", "error_categories", "budget_usage", "checkpoints", "profile_state"]


class ManualOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    perfil: str
    ticker: str
    lado: str = Field(pattern="^(COMPRA|VENDA)$")
    dimensionamento: str = Field(pattern="^(quantidade|peso_alvo_pct|fracao_posicao)$")
    valor: float = Field(gt=0)
    stop_pct: float | None = Field(default=None, gt=0, lt=100)
    alvo_pct: float | None = Field(default=None, gt=0)
    motivo: str = Field(default="ordem manual de teste do motor", max_length=300)


class Jobs:
    """Uma tarefa longa por vez (coleta ou rodada), em segundo plano, com estado consultável."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"rodando": None, "ultimo": None}

    def start(self, name: str, fn: Callable[[], Any]) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.state["rodando"] = {"nome": name, "desde": now_iso()}

        def work() -> None:
            try:
                result: Any = fn()
                self.state["ultimo"] = {"nome": name, "ok": True, "fim": now_iso(), "resultado": result}
            except Exception as exc:                          # fronteira da tarefa: nunca derruba o servidor
                self.state["ultimo"] = {"nome": name, "ok": False, "fim": now_iso(), "erro": f"{type(exc).__name__}: {exc}"}
            finally:
                self.state["rodando"] = None
                self._lock.release()

        threading.Thread(target=work, name=f"job-{name}", daemon=True).start()
        return True


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(data, default=str, allow_nan=False, ensure_ascii=False)) if data is not None else None, status_code=status)


def _safe_cell(v: Any) -> Any:
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def create_app(app: App, static_dir: Path | None = None) -> Starlette:
    jobs = Jobs()
    pids = set(app.cfg.profile_ids())

    def profile(request: Request) -> str:
        pid = request.path_params["pid"]
        if pid not in pids:
            raise KeyError(pid)
        return pid

    def reads(fn: Callable[..., Any], *, with_pid: bool = True) -> Callable[[Request], Any]:
        def handler(request: Request) -> Response:
            try:
                args = [profile(request)] if with_pid else []
                return _json(fn(app, *args, **{k: v for k, v in request.query_params.items() if v != ""}))
            except KeyError:
                return _json({"erro": "perfil desconhecido"}, 404)
            except TypeError:
                return _json({"erro": "parâmetro de consulta inválido"}, 400)
        return handler

    def need_control(request: Request) -> Response | None:
        if request.headers.get(CONTROL_HEADER) != "1":
            return _json({"erro": "controle recusado: falta o cabeçalho X-Carteiras: 1"}, 403)
        return None

    # ------------------------------------------------------------------ leituras
    def r_meta(request: Request) -> Response:
        return _json({**q.meta(app), "tarefa": jobs.state})

    def r_decision(request: Request) -> Response:
        try:
            out = q.decision_detail(app, int(request.path_params["did"]))
        except ValueError:
            return _json({"erro": "id inválido"}, 400)
        return _json(out) if out else _json({"erro": "decisão não encontrada"}, 404)

    def r_market(request: Request) -> Response:
        t = request.path_params["ticker"].upper()
        if t not in set(app.cfg.universe.all_tickers()) | {app.cfg.universe.indice.codigo}:
            return _json({"erro": "ativo fora do universo"}, 404)
        pid = request.query_params.get("perfil")
        return _json(q.market(app, t, pid if pid in pids else None))

    def r_trace(request: Request) -> Response:
        keys = {k: v for k, v in request.query_params.items()}
        out = q.trace(app, request.path_params["tabela"], keys)
        return _json(out) if out else _json({"erro": "registro não encontrado ou tabela não rastreável"}, 404)

    def r_decisions(request: Request) -> Response:
        pid = profile(request)
        p = request.query_params
        return _json(q.decisions(app, pid, p.get("tipo"), p.get("ticker"), p.get("origem")))

    def r_config(request: Request) -> Response:
        return _json(q.config_view(app.cfg))

    def r_job(request: Request) -> Response:
        return _json(jobs.state)

    # ------------------------------------------------------------------ controles
    async def c_pause(request: Request) -> Response:
        if (bad := need_control(request)):
            return bad
        pid, pause = profile(request), request.url.path.endswith("/pause/" + request.path_params["pid"])
        started = app.db.scalar("SELECT iniciado_em FROM profile_state WHERE profile_id = ?", (pid,))
        if not started:
            return _json({"erro": "a carteira ainda não foi iniciada"}, 409)
        app.db.execute("UPDATE profile_state SET pausado = ?, atualizado_em = ? WHERE profile_id = ?", (1 if pause else 0, now_iso(), pid))
        log_event(app.db, origem="manual", tipo="CARTEIRA_PAUSADA" if pause else "CARTEIRA_RETOMADA", profile_id=pid,
                  motivo="ação no painel de controles", is_mock=app.is_mock)
        return _json({"perfil": pid, "pausada": pause})

    async def c_collect(request: Request) -> Response:
        if (bad := need_control(request)):
            return bad
        return _json({"iniciada": jobs.start("coleta", app.collect)}, 202)

    async def c_run(request: Request) -> Response:
        if (bad := need_control(request)):
            return bad

        def go() -> Any:
            res = app.runner().run_round(start_at=default_start())
            return {"status": res.status, "processados": len(res.processados), "erros": res.erros, "pulados": res.pulados}
        return _json({"iniciada": jobs.start("rodada", go)}, 202)

    def default_start() -> str | None:
        ate = q.data_day(app.db)
        if not ate:
            return None
        return mock_collector.seed_window(date.fromisoformat(ate), app.cal, 70).isoformat() if app.is_mock else None

    async def c_manual_order(request: Request) -> Response:
        if (bad := need_control(request)):
            return bad
        try:
            body = ManualOrder.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return _json({"erro": f"ordem inválida: {str(exc)[:200]}"}, 400)
        if body.perfil not in pids:
            return _json({"erro": "perfil desconhecido"}, 404)
        if body.ticker not in app.cfg.profiles[body.perfil].universo.tickers:
            return _json({"erro": "ativo fora do universo do perfil"}, 400)
        last = app.db.scalar("SELECT ultimo_pregao FROM profile_state WHERE profile_id = ?", (body.perfil,))
        if not last:
            return _json({"erro": "a carteira ainda não processou nenhum pregão"}, 409)
        dim, val = body.dimensionamento, body.valor
        req = OrderRequest(
            profile_id=body.perfil, ticker=body.ticker, side=body.lado, dimensionamento=dim, pregao_decisao=last, origem="manual",
            motivo=body.motivo, quantidade=int(val) if dim == "quantidade" else None, peso_alvo_pct=val if dim == "peso_alvo_pct" else None,
            fracao_posicao=val if dim == "fracao_posicao" else None, stop_pct=body.stop_pct, alvo_pct=body.alvo_pct,
            idem_key=f"manual:{now_iso()}:{body.ticker}:{body.lado}")
        order_id = await run_in_threadpool(lambda: app.runner().broker.schedule(req))
        order = app.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
        regras = app.db.query("SELECT regra, resultado, motivo FROM rule_events WHERE order_id = ?", (order_id,))
        return _json({"ordem": order, "regras": regras, "nota": "A ordem passou pelas regras do perfil e será executada no próximo pregão processado."}, 201)

    async def c_reset(request: Request) -> Response:
        if (bad := need_control(request)):
            return bad
        pid = profile(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body.get("confirmar") is not True or body.get("frase") != f"RESETAR {pid}":
            return _json({"erro": f"confirmação dupla exigida: confirmar=true e frase exatamente 'RESETAR {pid}'"}, 400)
        if jobs.state["rodando"]:
            return _json({"erro": "há uma tarefa em andamento; aguarde"}, 409)
        backup = await run_in_threadpool(lambda: _backup(app))
        with app.db.transaction():
            for t in PROFILE_TABLES:
                app.db.execute(f"DELETE FROM {t} WHERE profile_id = ?", (pid,))
            log_event(app.db, origem="manual", tipo="CARTEIRA_RESETADA", profile_id=pid, motivo=f"reset com backup em {backup.name}", is_mock=app.is_mock)
        return _json({"perfil": pid, "backup": backup.name, "nota": "Carteira apagada. Rode uma nova rodada para recriá-la com o capital inicial do perfil."})

    def c_export(request: Request) -> Response:
        kind, pid = request.path_params["kind"], profile(request)
        if kind not in EXPORTS:
            return _json({"erro": f"exportação desconhecida: {sorted(EXPORTS)}"}, 404)
        table, where = EXPORTS[kind]
        rows = app.db.query(f"SELECT * FROM {table} WHERE {where} ORDER BY 1", (pid,))
        fmt = request.query_params.get("formato", "csv")
        name = f"{kind}_{pid}_{datetime.now():%Y%m%d_%H%M}"
        if fmt == "json":
            return Response(json.dumps(rows, ensure_ascii=False, default=str, indent=1), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow({k: _safe_cell(v) for k, v in r.items()})
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

    # ------------------------------------------------------------------ SSE
    async def events(request: Request) -> EventSourceResponse:
        async def stream() -> Any:
            last = await run_in_threadpool(lambda: int(app.db.scalar("SELECT COALESCE(MAX(id), 0) FROM event_log", (), 0)))
            while True:
                if await request.is_disconnected():
                    break
                rows = await run_in_threadpool(lambda: app.db.query(
                    "SELECT id, ts, tipo, profile_id, pregao, motivo, origem, is_mock FROM event_log WHERE id > ? ORDER BY id LIMIT 50", (last,)))
                for r in rows:
                    last = r["id"]
                    yield {"event": "evento", "id": str(r["id"]), "data": dumps(r)}
                if not rows:
                    yield {"event": "ping", "data": dumps({"ts": now_iso(), "tarefa": jobs.state})}
                await asyncio.sleep(2.0)
        return EventSourceResponse(stream())

    def index(request: Request) -> Response:
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Carteiras B3</title><body style='font-family:system-ui;max-width:40rem;margin:3rem auto'>"
                            "<h1>Carteiras B3</h1><p>A API está no ar, mas o dashboard ainda não foi compilado.</p>"
                            "<p>Rode <code>cd dashboard &amp;&amp; npm install &amp;&amp; npm run build</code> e recarregue.</p>"
                            "<p>API: <a href='/api/meta'>/api/meta</a></p>")

    routes: list[Any] = [
        Route("/api/meta", r_meta), Route("/api/overview", reads(q.overview, with_pid=False)),
        Route("/api/compare", reads(q.compare, with_pid=False)), Route("/api/config", r_config), Route("/api/job", r_job),
        Route("/api/portfolio/{pid}", reads(q.portfolio)), Route("/api/radar/{pid}", reads(q.radar)),
        Route("/api/market/{ticker}", r_market), Route("/api/mind/{pid}", reads(q.mind)), Route("/api/plans/{pid}", reads(q.plans)),
        Route("/api/decisions/{pid}", r_decisions), Route("/api/decision/{did}", r_decision),
        Route("/api/memory/{pid}", reads(q.memory_view)), Route("/api/risk/{pid}", reads(q.risk)), Route("/api/orders/{pid}", reads(q.orders_view)),
        Route("/api/performance/{pid}", reads(q.performance)), Route("/api/system", reads(q.system, with_pid=False)),
        Route("/api/trace/{tabela}", r_trace), Route("/api/events", events),
        Route("/api/control/pause/{pid}", c_pause, methods=["POST"]), Route("/api/control/resume/{pid}", c_pause, methods=["POST"]),
        Route("/api/control/collect", c_collect, methods=["POST"]), Route("/api/control/run", c_run, methods=["POST"]),
        Route("/api/control/manual-order", c_manual_order, methods=["POST"]), Route("/api/control/reset/{pid}", c_reset, methods=["POST"]),
        Route("/api/control/export/{kind}/{pid}", c_export),
    ]
    static = static_dir or Path(__file__).resolve().parents[3] / "dashboard" / "dist"
    routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="dashboard") if static.is_dir() else Route("/", index))
    hosts = ["127.0.0.1", "localhost", "testserver"] + [h for h in os.environ.get("CARTEIRAS_ALLOWED_HOSTS", "").split(",") if h]
    return Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=hosts)])


def _backup(app: App) -> Path:
    folder = settings.data_dir() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{app.mode}-{datetime.now():%Y%m%d-%H%M%S}.db"
    out = sqlite3.connect(dest)
    try:
        app.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        src = sqlite3.connect(app.db.path)
        try:
            src.backup(out)
        finally:
            src.close()
    finally:
        out.close()
    return dest
