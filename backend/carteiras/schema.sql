-- Esquema do banco (SQLite). Evita recursos exclusivos do SQLite sempre que possível, para migrar a Postgres.
-- Todo dado de demonstração tem is_mock = 1. Datas de pregão são texto AAAA-MM-DD.

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Cópia versionada dos perfis usados, para auditar mudanças de parâmetros.
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  version_hash TEXT NOT NULL,
  status_parametros TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (profile_id, version_hash)
);

CREATE TABLE IF NOT EXISTS profile_state (
  profile_id TEXT PRIMARY KEY,
  pausado INTEGER NOT NULL DEFAULT 0,
  iniciado_em TEXT,
  ultimo_pregao TEXT,
  somente_reduzir INTEGER NOT NULL DEFAULT 0,
  pico_patrimonio REAL,
  capital_inicial REAL,
  atualizado_em TEXT
);

CREATE TABLE IF NOT EXISTS assets (
  ticker TEXT PRIMARY KEY,
  nome TEXT,
  emissor TEXT,
  setor TEXT,
  tipo TEXT NOT NULL DEFAULT 'acao',
  lote INTEGER NOT NULL DEFAULT 100,
  ativo INTEGER NOT NULL DEFAULT 1,
  is_mock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prices_raw (
  ticker TEXT NOT NULL,
  pregao TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume_fin REAL,
  quantidade REAL,
  negocios INTEGER,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  collected_at TEXT NOT NULL,
  PRIMARY KEY (ticker, pregao)
);
CREATE INDEX IF NOT EXISTS idx_prices_raw_pregao ON prices_raw (pregao);

CREATE TABLE IF NOT EXISTS prices_adj (
  ticker TEXT NOT NULL,
  pregao TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume_fin REAL,
  fator REAL NOT NULL DEFAULT 1,
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ticker, pregao)
);

CREATE TABLE IF NOT EXISTS corporate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  tipo TEXT NOT NULL,              -- DIVIDENDO | JCP | DESDOBRAMENTO | GRUPAMENTO | BONIFICACAO
  data_com TEXT,
  data_ex TEXT NOT NULL,
  data_pagamento TEXT,
  valor REAL NOT NULL DEFAULT 0,   -- provento por ação (BRL)
  fator REAL NOT NULL DEFAULT 1,   -- multiplicador de quantidade (desdobramento 1:2 = 2; grupamento 10:1 = 0.1; bonificação de 10% = 1.1)
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  UNIQUE (ticker, tipo, data_ex, valor, fator)
);

CREATE TABLE IF NOT EXISTS macro_series (
  codigo INTEGER NOT NULL,
  nome TEXT,
  data TEXT NOT NULL,
  valor REAL NOT NULL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (codigo, data)
);

CREATE TABLE IF NOT EXISTS filings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  protocolo TEXT NOT NULL UNIQUE,
  cnpj TEXT,
  nome_emissor TEXT,
  categoria TEXT,
  assunto TEXT,
  data_referencia TEXT,
  data_entrega TEXT,
  link TEXT,
  ticker TEXT,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings (ticker, data_entrega);

CREATE TABLE IF NOT EXISTS data_sources (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,            -- ok | atrasada | falhou | plano_b | sem_acesso | nao_configurada | mock
  detalhe TEXT,
  dados_ate TEXT,
  ultima_tentativa TEXT,
  ultimo_ok TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,            -- '*' para alertas de mercado
  tipo TEXT NOT NULL,
  severidade TEXT NOT NULL,        -- INFO | ATENCAO | CRITICO
  pregao TEXT NOT NULL,
  medido REAL,
  limiar REAL,
  detalhe_json TEXT,
  refs_json TEXT,
  cooldown INTEGER NOT NULL DEFAULT 0,   -- 1 = suprimido pelo cooldown (não escala)
  em_carteira INTEGER NOT NULL DEFAULT 0,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (profile_id, pregao, ticker, tipo)
);
CREATE INDEX IF NOT EXISTS idx_alerts_profile ON alerts (profile_id, pregao);

CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id INTEGER NOT NULL UNIQUE,
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  pregao TEXT NOT NULL,
  status TEXT NOT NULL,            -- EXECUTADO | ADIADO_ORCAMENTO | NAO_ELEGIVEL
  motivo TEXT,
  consumo_json TEXT,
  dossier_id INTEGER,
  decision_id INTEGER,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dossiers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  ticker TEXT,
  pregao TEXT NOT NULL,
  tipo TEXT NOT NULL,              -- ALERTA | REVISAO | INICIAL
  conteudo_json TEXT NOT NULL,
  refs_json TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  pregao TEXT NOT NULL,
  ticker TEXT,
  tipo TEXT NOT NULL,              -- ALERTA | REVISAO | INICIAL | MANUAL | GATILHO
  origem TEXT NOT NULL,            -- manual | mock | agente
  agente TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  registro_json TEXT NOT NULL,
  dossier_id INTEGER,
  parecer_regras_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_profile ON decisions (profile_id, pregao);

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  decision_id INTEGER,
  descricao TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ATIVO',
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER,
  profile_id TEXT NOT NULL,
  ticker TEXT,
  descricao TEXT,
  tipo TEXT NOT NULL,
  condicao_json TEXT NOT NULL,
  invalida_json TEXT,
  acao_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDENTE',   -- PENDENTE | DISPARADO | INVALIDADO
  criado_pregao TEXT NOT NULL DEFAULT '',    -- só é avaliado em pregões posteriores a este (sem look-ahead)
  ultima_avaliacao TEXT,
  ultima_avaliacao_json TEXT,
  disparado_em TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  lado TEXT NOT NULL,              -- COMPRA | VENDA
  tipo_ordem TEXT NOT NULL DEFAULT 'NORMAL',   -- NORMAL | CONSTRUCAO_INICIAL | STOP | ALVO | GATILHO
  dimensionamento TEXT NOT NULL,   -- quantidade | peso_alvo_pct | fracao_posicao
  quantidade INTEGER,
  peso_alvo_pct REAL,
  fracao_posicao REAL,
  stop_pct REAL,
  alvo_pct REAL,
  pregao_decisao TEXT NOT NULL,
  pregao_exec TEXT NOT NULL,
  status TEXT NOT NULL,            -- AGENDADA | EXECUTADA | REDUZIDA | VETADA | CANCELADA
  origem TEXT NOT NULL,            -- manual | mock | agente | sistema
  motivo TEXT,
  decision_id INTEGER,
  idem_key TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (profile_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_orders_exec ON orders (profile_id, pregao_exec, status);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  lado TEXT NOT NULL,
  quantidade INTEGER NOT NULL,
  preco REAL NOT NULL,
  valor REAL NOT NULL,
  custos REAL NOT NULL,
  slippage_brl REAL NOT NULL,
  pregao TEXT NOT NULL,
  tipo TEXT,
  detalhe_json TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cash_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  pregao TEXT NOT NULL,
  tipo TEXT NOT NULL,              -- APORTE | COMPRA | VENDA | CUSTO | PROVENTO | AJUSTE
  valor REAL NOT NULL,             -- positivo entra, negativo sai
  ref_json TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cash_profile ON cash_ledger (profile_id, pregao);

CREATE TABLE IF NOT EXISTS positions (
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  quantidade INTEGER NOT NULL,
  custo_medio REAL NOT NULL,       -- inclui os custos de compra
  stop REAL,
  alvo REAL,
  aberta_em TEXT NOT NULL,
  atualizado_em TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, ticker)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
  profile_id TEXT NOT NULL,
  pregao TEXT NOT NULL,
  ticker TEXT NOT NULL,
  quantidade INTEGER NOT NULL,
  preco_fech REAL,
  custo_medio REAL,
  PRIMARY KEY (profile_id, pregao, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  entrada TEXT,
  saida TEXT NOT NULL,
  quantidade INTEGER NOT NULL,
  preco_entrada REAL NOT NULL,
  preco_saida REAL NOT NULL,
  pnl REAL NOT NULL,
  pnl_pct REAL NOT NULL,
  motivo_saida TEXT,
  fill_id INTEGER UNIQUE,
  is_mock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dividend_accruals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  quantidade INTEGER NOT NULL,
  valor_total REAL NOT NULL,
  pregao_credito TEXT NOT NULL,
  creditado INTEGER NOT NULL DEFAULT 0,
  UNIQUE (profile_id, event_id)
);

CREATE TABLE IF NOT EXISTS event_applications (
  profile_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  pregao TEXT NOT NULL,
  PRIMARY KEY (profile_id, event_id)
);

CREATE TABLE IF NOT EXISTS portfolio_history (
  profile_id TEXT NOT NULL,
  pregao TEXT NOT NULL,
  patrimonio REAL NOT NULL,
  caixa REAL NOT NULL,
  posicoes_valor REAL NOT NULL,
  retorno_dia REAL,
  pico REAL NOT NULL,
  drawdown REAL NOT NULL,
  defasados INTEGER NOT NULL DEFAULT 0,   -- posições valorizadas com preço de dia anterior (sem negócio no pregão)
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, pregao)
);

