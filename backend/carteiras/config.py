"""Leitura e validação dos arquivos de configuração. Nenhum valor de perfil mora no código."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from . import settings


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Horizonte(_Strict):
    min_dias: int
    max_dias: int


class LiquidezMinima(_Strict):
    volume_financeiro_medio_20d: float


class UniversoPerfil(_Strict):
    origem: str
    liquidez_minima: LiquidezMinima
    tickers: list[str] = Field(default_factory=list)  # preenchido a partir de universe.yaml


class Limites(_Strict):
    posicoes_min: int
    posicoes_max: int
    max_por_posicao_pct: float
    max_por_setor_pct: float
    caixa_minimo_pct: float
    stop_padrao_pct: float
    drawdown_max_carteira_pct: float
    drawdown_retomada_pct: float
    giro_max_mensal_pct: float


class Execucao(_Strict):
    momento: Literal["abertura_d_mais_1", "fechamento_d_mais_1"]
    slippage_pct: float
    corretagem_brl: float
    emolumentos_pct: float
    liquidacao_pct: float
    tick_brl: float


class Radar(_Strict):
    janela_volume_dias: int
    janela_atr_dias: int
    janela_variacao_dias: int
    janela_rompimento_curta_dias: int
    janela_rompimento_longa_dias: int
    mult_critico: float
    variacao_diaria_atencao_pct: float
    variacao_diaria_critico_pct: float
    variacao_janela_atencao_pct: float
    volume_relativo_atencao: float
    volume_relativo_critico: float
    gap_abertura_atencao_pct: float
    range_vs_atr_atencao: float
    queda_vs_ibov_atencao_pct: float
    proximidade_stop_atencao_pct: float
    proximidade_alvo_atencao_pct: float
    drawdown_posicao_atencao_pct: float
    drawdown_carteira_atencao_pct: float
    ibov_variacao_critica_pct: float
    mercado_queda_forte_pct: float
    mercado_fracao_universo_pct: float
    salto_sem_evento_pct: float
    dias_para_data_com_atencao: int
    defasagem_max_pregoes: int
    cooldown_pregoes: int


class Revisao(_Strict):
    rotina_a_cada_pregoes: int
    rebalanceamento: str


class Orcamento(_Strict):
    escalonamentos_max_por_dia: int
    chamadas_ia_max_por_dia: int
    tokens_ia_max_por_dia: int


class Profile(_Strict):
    id: str
    nome: str
    status_parametros: str
    descricao: str
    horizonte: Horizonte
    capital_inicial: float
    benchmarks: list[str]
    universo: UniversoPerfil
    limites: Limites
    execucao: Execucao
    radar: Radar
    revisao: Revisao
    orcamento: Orcamento

    @property
    def provisorio(self) -> bool:
        return self.status_parametros.upper().startswith("PROVIS")

    def version_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Ativo(_Strict):
    nome: str
    setor: str
    cvm: list[str] = Field(default_factory=list)


class Indice(_Strict):
    codigo: str
    nome: str
    simbolo_brapi: str


class Universe(_Strict):
    indice: Indice
    ativos: dict[str, Ativo]
    perfis: dict[str, dict[str, list[str]]]

    def setor(self, ticker: str) -> str:
        ativo = self.ativos.get(ticker)
        return ativo.setor if ativo else "Sem setor"

    def all_tickers(self) -> list[str]:
        seen: dict[str, None] = {}
        for spec in self.perfis.values():
            for t in spec["tickers"]:
                seen.setdefault(t, None)
        return list(seen)


class BrapiBudget(_Strict):
    cota_mensal: int
    fracao_maxima: float
    requisicoes_por_escalonamento: int


class Prioridade(_Strict):
    ordem_severidade: list[str]
    carteira_antes_da_watchlist: bool


class Budgets(_Strict):
    brapi: BrapiBudget
    prioridade: Prioridade


class PosicaoInicial(_Strict):
    ticker: str
    peso_pct: float
    alvo_pct: float | None = None
    stop_pct: float | None = None
    tese: str


class GatilhoInicial(_Strict):
    descricao: str
    tipo: str
    valor: float
    tickers: list[str] = Field(default_factory=list)
    acao: str
    fracao: float


class Confianca(_Strict):
    valor: float
    justificativa: str


class CarteiraInicial(_Strict):
    leitura_de_mercado: str
    confianca: Confianca
    riscos: list[str]
    posicoes: list[PosicaoInicial]
    gatilhos: list[GatilhoInicial] = Field(default_factory=list)


class DecisaoInicialMeta(_Strict):
    agente: str
    data_decisao: str


class InitialPortfolios(_Strict):
    decisao_inicial: DecisaoInicialMeta
    perfis: dict[str, CarteiraInicial]


@dataclass
class Config:
    profiles: dict[str, Profile]
    universe: Universe
    budgets: Budgets
    initial: InitialPortfolios
    calendar_closed: set[date] = field(default_factory=set)
    calendar_open: set[date] = field(default_factory=set)
    directory: Path | None = None

    def profile_ids(self) -> list[str]:
        return sorted(self.profiles)


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de configuração ausente: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _dates(values: Any) -> set[date]:
    return {v if isinstance(v, date) else date.fromisoformat(str(v)) for v in (values or [])}


def load_config(directory: Path | None = None) -> Config:
    base = directory or settings.config_dir()
    raw_profiles = _read_yaml(base / "profiles.yaml")["perfis"]
    universe = Universe.model_validate(_read_yaml(base / "universe.yaml"))
    profiles: dict[str, Profile] = {}
    for item in raw_profiles:
        profile = Profile.model_validate(item)
        spec = universe.perfis.get(profile.id)
        if spec is None:
            raise ValueError(f"universe.yaml não define o perfil {profile.id}")
        profile.universo.tickers = list(spec["tickers"])
        unknown = [t for t in profile.universo.tickers if t not in universe.ativos]
        if unknown:
            raise ValueError(f"Perfil {profile.id}: tickers sem classificação em universe.yaml: {unknown}")
        profiles[profile.id] = profile
    budgets = Budgets.model_validate(_read_yaml(base / "budgets.yaml"))
    initial = InitialPortfolios.model_validate(_read_yaml(base / "initial_portfolios.yaml"))
    overrides_path = base / "calendario_overrides.yaml"
    overrides = _read_yaml(overrides_path) if overrides_path.is_file() else {}
    overrides = overrides or {}
    return Config(
        profiles=profiles,
        universe=universe,
        budgets=budgets,
        initial=initial,
        calendar_closed=_dates(overrides.get("fechados")),
        calendar_open=_dates(overrides.get("abertos")),
        directory=base,
    )
