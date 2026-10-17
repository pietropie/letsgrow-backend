"""Reindexação da Brain wiki pelo admin (POST /admin/knowledge/*, /admin/strains/reindex).

Rotas reais em SQLite em memória; o provedor de embeddings é simulado.
"""
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import test_private_access as support  # registra JSONB -> JSON no SQLite
from app.api.v1 import admin_knowledge
from app.config import settings
from app.database import get_db
from app.models.base import Base
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.strain import Strain
from app.models.user import User

CONFIG = SimpleNamespace(
    embedding_provider="gemini", embedding_model="models/gemini-embedding-001", embedding_dimensions=768,
)

DOC = """---
title: LST
tags: [tecnica, vegetativo]
---

## Resumo

Low Stress Training dobra os galhos para abrir a copa e uniformizar a altura dos topos sob a luz.
Funciona melhor no vegetativo, antes do flip, e não estressa a planta como o topping.

## Como fazer

Amarre os galhos principais com arame macio, puxando para fora aos poucos ao longo de vários dias,
sem quebrar o caule. Ajuste a cada dois ou três dias conforme a planta cresce em direção à luz.
"""

STRAIN = """---
title: Gelato #41 — Cookie Fam Genetics
type: strain
aliases: [gelato 41, G41]
dominancia: hibrida
breeder: Cookie Fam
floração_dias: 63-70
terpeno_dominante: limoneno
---

## Resumo

Híbrida doce e cremosa, de floração média.
"""


class FakeEmbeddings:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def aembed_documents(self, texts, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429 Resource exhausted")
        return [[0.1] * 768 for _ in texts]


class AdminKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine, tables=[
            User.__table__, AIConversation.__table__, KnowledgeChunk.__table__, Strain.__table__,
        ])
        self.session = Session(self.engine, expire_on_commit=False)
        self.embeddings = FakeEmbeddings()

        async def fake_context(_db):
            return CONFIG, None, self.embeddings

        self.enterContext(patch.object(admin_knowledge, "get_ai_context", fake_context))
        self.enterContext(patch.object(settings, "ADMIN_TOKEN", "token-de-teste"))
        app = FastAPI()
        app.include_router(admin_knowledge.router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: support.AsyncSessionAdapter(self.session)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.session.close()
        self.engine.dispose()

    def post(self, path, payload, token="token-de-teste"):
        return self.client.post(path, json=payload, headers={"X-Admin-Token": token})

    def chunks(self):
        return self.session.execute(select(KnowledgeChunk)).scalars().all()

    def test_requires_admin_token(self):
        response = self.post("/admin/knowledge/reindex", {"files": [{"path": "tecnicas/lst.md", "content": DOC}]}, token="x")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.chunks(), [])

    def test_reindex_is_idempotent_and_skips_private_files(self):
        files = [
            {"path": "tecnicas/lst.md", "content": DOC},
            {"path": "templates/strain.md", "content": DOC},
            {"path": "cultivations/pietro-ciclo-1-2026.md", "content": DOC},
        ]
        first = self.post("/admin/knowledge/reindex", {"files": files})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["files_indexed"], 1)
        self.assertEqual(set(body["skipped"]), {"templates/strain.md", "cultivations/pietro-ciclo-1-2026.md"})
        self.assertGreater(body["chunks_indexed"], 0)

        again = self.post("/admin/knowledge/reindex", {"files": files[:1]})
        self.assertEqual(again.json()["chunks_indexed"], body["chunks_indexed"])
        chunks = self.chunks()
        self.assertEqual(len(chunks), body["chunks_indexed"])
        self.assertEqual({c.source_file for c in chunks}, {"tecnicas/lst.md"})
        self.assertEqual(chunks[0].source_type, "technique")
        self.assertIn("veg", chunks[0].phase_relevance)

    def test_prune_keeps_user_and_conversation_chunks(self):
        user = User(id=uuid.uuid4(), email="a@example.test", username="a")
        self.session.add(user)
        self.session.flush()
        conversation = AIConversation(id=uuid.uuid4(), user_id=user.id, messages=[])
        self.session.add(conversation)
        self.session.add_all([
            KnowledgeChunk(content="experiência", source_file="user", source_type="user_experience"),
            KnowledgeChunk(content="extraído", source_file="conversa", source_type="technique",
                           source_conversation_id=conversation.id),
        ])
        self.session.commit()
        self.post("/admin/knowledge/reindex", {"files": [
            {"path": "tecnicas/lst.md", "content": DOC},
            {"path": "tecnicas/antigo.md", "content": DOC},
        ]})

        response = self.post("/admin/knowledge/prune", {"keep_paths": ["tecnicas/lst.md", "templates/x.md"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["chunks_deleted"], 0)
        self.assertEqual(
            {c.source_file for c in self.chunks()}, {"tecnicas/lst.md", "user", "conversa"},
        )

    def test_rejects_unsafe_paths(self):
        for path in ["../segredo.md", "/etc/x.md", "a\\b.md", "c:/x.md", "tecnicas/./x.md", "x.txt"]:
            response = self.post("/admin/knowledge/reindex", {"files": [{"path": path, "content": DOC}]})
            self.assertEqual(response.status_code, 422, path)

    def test_embedding_failure_returns_502_and_keeps_old_chunks(self):
        self.post("/admin/knowledge/reindex", {"files": [{"path": "tecnicas/lst.md", "content": DOC}]})
        before = len(self.chunks())
        self.embeddings.fail = True
        response = self.post("/admin/knowledge/reindex", {"files": [{"path": "tecnicas/lst.md", "content": DOC}]})
        self.assertEqual(response.status_code, 502)
        self.assertIn("RuntimeError", response.json()["detail"])
        self.assertEqual(len(self.chunks()), before)

    def test_strains_upsert_by_slug(self):
        files = [
            {"path": "strains/gelato-41.md", "content": STRAIN},
            {"path": "tecnicas/lst.md", "content": DOC},
        ]
        first = self.post("/admin/strains/reindex", {"files": files})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), {"created": 1, "updated": 0, "ignored": ["tecnicas/lst.md"]})
        second = self.post("/admin/strains/reindex", {"files": files[:1]})
        self.assertEqual(second.json()["updated"], 1)

        strain = self.session.execute(select(Strain)).scalar_one()
        self.assertEqual((strain.slug, strain.name, strain.genetics, strain.flowering_days),
                         ("gelato-41", "Gelato #41", "hybrid", 63))
        self.assertEqual(strain.source_file, "strains/gelato-41.md")


if __name__ == "__main__":
    unittest.main()
