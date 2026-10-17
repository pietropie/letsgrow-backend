"""
Envia a Brain wiki local para a API (produção ou outra) e reindexa lá:
base de conhecimento do Bobby (RAG/pgvector) e catálogo de strains.

Use quando a wiki não está no servidor nem o banco está acessível — o caso do
deploy no Coolify. Para indexar direto num banco local, use index_brain.py e
index_strains.py.

Uso (a partir de letsgrow-backend/, só biblioteca padrão):
    python scripts/push_brain.py --dry-run
    python scripts/push_brain.py
    python scripts/push_brain.py --api-url http://localhost:8000/api/v1 --skip-strains

O ADMIN_TOKEN é lido de LETSGROW_ADMIN_TOKEN ou pedido num prompt oculto —
nunca passe o token como argumento (fica no histórico do shell).

Passos: envia os .md em lotes para POST /admin/knowledge/reindex (cada arquivo
substitui os próprios chunks), depois POST /admin/knowledge/prune com a lista
completa (apaga chunks de arquivos removidos) e POST /admin/strains/reindex.
Lotes que falham por cota/rede são repetidos com espera crescente; se algum
lote falhar de vez, o prune não roda (nada é apagado).
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_URL = "https://api.letsgrowapp.com.br/api/v1"
DEFAULT_WIKI = Path(__file__).parent.parent.parent / "Brain" / "wiki"
RETRY_STATUS = {429, 500, 502, 503, 504}


def _post(api_url: str, token: str, path: str, payload: dict, attempts: int = 6) -> dict:
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            api_url.rstrip("/") + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": token,
                "User-Agent": "letsgrow-push-brain/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403):
                sys.exit(f"[ERRO] {exc.code}: token de admin recusado.")
            if exc.code not in RETRY_STATUS or attempt == attempts:
                raise RuntimeError(f"{path} → HTTP {exc.code}: {detail}") from exc
            wait = 15 * attempt
            print(f"  HTTP {exc.code} ({detail[:160]}) — nova tentativa em {wait}s")
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"{path} → {exc}") from exc
            wait = 15 * attempt
            print(f"  {exc} — nova tentativa em {wait}s")
        time.sleep(wait)
    raise AssertionError("unreachable")


def _load(wiki: Path) -> list[dict]:
    return [
        {"path": p.relative_to(wiki).as_posix(), "content": p.read_text(encoding="utf-8")}
        for p in sorted(wiki.rglob("*.md"))
    ]


def main() -> None:
    sys.stdout.reconfigure(errors="replace")  # console cp1252 no Windows
    parser = argparse.ArgumentParser(description="Envia Brain/wiki para a API e reindexa")
    parser.add_argument("--wiki-path", type=Path, default=DEFAULT_WIKI)
    parser.add_argument("--api-url", default=os.environ.get("LETSGROW_API_URL", DEFAULT_API_URL))
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--skip-knowledge", action="store_true")
    parser.add_argument("--skip-strains", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Só lista o que seria enviado")
    args = parser.parse_args()

    if not args.wiki_path.is_dir():
        sys.exit(f"[ERRO] Pasta não encontrada: {args.wiki_path}")

    files = _load(args.wiki_path)
    # O servidor decide o que entra no RAG (app/rag/indexer.py:should_index).
    knowledge = files
    strains = [f for f in files if f["path"].startswith("strains/")]
    print(f"Wiki: {args.wiki_path} — {len(files)} .md, {len(strains)} em strains/")
    print(f"API:  {args.api_url}")

    if args.dry_run:
        for f in files:
            print("  ", f["path"])
        return

    token = os.environ.get("LETSGROW_ADMIN_TOKEN") or getpass.getpass("ADMIN_TOKEN (não aparece ao digitar): ")
    # Colar no prompt oculto pode trazer os marcadores de "bracketed paste"
    # (ESC[200~ … ESC[201~) e outros caracteres de controle junto do token.
    token = re.sub(r"\x1b\[20[01]~|[\x00-\x1f\x7f]", "", token).strip()
    if not token:
        sys.exit("[ERRO] Token vazio.")
    print(f"Token recebido: {len(token)} caracteres.")

    if not args.skip_knowledge:
        total_chunks = 0
        failed: list[str] = []
        batches = [knowledge[i:i + args.batch_size] for i in range(0, len(knowledge), args.batch_size)]
        for n, batch in enumerate(batches, 1):
            try:
                result = _post(args.api_url, token, "/admin/knowledge/reindex", {"files": batch})
            except RuntimeError as exc:
                print(f"[{n}/{len(batches)}] FALHOU: {exc}")
                failed += [f["path"] for f in batch]
                continue
            total_chunks += result["chunks_indexed"]
            print(
                f"[{n}/{len(batches)}] {result['files_indexed']} arquivos → {result['chunks_indexed']} chunks"
                + (f" (ignorados: {', '.join(result['skipped'])})" if result["skipped"] else "")
            )

        print(f"\nRAG: {total_chunks} chunks indexados.")
        if failed:
            print(f"[AVISO] {len(failed)} arquivos falharam; prune não executado. Rode de novo.")
        else:
            result = _post(args.api_url, token, "/admin/knowledge/prune", {"keep_paths": [f["path"] for f in knowledge]})
            print(f"Prune: {result['chunks_deleted']} chunks de arquivos removidos/ignorados apagados.")

    if not args.skip_strains and strains:
        result = _post(args.api_url, token, "/admin/strains/reindex", {"files": strains})
        print(f"Strains: {result['created']} criadas, {result['updated']} atualizadas.")


if __name__ == "__main__":
    main()
