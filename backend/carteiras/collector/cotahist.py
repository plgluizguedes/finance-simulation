"""Leitura do arquivo COTAHIST da B3 (registro tipo 01, 245 bytes por linha).

IMPORTANTE (R7): a B3 veda a coleta automatizada desses arquivos. Este módulo NÃO baixa nada.
Uma pessoa baixa o arquivo no site da B3 e o coloca em data/inbox/; aqui só se lê o arquivo local.

Layout conferido contra o PDF oficial SeriesHistoricas_Layout.pdf. Os preços NÃO são ajustados por proventos.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from ..db import Database
from ..eventlog import log_event, log_system
from .base import PriceBar
from .store import set_source_status, upsert_prices

LINE_LEN = 245


def _int(text: str) -> int:
    text = text.strip()
    return int(text) if text else 0


def parse_line(line: str) -> PriceBar | None:
    """Converte uma linha do registro 01 em PriceBar. Devolve None para o que não interessa."""
    if len(line) < LINE_LEN - 3 or line[0:2] != "01":
        return None
    codbdi = line[10:12]
    tpmerc = line[24:27]
    # Só mercado à vista, lote padrão. O fracionário (BDI 96, TPMERC 020) duplicaria volume.
    if tpmerc != "010" or codbdi != "02":
        return None
    if _int(line[210:217]) != 1:  # FATCOT: ações negociam por unidade
        return None
    try:
        pregao = datetime.strptime(line[2:10], "%Y%m%d").strftime("%Y-%m-%d")
        close = _int(line[108:121]) / 100
        if close <= 0:
            return None
        return PriceBar(
            ticker=line[12:24].strip(),
            pregao=pregao,
            open=_int(line[56:69]) / 100,
            high=_int(line[69:82]) / 100,
            low=_int(line[82:95]) / 100,
            close=close,
            volume_fin=_int(line[170:188]) / 100,
            quantidade=float(_int(line[152:170])),
            negocios=_int(line[147:152]),
            source="cotahist",
        )
    except ValueError:
        return None


def parse_lines(lines: Iterable[str], tickers: set[str] | None = None) -> Iterator[PriceBar]:
    for raw in lines:
        bar = parse_line(raw.rstrip("\r\n"))
        if bar is not None and (tickers is None or bar.ticker in tickers):
            yield bar


def read_file(path: Path, tickers: set[str] | None = None) -> Iterator[PriceBar]:
    """Lê um .txt ou .zip do COTAHIST local (codificação latin-1)."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".txt"):
                    with zf.open(name) as raw:
                        yield from parse_lines(io.TextIOWrapper(raw, encoding="latin-1"), tickers)
    else:
        with path.open("r", encoding="latin-1") as fh:
            yield from parse_lines(fh, tickers)


def import_inbox(db: Database, inbox: Path, tickers: set[str], *, index_codes: set[str] | None = None) -> dict[str, int]:
    """Importa todos os arquivos COTAHIST (.zip/.txt) que estiverem na caixa de entrada."""
    summary = {"arquivos": 0, "barras": 0, "ignoradas": 0}
    files = sorted(p for p in inbox.glob("*") if p.suffix.lower() in (".zip", ".txt")) if inbox.is_dir() else []
    last_day: str | None = None
    for path in files:
        bars = list(read_file(path, tickers | (index_codes or set())))
        written, skipped = upsert_prices(db, bars)
        summary["arquivos"] += 1
        summary["barras"] += written
        summary["ignoradas"] += skipped
        if bars:
            last_day = max([last_day or "", *(b.pregao for b in bars)])
        log_event(
            db, origem="sistema", tipo="IMPORTACAO_COTAHIST", motivo=f"arquivo {path.name}",
            payload={"barras": written, "ignoradas": skipped},
        )
    if summary["arquivos"] == 0:
        set_source_status(db, "cotahist", "nao_configurada", "nenhum arquivo em data/inbox (importação manual)")
        log_system(db, "INFO", "cotahist", "caixa de entrada vazia; baixe o COTAHIST no site da B3 e coloque em data/inbox")
    else:
        set_source_status(db, "cotahist", "ok", f"{summary['arquivos']} arquivo(s) importado(s) manualmente", last_day)
    return summary


def make_line(
    ticker: str, pregao: str, o: float, h: float, low: float, c: float, volume_fin: float,
    quantidade: int = 1000, negocios: int = 50, bdi: str = "02", tpmerc: str = "010",
) -> str:
    """Monta uma linha no layout oficial. Usada só em testes (arquivo SINTÉTICO, nunca dado real)."""

    def num(v: float, width: int) -> str:
        return str(round(v * 100)).zfill(width)

    line = (
        "01" + pregao.replace("-", "") + bdi + ticker.ljust(12) + tpmerc + "EMPRESA TESTE"[:12].ljust(12) + "ON".ljust(10)
        + "   " + "R$  " + num(o, 13) + num(h, 13) + num(low, 13) + num(c, 13) + num(c, 13) + num(c, 13) + num(c, 13)
        + str(negocios).zfill(5) + str(quantidade).zfill(18) + num(volume_fin, 18) + num(0, 13) + "0" + "99991231"
        + "0000001" + "0000000000000" + ("BRTESTACNOR0").ljust(12) + "100"
    )
    assert len(line) == LINE_LEN, len(line)
    return line
