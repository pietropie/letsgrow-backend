"""
Script para indexar a base de conhecimento (Brain/wiki) no PostgreSQL (pgvector).

Uso:
    python scripts/index_brain.py --wiki-path ../Brain/wiki --clear

Argumentos:
    --wiki-path   Caminho para a pasta wiki (default: ../Brain/wiki)
    --clear       Remove chunks existentes antes de reindexar (exceto user_experience)
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import AsyncSessionLocal
from app.rag.indexer import index_wiki
from app.services.rag import get_ai_context


async def main(wiki_path: Path, clear: bool) -> None:
    if not wiki_path.exists():
        print(f"[ERROR] Pasta não encontrada: {wiki_path}")
        sys.exit(1)

    print(f"Indexando wiki em: {wiki_path}")
    print(f"Limpar existentes: {clear}")

    async with AsyncSessionLocal() as db:
        # Lê o provider/modelo de embedding ativo da ai_config (editável no
        # painel admin /admin/ai-panel) — assim o script sempre usa o mesmo
        # "espaço vetorial" do retriever em runtime.
        config, _, embeddings = await get_ai_context(db)
        print(
            f"Provider de embedding: {config.embedding_provider} "
            f"({config.embedding_model}, {config.embedding_dimensions}d)"
        )
        total = await index_wiki(db, wiki_path, embeddings, clear_existing=clear, ai_config=config)

    print(f"\n✓ {total} chunks indexados com sucesso.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indexa a Brain wiki no pgvector")
    parser.add_argument(
        "--wiki-path",
        type=Path,
        default=Path(__file__).parent.parent.parent / "Brain" / "wiki",
        help="Caminho para Brain/wiki",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove chunks existentes antes de indexar",
    )
    args = parser.parse_args()
    asyncio.run(main(args.wiki_path, args.clear))
