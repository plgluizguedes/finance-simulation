# Decisões técnicas

Cada decisão tem o motivo e o que ela custa. Quem decidiu, quando não está dito, fui eu (Claude), construindo o projeto em 08/11/2026.

## D1. Backend em Starlette, não FastAPI
FastAPI e httpx não estavam instalados no ambiente de construção e não havia rede para instalar. Starlette (a base do FastAPI), Uvicorn, sse-starlette, Pydantic, PyYAML e requests estavam, e tudo precisava ser executado e testado. As rotas são funções simples; migrar para FastAPI depois é direto. Custo: sem documentação OpenAPI automática, por isso `docs/API.md` é escrito à mão.

## D2. Frontend em React + TypeScript, com esbuild e gráficos próprios
Vite, Recharts e lightweight-charts não estavam disponíveis. React 19, TypeScript, esbuild e as peças do d3 (escalas e formas) estavam. O build usa esbuild e os gráficos (linhas, áreas, candles, barras) são SVG desenhado com escalas do d3. Custo: os candles e o cursor são código nosso. Como não há `@types/react` aqui, a checagem do TypeScript foi feita em modo frouxo; no `package.json` os tipos estão declarados para a checagem completa na sua máquina.

## D3. SQLite sem ORM, e dois bancos separados
O acesso é por uma camada fina (`Database`) sobre o `sqlite3` da biblioteca padrão; o `schema.sql` evita recursos exclusivos do SQLite sempre que possível. Há **dois arquivos de banco**: `data/mock.db` e `data/real.db`. Dado MOCK e dado real nunca dividem arquivo, o que torna a mistura silenciosa impossível. As tabelas também têm a coluna `is_mock`, como pede o projeto.

## D4. COTAHIST da B3 não é coletado por script
A página oficial das séries históricas proíbe "coleta automatizada, inclusive por meio de ferramentas de inteligência artificial". Pela regra R7 não contornei. O desenho original previa esse arquivo como fonte em lote; por isso:
- a **coleta diária automática** usa a brapi, por ativo, apenas para tickers das carteiras e da watchlist (cerca de 660 requisições por mês para 30 tickers);
- o COTAHIST é **importado à mão**: a pessoa baixa, coloca em `data/inbox/` e roda `python -m carteiras import-cotahist`;
- BCB e CVM continuam automáticos (dados abertos).
Custo: indicadores de 52 semanas e médias longas dependem do arquivo importado ou de acumular cache; enquanto faltar, o alerta mostra "sem dado".

## D5. yfinance não implementado
É uma fonte não oficial, de termos incertos, e não estava instalada. A interface `DataProvider` deixa o encaixe pronto; uma classe de plano B pode ser adicionada sem mexer no resto.

## D6. As duas carteiras iniciais foram escolhidas por mim
Por instrução do dono do projeto, a IA decide os investimentos. Defini os universos e as carteiras iniciais em `config/universe.yaml` e `config/initial_portfolios.yaml`, e registrei cada uma como Registro de Decisão (`origem = agente`, `agente = claude-construcao`). Limites importantes:
- foi feito **sem cotações ao vivo**: usei conhecimento geral até meados de 2026 e o cenário de juros altos (Selic em 13,75%, CDI diário de 0,050788%, conferido no SGS);
- as ordens são por **peso-alvo** e são dimensionadas no preço real de abertura do primeiro pregão seguinte, passando pelo módulo `rules`;
- os tickers precisam passar pelo filtro de liquidez na primeira coleta real; quem não passar é excluído e registrado;
- não é recomendação de investimento.

## D7. Fases executadas em sequência, sem pausa
O desenho original manda parar ao fim de cada fase e esperar. O dono do projeto pediu que eu fizesse tudo sem perguntar e só desse retorno ao terminar. Segui a instrução mais recente. Os documentos de cada fase continuam existindo como entregas.

## D8. Calendário B3 calculado
Feriados fixos e móveis (Carnaval, Sexta-feira Santa, Corpus Christi) são calculados a partir da Páscoa, mais 24 e 31/12 sem pregão. Pode divergir do calendário oficial em dias específicos. Por isso existe `config/calendario_overrides.yaml` para forçar dias fechados ou abertos. Status: A_CONFERIR.

## D9. Custos B3 conservadores
`emolumentos_pct` e `liquidacao_pct` foram colocados acima do que acredito ser a tabela atual, para a simulação não ficar otimista. Status: A_CONFERIR na fonte da B3. Corretagem zero, como no perfil provisório.

## D10. Proventos creditados brutos
Dividendos e JCP entram pelo valor bruto, sem retenção de imposto (impostos estão fora do escopo desta etapa). Isso aparece na aba de ordens e na de desempenho.

## D11. Quantidades
Quantidades são inteiras. Lote padrão de 100; o resto vai ao fracionário (sufixo F), com o mesmo preço e os mesmos custos. Tick de R$ 0,01.

## D12. Mock e dados reais
O modo é escolhido pela variável `CARTEIRAS_MODE` (`mock` ou `real`, padrão `mock`). O dashboard mostra o modo em uma faixa fixa. O mock é determinístico (semente fixa) e é processado pelo mesmo pipeline do modo real.

## D13. Tempo real
O stream usa SSE com o `event_log` como fonte: a API consulta o maior id do log a cada segundo e envia o que for novo. Funciona mesmo quando a rodada roda em outro processo.