CREATE TABLE IF NOT EXISTS rule_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  order_id INTEGER,
  regra TEXT NOT NULL,
  resultado TEXT NOT NULL,         -- VETADA | REDUZIDA | AVISO | MODO
  motivo TEXT NOT NULL,
  detalhe_json TEXT,
  pregao TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- Encaixes de memória e aprendizado (preenchidos com dados MOCK rotulados nesta etapa).
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  titulo TEXT NOT NULL,
  descricao TEXT NOT NULL,
  categoria TEXT,
  decision_ids_json TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration (
  profile_id TEXT NOT NULL,
  faixa_confianca TEXT NOT NULL,
  n INTEGER NOT NULL,
  acertos INTEGER NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, faixa_confianca)
);

CREATE TABLE IF NOT EXISTS error_categories (
  profile_id TEXT NOT NULL,
  categoria TEXT NOT NULL,
  n INTEGER NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, categoria)
);

CREATE TABLE IF NOT EXISTS budget_usage (
  dia TEXT NOT NULL,
  profile_id TEXT NOT NULL,        -- '*' para recursos globais (cota da brapi)
  recurso TEXT NOT NULL,           -- brapi_req | escalonamento | ia_chamadas | ia_tokens
  usado REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (dia, profile_id, recurso)
);

CREATE TABLE IF NOT EXISTS checkpoints (
  profile_id TEXT NOT NULL,        -- '*' para etapas globais
  pregao TEXT NOT NULL,
  etapa TEXT NOT NULL,
  status TEXT NOT NULL,
  detalhe TEXT,
  ts TEXT NOT NULL,
  PRIMARY KEY (profile_id, pregao, etapa)
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  iniciada_em TEXT NOT NULL,
  finalizada_em TEXT,
  status TEXT NOT NULL,
  pregoes_json TEXT,
  detalhe TEXT
);

-- Log de eventos append-only (regra R5). Nada é atualizado nem apagado.
CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  profile_id TEXT,
  origem TEXT NOT NULL CHECK (origem IN ('manual', 'mock', 'agente', 'sistema')),
  tipo TEXT NOT NULL,
  pregao TEXT,
  motivo TEXT,
  ref_json TEXT,
  payload_json TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS event_log_no_update BEFORE UPDATE ON event_log
BEGIN SELECT RAISE(ABORT, 'event_log é append-only'); END;

CREATE TRIGGER IF NOT EXISTS event_log_no_delete BEFORE DELETE ON event_log
BEGIN SELECT RAISE(ABORT, 'event_log é append-only'); END;

CREATE TABLE IF NOT EXISTS system_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  nivel TEXT NOT NULL,
  modulo TEXT NOT NULL,
  mensagem TEXT NOT NULL,
  extra_json TEXT
);
