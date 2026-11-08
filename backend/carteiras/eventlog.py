"""Log de eventos append-only (R5) e logs técnicos estruturados, sempre com segredos mascarados (R1)."""

from __future__ import annotations

from typing import Any

from .db import Database, dumps, now_iso
from .settings import mask_secrets

ORIGENS = ("manual", "mock", "agente", "sistema")


def log_event(
    db: Database,
    *,
    origem: str,
    tipo: str,
    profile_id: str | None = None,
    pregao: str | None = None,
    motivo: str | None = None,
    ref: Any = None,
    payload: Any = None,
    is_mock: bool = False,
) -> int:
    if origem not in ORIGENS:
        raise ValueError(f"origem inválida: {origem!r}")
    cur = db.execute(
        "INSERT INTO event_log (ts, profile_id, origem, tipo, pregao, motivo, ref_json, payload_json, is_mock) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            profile_id,
            origem,
            tipo,
            pregao,
            mask_secrets(motivo) if motivo else None,
            mask_secrets(dumps(ref)) if ref is not None else None,
            mask_secrets(dumps(payload)) if payload is not None else None,
            1 if is_mock else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def log_system(db: Database, nivel: str, modulo: str, mensagem: str, extra: Any = None) -> None:
    db.execute(
        "INSERT INTO system_logs (ts, nivel, modulo, mensagem, extra_json) VALUES (?, ?, ?, ?, ?)",
        (
            now_iso(),
            nivel.upper(),
            modulo,
            mask_secrets(mensagem),
            mask_secrets(dumps(extra)) if extra is not None else None,
        ),
    )
