"""Consultas de leitura sobre preços e macro. Sempre 'como de' uma data: nunca devolvem dado posterior a ela."""

from __future__ import annotations

from typing import Any

from .db import Database

Bar = dict[str, Any]


def bars_adj(db: Database, ticker: str, until: str, n: int) -> list[Bar]:
    rows = db.query(
        "SELECT * FROM prices_adj WHERE ticker = ? AND pregao <= ? ORDER BY pregao DESC LIMIT ?", (ticker, until, n))
    return list(reversed(rows))


def bars_raw(db: Database, ticker: str, until: str, n: int) -> list[Bar]:
    rows = db.query(
        "SELECT * FROM prices_raw WHERE ticker = ? AND pregao <= ? ORDER BY pregao DESC LIMIT ?", (ticker, until, n))
    return list(reversed(rows))


def bar_raw(db: Database, ticker: str, pregao: str) -> Bar | None:
    return db.one("SELECT * FROM prices_raw WHERE ticker = ? AND pregao = ?", (ticker, pregao))


def last_raw_before(db: Database, ticker: str, pregao: str) -> Bar | None:
    return db.one("SELECT * FROM prices_raw WHERE ticker = ? AND pregao < ? ORDER BY pregao DESC LIMIT 1", (ticker, pregao))


def macro_latest(db: Database, codigo: int, until: str) -> dict[str, Any] | None:
    return db.one("SELECT data, valor, nome, is_mock FROM macro_series WHERE codigo = ? AND data <= ? ORDER BY data DESC LIMIT 1",
                  (codigo, until))


def available_until(db: Database) -> str | None:
    """Último pregão com dados coletados (usa o índice como referência; sem índice, qualquer ativo)."""
    value = db.scalar("SELECT MAX(pregao) FROM prices_raw WHERE ticker = 'IBOV'")
    return value or db.scalar("SELECT MAX(pregao) FROM prices_raw")
