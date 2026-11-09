"""Linha de comando: `python -m carteiras <comando>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from . import settings
from .bootstrap import App, build_app


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(app: App, _: argparse.Namespace) -> int:
    print(f"Banco pronto em {settings.db_path(app.mode)} (modo {app.mode}).")
    return 0


def cmd_seed_mock(app: App, args: argparse.Namespace) -> int:
    res = app.seed_mock(end=date.fromisoformat(args.ate) if args.ate else None, window=args.janela)
    _print({"status": res.status, "pregoes_processados": len(res.processados), "erros": res.erros})
    return 0 if res.status == "OK" else 1


def cmd_run(app: App, args: argparse.Namespace) -> int:
    res = app.runner().run_round(until=args.ate, start_at=args.inicio)
    _print({"status": res.status, "processados": res.processados, "erros": res.erros, "pulados": res.pulados,
            "sem_dado": res.pendentes_sem_dado})
    return 0 if res.status in ("OK", "SEM_DADOS") else 1


def cmd_collect(app: App, _: argparse.Namespace) -> int:
    _print(app.collect())
    return 0


def cmd_import(app: App, _: argparse.Namespace) -> int:
    from .collector.cotahist import import_inbox
    _print(import_inbox(app.db, app.inbox(), set(app.cfg.universe.all_tickers()), index_codes={app.cfg.universe.indice.codigo}))
    from .adjust.adjust import rebuild_all
    rebuild_all(app.db, app.cfg.universe.all_tickers() + [app.cfg.universe.indice.codigo])
    return 0


def cmd_status(app: App, _: argparse.Namespace) -> int:
    from .portfolio.metrics import profile_metrics
    _print({
        "modo": app.mode, "banco": str(settings.db_path(app.mode)),
        "perfis": {pid: {"ultimo_pregao": (app.db.one("SELECT ultimo_pregao FROM profile_state WHERE profile_id = ?", (pid,)) or {}).get("ultimo_pregao"),
                         "retorno_total": profile_metrics(app.db, pid)["carteira"]["retorno_total"]} for pid in app.cfg.profile_ids()},
        "orcamento": app.budget().snapshot(),
    })
    return 0


def cmd_serve(app: App, args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    uvicorn.run(create_app(app), host=args.host, port=args.porta, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carteiras", description="Simulador de carteiras virtuais da B3 (sem ordens reais).")
    parser.add_argument("--modo", choices=settings.MODES, help="sobrepõe CARTEIRAS_MODE (padrão: mock)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="cria o banco do modo atual").set_defaults(fn=cmd_init)
    p = sub.add_parser("seed-mock", help="gera dados MOCK e roda o pipeline sobre eles (só em modo mock)")
    p.add_argument("--ate", help="último pregão dos dados MOCK (AAAA-MM-DD); padrão: último pregão antes de hoje")
    p.add_argument("--janela", type=int, default=70, help="quantos pregões simular (padrão 70)")
    p.set_defaults(fn=cmd_seed_mock)
    p = sub.add_parser("run", help="processa os pregões pendentes (em modo real, coleta antes)")
    p.add_argument("--ate", help="processar até este pregão (AAAA-MM-DD)")
    p.add_argument("--inicio", help="pregão de início de uma carteira nova (AAAA-MM-DD)")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("collect", help="só coleta (modo real)").set_defaults(fn=cmd_collect)
    sub.add_parser("import-inbox", help="importa COTAHIST de data/inbox").set_defaults(fn=cmd_import)
    sub.add_parser("status", help="resumo das carteiras e orçamentos").set_defaults(fn=cmd_status)
    p = sub.add_parser("serve", help="sobe a API e o dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)
    args = parser.parse_args(argv)
    app = build_app(args.modo)
    return int(args.fn(app, args))


if __name__ == "__main__":
    sys.exit(main())
