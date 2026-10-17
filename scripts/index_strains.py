"""
Script para indexar Brain/wiki/strains/*.md na tabela `strains` (catálogo
estruturado usado para autocomplete e para o card de informações da strain).

Faz parse do frontmatter YAML (formato simples chave: valor / listas [a, b, c])
e da seção "## Resumo" de cada arquivo, e faz UPSERT idempotente por `slug`
(slug = nome do arquivo .md sem extensão).

Uso (a partir de letsgrow-backend/):
    python scripts/index_strains.py
    python scripts/index_strains.py --wiki-path ../Brain/wiki/strains

Não requer dependências externas (não usa PyYAML — frontmatter é parseado com
regex simples, suficiente para o formato usado na wiki).

NOTA: este script não é executado automaticamente em deploy — rode manualmente
sempre que novos arquivos forem adicionados/editados em Brain/wiki/strains/.
Em produção (Coolify), use scripts/push_brain.py, que envia os .md para
POST /admin/strains/reindex (a wiki não vai na imagem).
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import AsyncSessionLocal
from app.services.strain_catalog import upsert_strains


async def index_strains(strains_path: Path) -> tuple[int, int]:
    """Lê os .md de `strains_path`, faz upsert por slug. Retorna (criados, atualizados)."""
    documents = []
    for md_path in sorted(strains_path.glob("*.md")):
        try:
            source_file = str(md_path.relative_to(strains_path.parent))
        except ValueError:
            source_file = str(md_path)
        documents.append((md_path.stem, md_path.read_text(encoding="utf-8"), source_file))

    async with AsyncSessionLocal() as db:
        return await upsert_strains(db, documents)


async def main(strains_path: Path) -> None:
    if not strains_path.exists():
        print(f"[ERROR] Pasta não encontrada: {strains_path}")
        sys.exit(1)

    print(f"Indexando strains em: {strains_path}")
    created, updated = await index_strains(strains_path)
    print(f"\n✓ {created} strains criadas, {updated} atualizadas (upsert idempotente por slug).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indexa Brain/wiki/strains na tabela `strains`")
    parser.add_argument(
        "--wiki-path",
        type=Path,
        default=Path(__file__).parent.parent.parent / "Brain" / "wiki" / "strains",
        help="Caminho para Brain/wiki/strains",
    )
    args = parser.parse_args()
    asyncio.run(main(args.wiki_path))
