"""MockAgent: decide por regras simples e determinísticas sobre o dossiê. NÃO é inteligência: serve para exercitar o pipeline
inteiro (dossiê → decisão → regras → ordens → gatilhos). Tudo o que produz é rotulado `mock` e `is_mock`.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .schema import RegistroDecisao

NUM = (int, float)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, NUM) and not isinstance(v, bool) else None


def _fmt(v: Any) -> str:
    return "sem dado" if v is None else (f"{v:.4g}" if isinstance(v, float) else str(v))


class MockAgent:
    nome = "mock-regras"
    origem = "mock"
    is_mock = True

    def analyze(self, dossie: dict[str, Any], contexto: dict[str, Any]) -> RegistroDecisao | dict[str, Any] | None:
        ident = dossie["identificacao"]
        tipo, ticker = ident["tipo"], ident.get("ticker")
        seed = int(hashlib.sha256(f"{ident['perfil']}|{ident['pregao']}|{ticker}|{tipo}".encode()).hexdigest()[:8], 16)
        if tipo == "REVISAO":
            return self._revisao(dossie, seed)
        return self._alerta(dossie, seed)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _base(dossie: dict[str, Any], seed: int, leitura: str, hip: tuple[str, str], p: float, conf: float, conf_txt: str) -> dict[str, Any]:
        ident = dossie["identificacao"]
        dados = [f"{a['tipo']} ({a['severidade']}): medido {_fmt(a['medido'])} contra limiar {_fmt(a['limiar'])} [alerta #{a['id']}]"
                 for a in ident["alertas"]]
        ind = (dossie.get("ativo") or {}).get("indicadores", {})
        sinais = [{"indicador": k, "valor": v if isinstance(v, (int, float, str)) else None,
                   "referencia": f"prices_adj {ident.get('ticker')} até {ident['pregao']}"} for k, v in ind.items()]
        passado = dossie.get("decisoes_recentes", [])
        licoes = dossie.get("licoes_relevantes", [])
        return {
            "leitura_de_mercado": f"[MOCK] {leitura}",
            "hipoteses": [
                {"descricao": hip[0], "evidencias_a_favor": dados[:2], "evidencias_contra": [], "probabilidade": round(p, 2)},
                {"descricao": hip[1], "evidencias_a_favor": [], "evidencias_contra": dados[:1], "probabilidade": round(max(0.0, 0.9 - p), 2)},
            ],
            "sinais": sinais,
            "decisao": [],
            "confianca": {"valor": round(conf, 2), "justificativa": f"[MOCK] {conf_txt}"},
            "alternativas_descartadas": [],
            "riscos_identificados": ["[MOCK] parecer gerado por regra fixa, não por análise de mercado"],
            "plano_futuro": {"passos": [], "gatilhos": []},
            "o_que_mudaria_minha_opiniao": [],
            "influencia_do_passado": {
                "decisoes_ids": [d["id"] for d in passado], "licoes_ids": [lc["id"] for lc in licoes],
                "como_pesaram": "[MOCK] listadas como contexto; a regra fixa não as pondera",
            },
            "dado_vs_inferencia": {"dados": dados, "inferencias": ["[MOCK] a leitura do cenário é uma regra, não uma inferência de modelo"]},
            "origem": "mock",
        }

    # ------------------------------------------------------------------ alertas
    def _alerta(self, d: dict[str, Any], seed: int) -> dict[str, Any]:
        ident = d["identificacao"]
        ticker = ident.get("ticker")
        alerts = ident["alertas"]
        tipos = {a["tipo"] for a in alerts}
        crit = any(a["severidade"] == "CRITICO" for a in alerts)
        pos = d.get("posicao")
        ind = (d.get("ativo") or {}).get("indicadores", {})
        var1 = _num(ind.get("variacao_1d_pct"))
        vol = _num(ind.get("volume_relativo"))
        lim = d["carteira"]["limites"]
        conf = 0.45 + (seed % 25) / 100

        if not ticker or ticker == "*":
            r = self._base(d, seed, "alerta no nível da carteira ou do mercado; sem ordem automática.",
                           ("o movimento é transitório", "o movimento sinaliza mudança de regime"), 0.55, conf,
                           "regra fixa para alertas sem ativo específico")
            r["decisao"] = [{"ticker": "*", "acao": "nenhuma", "justificativa": "[MOCK] alerta de carteira/mercado: apenas registrado; as regras do perfil já agem sobre limites e drawdown"}]
            r["alternativas_descartadas"] = [{"descricao": "reduzir exposição geral", "motivo_descarte": "[MOCK] sem base para dimensionar sem escolher ativo"}]
            r["o_que_mudaria_minha_opiniao"] = ["[MOCK] persistência do alerta por mais de um pregão"]
            return r

        if pos and "LIMITE_POSICAO" in tipos:
            a = next(x for x in alerts if x["tipo"] == "LIMITE_POSICAO")
            peso, limite = _num(a["medido"]) or 0.0, _num(a["limiar"]) or 0.0
            frac = round(min(0.9, max(0.05, (peso - limite) / peso)), 2) if peso else 0.1
            r = self._base(d, seed, f"{ticker} passou do limite de posição do perfil ({peso:.2f}% contra {limite:g}%).",
                           ("reduzir devolve a posição ao limite", "esperar a valorização continuar"), 0.7, conf + 0.1, "o limite do perfil é regra, não opinião")
            r["decisao"] = [{"ticker": ticker, "acao": "reduzir", "tamanho": {"dimensionamento": "fracao_posicao", "valor": frac},
                             "justificativa": f"[MOCK] reduzir {frac:.0%} da posição para voltar ao limite de {limite:g}%"}]
            r["alternativas_descartadas"] = [{"descricao": "manter acima do limite", "motivo_descarte": "limite do perfil é obrigatório (R4)"}]
            r["o_que_mudaria_minha_opiniao"] = ["[MOCK] mudança do limite no perfil"]
            return r

        queda = var1 is not None and var1 < 0
        if pos and (("PROXIMIDADE_ALVO" in tipos) or (queda and (crit or "PROXIMIDADE_STOP" in tipos or "DRAWDOWN_POSICAO" in tipos))):
            alvo = "PROXIMIDADE_ALVO" in tipos
            r = self._base(d, seed, f"{ticker}: " + ("preço perto do alvo; realizar parte do ganho." if alvo else "queda relevante com posição aberta; reduzir risco."),
                           ("realizar parcialmente protege o resultado" if alvo else "reduzir limita a perda", "a tendência anterior se mantém"),
                           0.55, conf, "regra fixa: reduzir metade em alvo próximo ou em queda crítica")
            r["decisao"] = [{"ticker": ticker, "acao": "reduzir", "tamanho": {"dimensionamento": "fracao_posicao", "valor": 0.5},
                             "justificativa": "[MOCK] reduzir metade da posição"}]
            r["alternativas_descartadas"] = [{"descricao": "zerar a posição", "motivo_descarte": "[MOCK] regra fixa prefere saída parcial; o stop cobre o restante"}]
            r["riscos_identificados"].append(f"posição restante segue exposta; stop em {pos.get('stop')}")
            r["o_que_mudaria_minha_opiniao"] = ["[MOCK] recuperação acima da abertura do dia com volume relativo alto"]
            cm = _num(pos.get("custo_medio"))
            if cm and not alvo:
                r["plano_futuro"] = {"passos": [f"reavaliar {ticker} se a perda aumentar"], "gatilhos": [{
                    "descricao": f"{ticker}: perda de 3 pontos percentuais adicionais sobre o custo médio, sair do restante",
                    "condicao": {"tipo": "drawdown_posicao_acima", "ticker": ticker, "valor": round((_num(pos.get("pnl_pct")) or 0) * -1 + 3, 2)},
                    "invalida_se": {"tipo": "preco_fechamento_acima", "ticker": ticker, "valor": round(cm * 1.02, 2)},
                    "acao": {"acao": "vender", "ticker": ticker, "tamanho": {"dimensionamento": "fracao_posicao", "valor": 1.0}},
                }]}
            return r

        if pos:
            r = self._base(d, seed, f"{ticker}: alerta sem gatilho de ação na regra fixa; manter posição.",
                           ("o alerta não muda a tese", "o alerta antecipa deterioração"), 0.6, conf, "regra fixa: manter quando não há queda crítica nem alvo próximo")
            r["decisao"] = [{"ticker": ticker, "acao": "manter", "justificativa": "[MOCK] manter; stop e alvo seguem valendo"}]
            r["o_que_mudaria_minha_opiniao"] = ["[MOCK] alerta crítico de queda ou aproximação do stop"]
            return r

        # ativo da watchlist
        rompe = "ROMPIMENTO_ALTA_20" in tipos or "MAXIMA_52S" in tipos
        if crit and rompe and var1 is not None and var1 > 0 and vol is not None and vol >= 1.5:
            peso = round(min(3.0, lim["max_por_posicao_pct"] / 3), 2)
            r = self._base(d, seed, f"{ticker}: rompimento de máxima com volume relativo {vol:.2f}; posição inicial pequena.",
                           ("rompimento com volume tende a continuar", "rompimento falso e devolução do movimento"), 0.5, conf,
                           "regra fixa: compra pequena em rompimento com volume")
            r["decisao"] = [{"ticker": ticker, "acao": "comprar", "tamanho": {"dimensionamento": "peso_alvo_pct", "valor": peso},
                             "entrada": "abertura_d_mais_1", "stop_pct": lim["stop_padrao_pct"],
                             "justificativa": f"[MOCK] compra de {peso:g}% do patrimônio com stop de {lim['stop_padrao_pct']:g}%"}]
            r["alternativas_descartadas"] = [{"descricao": "esperar fechamento da semana", "motivo_descarte": "[MOCK] regra fixa não espera"}]
            r["riscos_identificados"].append("rompimento falso; o stop limita a perda ao padrão do perfil")
            r["o_que_mudaria_minha_opiniao"] = ["[MOCK] fechamento abaixo da máxima rompida"]
            return r

        r = self._base(d, seed, f"{ticker} (fora da carteira): alerta registrado; sem entrada pela regra fixa.",
                       ("o movimento é ruído", "o movimento antecipa tendência"), 0.5, conf, "regra fixa: sem entrada sem rompimento, volume e alta")
        r["decisao"] = [{"ticker": ticker, "acao": "nenhuma", "justificativa": "[MOCK] sem sinal suficiente para entrar"}]
        mx = _num(ind.get("maxima_20d"))
        if mx:
            r["plano_futuro"] = {"passos": [f"observar {ticker}"], "gatilhos": [{
                "descricao": f"{ticker}: fechar acima da máxima de 20 pregões ({mx:g}); entrada pequena",
                "condicao": {"tipo": "preco_fechamento_acima", "ticker": ticker, "valor": mx},
                "invalida_se": {"tipo": "data_apos", "data": _days_after(ident["pregao"], 15)},
                "acao": {"acao": "comprar", "ticker": ticker, "tamanho": {"dimensionamento": "peso_alvo_pct", "valor": round(min(3.0, lim["max_por_posicao_pct"] / 3), 2)},
                         "stop_pct": lim["stop_padrao_pct"]},
            }]}
        r["o_que_mudaria_minha_opiniao"] = ["[MOCK] rompimento da máxima de 20 pregões com volume"]
        return r

    # ------------------------------------------------------------------ revisão rotineira
    def _revisao(self, d: dict[str, Any], seed: int) -> dict[str, Any]:
        pos = d.get("posicoes_detalhe", [])
        r = self._base(d, seed, f"revisão rotineira de {len(pos)} posição(ões); nenhuma mudança pela regra fixa.",
                       ("a carteira segue de acordo com as teses", "alguma tese perdeu validade"), 0.6, 0.5 + (seed % 15) / 100,
                       "regra fixa: revisão sem ação")
        r["decisao"] = [{"ticker": p["ticker"], "acao": "manter", "justificativa": "[MOCK] revisão rotineira sem gatilho"} for p in pos] or \
                       [{"ticker": "*", "acao": "nenhuma", "justificativa": "[MOCK] carteira sem posições"}]
        r["o_que_mudaria_minha_opiniao"] = ["[MOCK] alerta crítico em qualquer posição"]
        return r


def _days_after(pregao: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(pregao) + timedelta(days=n)).isoformat()
