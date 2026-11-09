"""Caminhos, modo de execução, leitura de segredos e filtro de segredos nos logs (regra R1)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
MODES = ("mock", "real")

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)", re.IGNORECASE)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret|passwd)\s*[=:]\s*)[^\s&\"',;]+"),
]
MASK = "***"


class MissingSecretError(RuntimeError):
    """Chave obrigatória ausente. Não existe valor padrão para segredos."""


def project_root() -> Path:
    env = os.environ.get("CARTEIRAS_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    env = os.environ.get("CARTEIRAS_CONFIG_DIR")
    return Path(env) if env else project_root() / "config"


def data_dir() -> Path:
    env = os.environ.get("CARTEIRAS_DATA_DIR")
    path = Path(env) if env else project_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_mode() -> str:
    mode = os.environ.get("CARTEIRAS_MODE", "mock").strip().lower()
    if mode not in MODES:
        raise ValueError(f"CARTEIRAS_MODE inválido: {mode!r}. Use um de {MODES}.")
    return mode


def mock_agent_executes_orders(mode: str | None = None) -> bool:
    """Em modo mock, as ordens do MockAgent são simuladas. Em modo real, só com CARTEIRAS_MOCK_AGENT_ORDERS=1,
    para que decisões de mentira não contaminem a simulação sobre dados reais (docs/DECISIONS.md, D15)."""
    if (mode or current_mode()) == "mock":
        return True
    return os.environ.get("CARTEIRAS_MOCK_AGENT_ORDERS", "0").strip() == "1"


def db_path(mode: str | None = None) -> Path:
    env = os.environ.get("CARTEIRAS_DB")
    if env:
        return Path(env)
    return data_dir() / f"{mode or current_mode()}.db"


def load_dotenv(path: Path | None = None) -> None:
    """Lê o .env (se existir) para o ambiente, sem sobrescrever variáveis já definidas e sem logar valores."""
    path = path or project_root() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    """Devolve a variável de ambiente ou falha com mensagem clara. Nunca usa valor padrão."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSecretError(
            f"A variável {name} é obrigatória e não está definida. Coloque-a no arquivo .env (veja .env.example)."
        )
    return value


def optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v.strip()) >= 6]


def mask_secrets(text: str) -> str:
    """Mascara valores de variáveis secretas e padrões comuns de chave em qualquer texto."""
    out = text
    for secret in _secret_values():
        out = out.replace(secret, MASK)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + MASK, out)
    return out


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - formatação quebrada não pode derrubar o log
            message = str(record.msg)
        record.msg = mask_secrets(message)
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(f, SecretFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(SecretFilter())
        root.addHandler(handler)
