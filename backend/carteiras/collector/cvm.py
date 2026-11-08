"""CVM, Dados Abertos, dataset IPE (Fato Relevante e Comunicado ao Mercado). Licença ODbL.

URL (listagem real conferida em 08/11/2026): https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip
O conteúdo do CSV NÃO foi aberto durante a construção; as colunas abaixo seguem a documentação do dataset
e o parser tolera colunas ausentes. O mapeamento ticker -> emissor é por trechos do nome (config/universe.yaml).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ..config import Universe
from ..db import Database
from .base import Filing, SourceUnavailable
from .http import HttpClient
from .store import set_source_status, upsert_filings

URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"
CATEGORIAS = {"FATO RELEVANTE", "COMUNICADO AO MERCADO"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9 .]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def build_name_index(universe: Universe) -> list[tuple[str, str]]:
    """Lista (trecho normalizado, ticker). Trechos mais longos primeiro, para o mais específico vencer."""
    pairs = [(normalize(k), ticker) for ticker, a in universe.ativos.items() for k in a.cvm]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def map_ticker(nome_emissor: str, index: list[tuple[str, str]]) -> str | None:
    name = normalize(nome_emissor)
    for fragment, ticker in index:
        if fragment and fragment in name:
            return ticker
    return None


def _date(value: str) -> str:
    value = (value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_ipe_csv(text: str, index: list[tuple[str, str]], categorias: set[str] | None = None) -> Iterator[Filing]:
    wanted = categorias or CATEGORIAS
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    for row in reader:
        categoria = (row.get("Categoria") or "").strip()
        if normalize(categoria) not in wanted:
            continue
        nome = (row.get("Nome_Companhia") or "").strip()
        protocolo = (row.get("Protocolo_Entrega") or "").strip()
        versao = (row.get("Versao") or "").strip()
        if not protocolo:
            continue
        yield Filing(
            protocolo=f"{protocolo}-{versao}" if versao else protocolo,
            cnpj=(row.get("CNPJ_Companhia") or "").strip(),
            nome_emissor=nome,
            categoria=categoria,
            assunto=(row.get("Assunto") or "").strip(),
            data_referencia=_date(row.get("Data_Referencia") or ""),
            data_entrega=_date(row.get("Data_Entrega") or ""),
            link=(row.get("Link_Download") or "").strip(),
            ticker=map_ticker(nome, index),
        )


def read_ipe_zip(content: bytes, index: list[tuple[str, str]]) -> list[Filing]:
    filings: list[Filing] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".csv"):
                filings.extend(parse_ipe_csv(zf.read(name).decode("latin-1"), index))
    return filings


def collect_ipe(db: Database, universe: Universe, ano: int, http: HttpClient | Any | None = None) -> dict[str, int]:
    """Baixa o IPE do ano (arquivo aberto, sem cota) e grava os documentos dos emissores do universo."""
    http = http or HttpClient(min_interval=1.0)
    try:
        content = http.get_bytes(URL.format(ano=ano))
        filings = read_ipe_zip(content, build_name_index(universe))
    except SourceUnavailable as exc:
        set_source_status(db, "cvm_ipe", "falhou", str(exc))
        return {"lidos": 0, "gravados": 0, "sem_ticker": 0}
    mapped = [f for f in filings if f.ticker]
    written = upsert_filings(db, mapped, source="cvm_ipe")
    last = max((f.data_entrega for f in mapped if f.data_entrega), default=None)
    set_source_status(db, "cvm_ipe", "ok", f"IPE {ano}: {len(mapped)} documentos de emissores do universo", last)
    return {"lidos": len(filings), "gravados": written, "sem_ticker": len(filings) - len(mapped)}
