# Fontes de dados: o que foi testado de verdade

Relatório da Fase 0, escrito em 08/11/2026. A regra aqui é a R2 do projeto: só afirmo como validado o que foi de fato testado. O resto aparece como **não validado**, com o motivo.

## Limite do ambiente de construção

O ambiente onde este projeto foi construído **não tem acesso de saída à internet pelo shell** (toda requisição por `curl` retorna HTTP 000) e não consegue instalar pacotes. As consultas abaixo foram feitas por uma ferramenta de leitura de páginas, que devolve texto e não baixa arquivos binários. Portanto **nenhum arquivo zip foi baixado e aberto**. Isso afeta os critérios de aceite 1, 2 e 4, que continuam em aberto (ver o fim deste documento).

## Resultado por fonte

| Fonte | O que foi testado | Resultado | Status |
|---|---|---|---|
| B3, layout do COTAHIST | Leitura do PDF oficial `SeriesHistoricas_Layout.pdf` | Registro 01 com 245 bytes por linha; campos e posições conferidos um a um com o parser (`TPMERC` 010 = vista e 020 = fracionário; `CODBDI` 02 = lote padrão e 96 = fracionário) | Layout validado. Arquivo real não aberto |
| B3, download do COTAHIST | Leitura da página oficial de cotações históricas | A página informa: "é expressamente vedada a coleta automatizada, inclusive por meio de ferramentas de inteligência artificial". Os links dos arquivos não aparecem na página, só o acesso por um formulário | **Não automatizar** (regra R7). Ver D4 em `DECISIONS.md` |
| B3, Boletim de Negociação (BVBG.086) e Índices (BVBG.087) | Não testados | Mesma página de termos vale para a B3 | Não validado, não usado |
| Banco Central, SGS série 12 (CDI diário) | Consulta real a `.../bcdata.sgs.12/dados/ultimos/5?formato=json` | JSON válido: `[{"data":"29/10/2026","valor":"0.050788"}, ...]`. Data em `dd/mm/aaaa`, valor como texto com ponto decimal, unidade % ao dia. Equivale a cerca de 13,65% ao ano, coerente com a Selic de 13,75% | Validado (resposta real) |
| Banco Central, SGS séries 1, 432 e 433 | Não testadas uma a uma | Os códigos vêm do desenho original. A metadata (nome, periodicidade) não tem endpoint JSON próprio, então o projeto mantém uma tabela local de metadados | Não validado |
| CVM, IPE (Fato Relevante e Comunicado ao Mercado) | Listagem do diretório `.../CIA_ABERTA/DOC/IPE/DADOS/` | Há um zip por ano, de 2003 a 2026, cerca de 2 MB cada. `ipe_cia_aberta_2026.zip` consta como modificado em 01/11/2026 às 07:00. Não há arquivo de metadados na pasta | Existência e URL validadas. Conteúdo do CSV não aberto |
| CVM, mapeamento ticker para emissor | Não testado | O projeto casa o nome do emissor por palavras-chave definidas em `config/universe.yaml` | Não validado |
| brapi.dev | Leitura da documentação pública | Confirma que PETR4, MGLU3, VALE3 e ITUB4 respondem sem token. A página de documentação cita o endpoint `GET /api/v2/stocks/quote?symbols=` com cabeçalho `Authorization: Bearer`. Limites do plano (15 mil requisições por mês, histórico de 3 meses) **não foram confirmados**: vêm do desenho original | Parcial. Formato da resposta não validado |
| yfinance | Não testado | Biblioteca não oficial, não instalada aqui | Não implementado |

## Escolha da fonte de preços

1. **Coleta diária automática:** brapi, só para os tickers das carteiras e da watchlist (plano B do desenho original). Cabe com folga na cota.
2. **Histórico e conferência:** importação **manual** do COTAHIST. Quem baixa o arquivo, na página da B3, é uma pessoa; o programa só lê o arquivo colocado em `data/inbox/`. Isso respeita os termos da B3.
3. **Macro:** API SGS do Banco Central.
4. **Documentos de emissores:** dados abertos da CVM (licença ODbL).

## O que depende de você

- Baixar à mão os COTAHIST anuais (2025 e 2026) no site da B3 e colocar em `data/inbox/`, para dar histórico aos indicadores de 52 semanas. Sem isso, esses alertas aparecem como "sem dado".
- Criar um token na brapi e colocá-lo no `.env`, se quiser acompanhar mais que os quatro tickers de teste.

## Critérios de aceite do desenho original (seção 10)

| # | Critério | Situação |
|---|----------|----------|
| 1 | Baixar um dia completo de preços pela fonte em lote | **Aberto.** Parser feito e testado com arquivo sintético; arquivo real não aberto aqui e a coleta automática é vedada |
| 2 | Conferir 5 ativos contra uma segunda fonte | **Aberto.** Há comparador pronto (`carteiras.collector.crosscheck`); precisa de dados reais |
| 3 | CDI do SGS com metadata | **Parcial.** Resposta real obtida; metadata conferida só contra a tabela local |
| 4 | IPE da CVM com fato relevante real | **Aberto.** Parser feito e testado com CSV sintético; zip real não aberto |
| 5 | Rodada com 3 pregões pendentes, sem duplicar | **Atendido** em teste automatizado, com dados MOCK |
| 6 | Consumo de orçamento e "adiado por orçamento" no dashboard | **Atendido** com dados MOCK |
