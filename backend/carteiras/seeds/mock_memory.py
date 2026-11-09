"""Lições, calibração e categorias de erro de demonstração. TUDO rotulado `is_mock = 1`: são telas com dado de mentira
enquanto o agente real e a reflexão não existem (seção 10 do desenho). Nunca derivam de resultados reais.
"""

from __future__ import annotations

from ..config import Config
from ..db import Database, dumps, now_iso

LICOES = [
    ("[MOCK] Rompimento sem volume costuma falhar", "Exemplo de lição: rompimentos de máxima com volume relativo abaixo de 1,5 tiveram retorno pior que os com volume alto.", "entrada"),
    ("[MOCK] Reduzir antes do stop preserva capital em gaps", "Exemplo de lição: em quedas críticas, a saída parcial limitou a perda frente a esperar o stop.", "saida"),
    ("[MOCK] Posições acima do limite voltam rápido", "Exemplo de lição: estourar o limite por valorização foi corrigido em poucos pregões com redução parcial.", "dimensionamento"),
]
FAIXAS = [("0.4-0.5", 12, 5), ("0.5-0.6", 20, 11), ("0.6-0.7", 14, 9), ("0.7-0.8", 6, 4)]
ERROS = [("entrada precoce", 7), ("stop curto demais", 5), ("tamanho acima do ideal", 3), ("ignorou fato relevante", 2)]


def seed_mock_memory(db: Database, cfg: Config) -> None:
    """Idempotente: só preenche se ainda não houver linhas MOCK do perfil."""
    with db.transaction():
        for pid in cfg.profile_ids():
            if not db.scalar("SELECT 1 FROM lessons WHERE profile_id = ? AND is_mock = 1", (pid,)):
                ids = [r["id"] for r in db.query("SELECT id FROM decisions WHERE profile_id = ? AND is_mock = 1 ORDER BY id LIMIT 3", (pid,))]
                for titulo, desc, cat in LICOES:
                    db.insert("lessons", {"profile_id": pid, "titulo": titulo, "descricao": desc, "categoria": cat,
                                          "decision_ids_json": dumps(ids), "is_mock": 1, "created_at": now_iso()})
            for faixa, n, acertos in FAIXAS:
                db.insert("calibration", {"profile_id": pid, "faixa_confianca": faixa, "n": n, "acertos": acertos, "is_mock": 1}, or_ignore=True)
            for cat, n in ERROS:
                db.insert("error_categories", {"profile_id": pid, "categoria": cat, "n": n, "is_mock": 1}, or_ignore=True)
