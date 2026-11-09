"""Carteiras iniciais. A decisão (origem `agente`, agente `claude-construcao`) vem de `config/initial_portfolios.yaml`.

Vira um Registro de Decisão comum, com ordens por peso-alvo (tipo CONSTRUCAO_INICIAL) agendadas para o pregão seguinte
e validadas pelas regras do perfil como qualquer outra ordem (R4).
"""

from __future__ import annotations

from typing import Any

from ..agent.apply import apply_registro, save_decision
from ..agent.schema import RegistroDecisao
from ..config import Config
from ..db import Database
from ..escalation.dossier import save_dossier
from ..simulation.broker import BrokerSim

TRIGGER_MAP = {"queda_desde_entrada_pct": "drawdown_posicao_acima"}
SEM_DADOS_AO_VIVO = "Decisão tomada sem cotações, balanços nem notícias ao vivo; a base é conhecimento geral."


def build_registro(cfg: Config, profile_id: str, pregao: str) -> RegistroDecisao:
    init = cfg.initial.perfis[profile_id]
    prof = cfg.profiles[profile_id]
    held = {p.ticker for p in init.posicoes}
    gatilhos: list[dict[str, Any]] = []
    for g in init.gatilhos:
        if g.tipo not in TRIGGER_MAP:
            raise ValueError(f"gatilho inicial de tipo desconhecido: {g.tipo!r}")
        for t in (g.tickers or sorted(held)):
            gatilhos.append({
                "descricao": f"{t}: {g.descricao}", "condicao": {"tipo": TRIGGER_MAP[g.tipo], "ticker": t, "valor": g.valor},
                "acao": {"acao": "reduzir" if g.acao == "reduzir" else "vender", "ticker": t,
                         "tamanho": {"dimensionamento": "fracao_posicao", "valor": g.fracao}},
            })
    total = sum(p.peso_pct for p in init.posicoes)
    raw = {
        "leitura_de_mercado": init.leitura_de_mercado.strip(),
        "hipoteses": [
            {"descricao": "O cenário de juros altos favorece empresas de caixa previsível e baixa alavancagem.",
             "evidencias_a_favor": ["Selic e CDI elevados conforme a série do Banco Central consultada na construção"],
             "evidencias_contra": [SEM_DADOS_AO_VIVO], "probabilidade": init.confianca.valor},
            {"descricao": "Os preços do primeiro pregão contradizem as teses e exigem revisão.",
             "evidencias_a_favor": [SEM_DADOS_AO_VIVO], "evidencias_contra": [], "probabilidade": round(1 - init.confianca.valor - 0.2, 2)},
        ],
        "sinais": [{"indicador": "peso total em ações (%)", "valor": total, "referencia": "config/initial_portfolios.yaml"},
                   {"indicador": "caixa resultante (%)", "valor": round(100 - total, 2), "referencia": "config/initial_portfolios.yaml"}],
        "decisao": [{
            "ticker": p.ticker, "acao": "comprar", "tamanho": {"dimensionamento": "peso_alvo_pct", "valor": p.peso_pct},
            "entrada": prof.execucao.momento, "stop_pct": p.stop_pct or prof.limites.stop_padrao_pct,
            **({"alvo_pct": p.alvo_pct} if p.alvo_pct else {}), "justificativa": p.tese,
        } for p in init.posicoes],
        "confianca": {"valor": init.confianca.valor, "justificativa": init.confianca.justificativa},
        "alternativas_descartadas": [
            {"descricao": "Concentrar em poucos ativos", "motivo_descarte": f"o perfil exige de {prof.limites.posicoes_min} a {prof.limites.posicoes_max} posições"},
            {"descricao": "Ficar apenas em caixa", "motivo_descarte": "a carteira existe para simular a alocação em ações; o caixa mínimo do perfil já protege parte do capital"},
        ],
        "riscos_identificados": init.riscos,
        "plano_futuro": {"passos": ["Acompanhar a execução do primeiro pregão e revisar teses que o preço real contradiga."], "gatilhos": gatilhos},
        "o_que_mudaria_minha_opiniao": ["Execução a preços muito distantes dos pressupostos da tese.",
                                        "Mudança relevante dos juros ou do cenário de cada setor."],
        "influencia_do_passado": {"decisoes_ids": [], "licoes_ids": [], "como_pesaram": "Primeira decisão: não há histórico."},
        "dado_vs_inferencia": {
            "dados": [f"Perfil {profile_id}: limites e universo lidos de config/profiles.yaml e config/universe.yaml"],
            "inferencias": [init.leitura_de_mercado.strip(), SEM_DADOS_AO_VIVO],
        },
        "origem": "agente",
    }
    return RegistroDecisao.model_validate(raw)


def apply_initial(db: Database, cfg: Config, broker: BrokerSim, profile_id: str, pregao: str, *, is_mock: bool) -> int:
    """Registra a decisão inicial e agenda as ordens de construção. Idempotente: uma decisão INICIAL por perfil."""
    existing = db.scalar("SELECT id FROM decisions WHERE profile_id = ? AND tipo = 'INICIAL'", (profile_id,))
    if existing:
        return int(existing)
    registro = build_registro(cfg, profile_id, pregao)
    meta = cfg.initial.decisao_inicial
    dossier = {
        "identificacao": {"perfil": profile_id, "tipo": "INICIAL", "ticker": None, "pregao": pregao, "is_mock": is_mock, "alertas": []},
        "nota": SEM_DADOS_AO_VIVO, "decisao_tomada_em": meta.data_decisao, "agente": meta.agente,
        "limites": cfg.profiles[profile_id].limites.model_dump(),
        "universo": cfg.profiles[profile_id].universo.tickers,
    }
    refs = [{"tabela": "config", "arquivo": "config/initial_portfolios.yaml"}, {"tabela": "config", "arquivo": "config/profiles.yaml"}]
    with db.transaction():
        dossier_id = save_dossier(db, profile_id, None, pregao, "INICIAL", dossier, refs, is_mock=is_mock)
        decision_id = save_decision(db, profile_id, pregao, None, "INICIAL", "agente", meta.agente, registro, dossier_id, is_mock=is_mock)
        apply_registro(db, cfg, broker, profile_id, pregao, registro, decision_id, origem="agente", execute_orders=True,
                       is_mock=is_mock, tipo_ordem="CONSTRUCAO_INICIAL")
    return decision_id
