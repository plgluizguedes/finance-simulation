"""Banco Central, API SGS (dados abertos, sem chave).

Resposta real verificada em 08/11/2026 para a série 12: [{"data":"29/10/2026","valor":"0.050788"}, ...]
(data em dd/mm/aaaa; valor como texto com ponto decimal). Janelas de série diária ficam em até 5 anos por consulta.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..db import Database
from .base import MacroPoint, SourceUnavailable
from .http import HttpClient
from .store import set_source_status, upsert_macro

BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"

# Metadados mantidos localmente: o SGS não oferece endpoint JSON de metadata. Códigos NÃO conferidos um a um,
# exceto o 12, que respondeu de verdade em 08/11/2026.
SERIES_META: dict[int, dict[str, str]] = {
    12: {"nome": "CDI diário", "unidade": "% ao dia", "periodicidade": "diária"},
    11: {"nome": "Selic diária", "unidade": "% ao dia", "periodicidade": "diária"},
    432: {"nome": "Selic meta", "unidade": "% ao ano", "periodicidade": "por reunião"},
    433: {"nome": "IPCA", "unidade": "% ao mês", "periodicidade": "mensal"},
    1: {"nome": "Dólar comercial (venda)", "unidade": "R$", "periodicidade": "diária"},
}


def parse_points(codigo: int, payload: Any) -> list[MacroPoint]:
    if not isinstance(payload, list):
        raise SourceUnavailable("SGS devolveu um formato inesperado")
    points: list[MacroPoint] = []
    for item in payload:
        try:
            iso = datetime.strptime(str(item["data"]), "%d/%m/%Y").strftime("%Y-%m-%d")
            points.append(MacroPoint(codigo=codigo, data=iso, valor=float(str(item["valor"]).replace(",", "."))))
        except (KeyError, ValueError, TypeError):
            continue
    return points


class BcbClient:
    name = "bcb_sgs"

    def __init__(self, http: HttpClient | Any | None = None):
        self.http = http or HttpClient(min_interval=1.0)

    def fetch(self, codigo: int, inicio: date | None = None, fim: date | None = None, ultimos: int | None = None) -> list[MacroPoint]:
        url = BASE.format(codigo=codigo)
        if ultimos:
            url += f"/ultimos/{int(ultimos)}"
            params: dict[str, str] = {"formato": "json"}
        else:
            params = {"formato": "json"}
            if inicio:
                params["dataInicial"] = inicio.strftime("%d/%m/%Y")
            if fim:
                params["dataFinal"] = fim.strftime("%d/%m/%Y")
        return parse_points(codigo, self.http.get_json(url, params=params))

    def macro_series(self, codigo: int, inicio: str | None, fim: str | None) -> list[MacroPoint]:
        start = date.fromisoformat(inicio) if inicio else None
        end = date.fromisoformat(fim) if fim else None
        if start is not None and (end or date.today()) - start > timedelta(days=5 * 365):
            raise ValueError("pedido maior que 5 anos; divida em janelas menores")
        return self.fetch(codigo, start, end)


def collect_macro(db: Database, client: BcbClient, codigos: list[int], ultimos: int = 30) -> dict[int, int]:
    """Atualiza as séries macro. Falha de uma série não derruba as outras; o status da fonte registra o resultado."""
    result: dict[int, int] = {}
    errors: list[str] = []
    last: str | None = None
    for codigo in codigos:
        meta = SERIES_META.get(codigo, {"nome": f"SGS {codigo}"})
        try:
            points = client.fetch(codigo, ultimos=ultimos)
        except SourceUnavailable as exc:
            errors.append(f"série {codigo}: {exc}")
            continue
        result[codigo] = upsert_macro(db, points, nome=meta["nome"], source="bcb_sgs")
        if points:
            last = max([last or "", *(p.data for p in points)])
    if errors and not result:
        set_source_status(db, "bcb_sgs", "falhou", "; ".join(errors))
    elif errors:
        set_source_status(db, "bcb_sgs", "atrasada", "; ".join(errors), last)
    else:
        set_source_status(db, "bcb_sgs", "ok", f"séries {codigos}", last)
    return result
