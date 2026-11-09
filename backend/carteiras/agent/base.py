"""Interface Agent e ManualAgent."""

from __future__ import annotations

from typing import Any, Protocol

from .schema import RegistroDecisao


class Agent(Protocol):
    nome: str
    origem: str          # manual | mock | agente
    is_mock: bool

    def analyze(self, dossie: dict[str, Any], contexto: dict[str, Any]) -> RegistroDecisao | dict[str, Any] | None:
        """Recebe o dossiê e devolve um Registro de Decisão (ou dict a validar). None = sem parecer."""
        ...


class ManualAgent:
    """Pareceres escritos por uma pessoa. Sem parecer na fila para (perfil, ativo), não há decisão: o sistema registra 'nenhuma ação'."""

    nome = "manual"
    origem = "manual"
    is_mock = False

    def __init__(self) -> None:
        self._fila: dict[tuple[str, str], list[dict[str, Any] | RegistroDecisao]] = {}

    def submit(self, profile_id: str, ticker: str, registro: dict[str, Any] | RegistroDecisao) -> None:
        self._fila.setdefault((profile_id, ticker), []).append(registro)

    def analyze(self, dossie: dict[str, Any], contexto: dict[str, Any]) -> RegistroDecisao | dict[str, Any] | None:
        key = (dossie["identificacao"]["perfil"], dossie["identificacao"].get("ticker") or "*")
        fila = self._fila.get(key)
        return fila.pop(0) if fila else None
