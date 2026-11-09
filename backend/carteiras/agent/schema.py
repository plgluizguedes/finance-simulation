"""Registro de Decisão: o contrato entre o agente e o sistema (seção 8 do desenho).

Resposta fora do schema vira 'nenhuma ação' registrada (ver `coerce_registro`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ACOES = ("comprar", "vender", "manter", "reduzir", "nenhuma")
CONDICOES = ("preco_fechamento_acima", "preco_fechamento_abaixo", "data_apos", "volume_relativo_acima", "drawdown_posicao_acima")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hipotese(_M):
    descricao: str
    evidencias_a_favor: list[str] = Field(default_factory=list)
    evidencias_contra: list[str] = Field(default_factory=list)
    probabilidade: float = Field(ge=0, le=1)


class Sinal(_M):
    indicador: str
    valor: float | str | None
    referencia: str | None = None          # de onde veio o número (R8)


class Tamanho(_M):
    dimensionamento: Literal["quantidade", "peso_alvo_pct", "fracao_posicao"]
    valor: float = Field(gt=0)


class DecisaoAtivo(_M):
    ticker: str
    acao: Literal["comprar", "vender", "manter", "reduzir", "nenhuma"]
    tamanho: Tamanho | None = None
    entrada: Literal["abertura_d_mais_1", "fechamento_d_mais_1"] | None = None
    stop_pct: float | None = Field(default=None, gt=0, lt=100)
    alvo_pct: float | None = Field(default=None, gt=0)
    justificativa: str = ""

    @model_validator(mode="after")
    def _coerente(self) -> DecisaoAtivo:
        if self.acao in ("comprar", "reduzir") and self.tamanho is None:
            raise ValueError(f"ação '{self.acao}' exige tamanho")
        if self.acao in ("manter", "nenhuma") and self.tamanho is not None:
            raise ValueError(f"ação '{self.acao}' não leva tamanho")
        if self.acao != "comprar" and (self.stop_pct is not None or self.alvo_pct is not None):
            raise ValueError("stop e alvo só se definem na compra")
        return self


class Confianca(_M):
    valor: float = Field(ge=0, le=1)
    justificativa: str


class Alternativa(_M):
    descricao: str
    motivo_descarte: str


class Condicao(_M):
    tipo: Literal["preco_fechamento_acima", "preco_fechamento_abaixo", "data_apos", "volume_relativo_acima", "drawdown_posicao_acima"]
    ticker: str | None = None
    valor: float | None = None
    data: str | None = None

    @model_validator(mode="after")
    def _completa(self) -> Condicao:
        if self.tipo == "data_apos":
            if not self.data:
                raise ValueError("data_apos exige 'data' (AAAA-MM-DD)")
        else:
            if self.valor is None or not self.ticker:
                raise ValueError(f"{self.tipo} exige 'ticker' e 'valor'")
        return self


class AcaoPlanejada(_M):
    acao: Literal["comprar", "vender", "reduzir"]
    ticker: str
    tamanho: Tamanho
    stop_pct: float | None = Field(default=None, gt=0, lt=100)
    alvo_pct: float | None = Field(default=None, gt=0)


class Gatilho(_M):
    descricao: str
    condicao: Condicao                      # verificável por código
    invalida_se: Condicao | None = None     # condição de invalidação, também por código
    acao: AcaoPlanejada


class PlanoFuturo(_M):
    passos: list[str] = Field(default_factory=list)
    gatilhos: list[Gatilho] = Field(default_factory=list)


class Influencia(_M):
    decisoes_ids: list[int] = Field(default_factory=list)
    licoes_ids: list[int] = Field(default_factory=list)
    como_pesaram: str = ""


class DadoVsInferencia(_M):
    dados: list[str] = Field(default_factory=list)
    inferencias: list[str] = Field(default_factory=list)


class RegistroDecisao(_M):
    leitura_de_mercado: str
    hipoteses: list[Hipotese]
    sinais: list[Sinal]
    decisao: list[DecisaoAtivo]
    confianca: Confianca
    alternativas_descartadas: list[Alternativa] = Field(default_factory=list)
    riscos_identificados: list[str] = Field(default_factory=list)
    plano_futuro: PlanoFuturo = Field(default_factory=PlanoFuturo)
    o_que_mudaria_minha_opiniao: list[str] = Field(default_factory=list)
    influencia_do_passado: Influencia = Field(default_factory=Influencia)
    dado_vs_inferencia: DadoVsInferencia = Field(default_factory=DadoVsInferencia)
    origem: Literal["manual", "mock", "agente"]

    @field_validator("hipoteses")
    @classmethod
    def _ao_menos_uma(cls, v: list[Hipotese]) -> list[Hipotese]:
        if not v:
            raise ValueError("ao menos uma hipótese")
        return v


def registro_nenhuma_acao(origem: str, motivo: str) -> RegistroDecisao:
    """Registro formal de 'nenhuma ação', usado quando o agente não respondeu ou respondeu fora do schema."""
    return RegistroDecisao(
        leitura_de_mercado=motivo,
        hipoteses=[Hipotese(descricao="sem análise válida", probabilidade=0.0)],
        sinais=[], decisao=[DecisaoAtivo(ticker="*", acao="nenhuma", justificativa=motivo)],
        confianca=Confianca(valor=0.0, justificativa="sem análise válida"),
        dado_vs_inferencia=DadoVsInferencia(dados=[], inferencias=[]), origem=origem,  # type: ignore[arg-type]
    )


def coerce_registro(raw: Any, origem: str) -> tuple[RegistroDecisao, str | None]:
    """Valida a resposta do agente. Fora do schema: devolve 'nenhuma ação' e o erro (que deve ser registrado)."""
    if raw is None:
        return registro_nenhuma_acao(origem, "agente não devolveu parecer"), "sem resposta"
    if isinstance(raw, RegistroDecisao):
        return raw, None
    try:
        return RegistroDecisao.model_validate(raw), None
    except ValidationError as exc:
        resumo = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        return registro_nenhuma_acao(origem, f"resposta fora do schema, nenhuma ação tomada ({resumo})"), resumo
