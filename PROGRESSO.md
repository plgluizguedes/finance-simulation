# Andamento do projeto Carteiras B3

Arquivo de passagem de bastão. Quem for continuar (inclusive o Claude em outra sessão) lê este arquivo, roda os testes e segue pela primeira etapa que não esteja concluída.

Última atualização: 08/11/2026.

## O que é
Programa local que simula duas carteiras virtuais de ações da B3 (A: arrojada, médio/curto prazo; B: conservadora-arrojada, médio/longo prazo) e um dashboard para acompanhá-las. Sem corretora e sem ordens reais. A IA define os investimentos. O agente de IA em tempo de execução (Gemini) **não** faz parte desta etapa: só o encaixe e o `MockAgent`.

## Como retomar
1. `cd backend && python -m pytest -q` (tudo deve passar).
2. Ler a tabela de etapas abaixo e a seção "Pendências".
3. Continuar pela primeira etapa que não esteja concluída; ao terminar, atualizar este arquivo e fazer commit.

## Etapas

| Etapa | Situação |
|-------|----------|
| Fase 0: fontes e decisões (`docs/DATA_SOURCES.md`, `docs/DECISIONS.md`) | concluída |
| Núcleo: config, banco, calendário, coletores, ajuste | código pronto e testado por fumaça; testes automatizados pendentes |
| Simulação, regras, contabilidade | `rules/engine.py`, `simulation/broker.py` e `portfolio/metrics.py` prontos; testados por fumaça (execução D+1, stops, proventos, marcação a mercado, conciliação do patrimônio); testes automatizados pendentes |
| Radar, escalonamento, agente mock, memória, agendador | prontos e rodando de ponta a ponta com dados MOCK (70 pregões, 2 carteiras, reexecução sem duplicar); testes automatizados pendentes |
| Carteiras iniciais e seeds MOCK | carteiras iniciais prontas (`seeds/initial.py`); seeds MOCK de lições/calibração e CLI pendentes |
| API e dashboard (14 abas) | pendente |
| Testes, README e conferência final | pendente |

## Decisões que mudam o desenho (detalhe em `docs/DECISIONS.md`)
- A B3 veda coleta automatizada do COTAHIST: ele é importado à mão (`data/inbox/`). A coleta diária automática é pela brapi, por ativo.
- Starlette no lugar de FastAPI; esbuild no lugar de Vite; gráficos próprios com d3-scale e d3-shape (o ambiente de construção não tinha os pacotes e não tinha rede).
- Dois bancos: `data/mock.db` e `data/real.db`.

## Ações que só o dono do projeto pode fazer
- Baixar à mão os COTAHIST anuais no site da B3 e colocar em `data/inbox/`.
- Colocar o token da brapi no `.env` (opcional).
- Conferir contra a fonte oficial: taxas da B3 (`config/profiles.yaml`), calendário (`config/calendario_overrides.yaml`) e os tickers dos universos.

## Pendências e riscos conhecidos
- Nenhum arquivo real das fontes foi aberto durante a construção (sem rede no ambiente). Critérios 1, 2 e 4 da seção 10 do desenho original seguem abertos (ver `docs/DATA_SOURCES.md`).
- Formato de resposta da brapi não validado contra resposta real.

## Registro
- 08/11/2026: fontes testadas, decisões D1 a D13 escritas.
- 08/11/2026: config, banco (schema.sql), calendário, coletores (cotahist, bcb, cvm, brapi, mock), ajuste de preços, regras e corretora virtual escritos.
- 09/11/2026: `portfolio/metrics.py`, radar (`radar/`), escalonamento e dossiê (`escalation/`), agente (`agent/`: schema, MockAgent, ManualAgent, aplicação em ordens), memória e gatilhos (`memory/`), coleta real (`collector/pipeline.py`), carteiras iniciais (`seeds/initial.py`) e agendador (`scheduler/runner.py`) escritos. Rodada MOCK completa: 142 sessões processadas, patrimônio concilia com caixa + posições, caixa nunca negativo, reexecução sem duplicar.
  Próximos: seeds MOCK de memória, CLI (`python -m carteiras`), API Starlette, dashboard, testes pytest, README, D14 e D15 em `docs/DECISIONS.md`.
