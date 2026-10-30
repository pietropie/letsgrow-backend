"""Política da Google Play no Bobby (analise-2026-10-16/PLAYSTORE.md §2).

- Base de conhecimento: sem lojas/preço, extração, efeitos de consumo e promessas terapêuticas,
  na indexação, na busca e no catálogo de strains (app/rag/content_policy.py).
- Prompt: limites de tema no SYSTEM_BASE e no briefing diário.
- Denúncia "offensive" no feedback por resposta e confirmação de 18+ no cadastro.
"""
import os
import pathlib
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_admin_knowledge as knowledge_support
import test_bobby_chat as chat_support
import test_sem_api as sem
from app.api.v1 import auth, users
from app.config import settings
from app.database import get_db
from app.models.bob import BobFeedback
from app.models.knowledge import KnowledgeChunk
from app.models.strain import Strain
from app.models.user import User
from app.rag import content_policy as cp
from app.rag.indexer import _strip_frontmatter, should_index
from app.rag.prompts import SYSTEM_BASE, TOPIC_LIMITS, build_system_prompt
from app.services.auth import get_current_user
from app.services.daily_brief import _BRIEF_SYSTEM

WIKI = pathlib.Path(__file__).resolve().parents[2] / "Brain" / "wiki"

STRAIN_DOC = """---
title: AK-47
type: strain
tags: [cannabis, strain, netseeds, concentrado, sativa]
breeder: Serious Seeds
---

## Resumo

AK-47 é um híbrido da Serious Seeds. Disponível na NetSeeds por R$ 120. Floração de 53–63 dias.

## Ficha Técnica

| Parâmetro | Valor |
|---|---|
| **Floração** | 53–63 dias |

## Efeitos e Usos

**Efeitos reportados:** relaxante e sociável.

**Usos medicinais relatados:** ansiedade, depressão, dor leve.

## Dados de Cultivo

- **Indoor:** Planta de tamanho médio. Buds compactos, excelente para rosin.
- **Outdoor:** Colheita em outubro.

## Disponível no Brasil

| Loja | Preço |
|---|---|
| Flora Urbana | R$ 90 |

## Perfil de Terpenos

| Terpeno | Efeito |
|---|---|
| Mirceno | Relaxante, potencializa THC |
"""

NUTRITION_DOC = """## Efeito do pH na Disponibilidade de Nutrientes

Se o fósforo disponível no solo passar de 80 PPM, a micorriza não se forma.
- **Indica:** tolera doses altas; maior EC sem problemas.
- Não colher cedo por ansiedade: espere os tricomas.

## Colheita

| Tricomas | Ação |
|---|---|
| Leitosos | ✅ Colher (efeito mais cerebral) |
| Âmbar (>50%) | THC → CBN, sedativo |
"""


class SanitizeTests(unittest.TestCase):
    def test_strain_keeps_cultivation_and_drops_sale_effects_and_extraction(self):
        clean = cp.sanitize_markdown(_strip_frontmatter(STRAIN_DOC))
        self.assertIn("Floração de 53–63 dias.", clean)
        self.assertIn("| **Floração** | 53–63 dias |", clean)
        self.assertIn("- **Indoor:** Planta de tamanho médio.", clean)
        self.assertIn("Colheita em outubro", clean)
        for gone in ("NetSeeds", "R$", "Efeitos e Usos", "ansiedade", "rosin", "Flora Urbana", "Disponível no Brasil",
                     "Mirceno", "Perfil de Terpenos\n\n|"):
            self.assertNotIn(gone, clean)
        self.assertFalse(cp.is_blocked_text(clean))
        self.assertEqual(cp.sanitize_markdown(clean), clean)  # idempotente

    def test_cultivation_words_that_look_alike_stay(self):
        clean = cp.sanitize_markdown(NUTRITION_DOC)
        self.assertIn("## Efeito do pH na Disponibilidade de Nutrientes", clean)
        self.assertIn("fósforo disponível no solo", clean)
        self.assertIn("tolera doses altas", clean)
        self.assertIn("por ansiedade", clean)
        # o qualificador de efeito sai e a informação de colheita fica
        self.assertIn("| Leitosos | ✅ Colher |", clean)
        self.assertIn("| Âmbar (>50%) | THC → CBN |", clean)

    def test_summary_text_and_tags(self):
        self.assertEqual(cp.sanitize_text("AK-47 é um híbrido. Disponível na NetSeeds. Floração curta."),
                         "AK-47 é um híbrido. Floração curta.")
        self.assertIsNone(cp.sanitize_text("Disponível na NetSeeds."))
        self.assertEqual([t for t in ["cannabis", "netseeds", "concentrado", "sativa", "medicinal"] if not cp.is_blocked_tag(t)],
                         ["cannabis", "sativa"])

    def test_blocked_paths_never_index(self):
        for path in ("markets/lojas-sementes-brasil.md", "extractions/rosin.md", "sources/netseeds-2026-04.md",
                     "sources/flora-urbana-2026-04.md", "sources/seeds-genetics-brazil-2026-04.md",
                     "concepts/mapa-dores-grower.md", "concepts/personas-usuario.md", "concepts/seguranca-juridica-hc.md"):
            self.assertFalse(should_index(path), path)
        self.assertTrue(should_index("strains/ak-47.md"))
        self.assertTrue(should_index("sources/mr-canucks-grow-playlist-2026-04.md"))

    @unittest.skipUnless(WIKI.is_dir(), "Brain/wiki não está nesta máquina (CI)")
    def test_whole_wiki_is_clean_after_the_filter(self):
        checked = 0
        for md in sorted(WIKI.rglob("*.md")):
            rel = md.relative_to(WIKI).as_posix()
            if not should_index(rel):
                continue
            clean = cp.sanitize_markdown(_strip_frontmatter(md.read_text(encoding="utf-8")))
            self.assertFalse(cp.is_blocked_text(clean), rel)
            for term in ("NetSeeds", "Flora Urbana", "Seeds Genetics", "rosin", "Bubble Hash", "Efeitos e Usos",
                         "## Disponível no Brasil", "Usos medicinais"):
                self.assertNotIn(term.lower(), clean.lower(), f"{rel}: {term}")
            checked += 1
        self.assertGreater(checked, 150)


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = knowledge_support.AdminKnowledgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def chunks(self):
        self.fixture.session.expire_all()
        return self.fixture.chunks()

    def test_reindex_sanitizes_and_skips_blocked_files(self):
        files = [
            {"path": "strains/ak-47.md", "content": STRAIN_DOC},
            {"path": "markets/lojas-sementes-brasil.md", "content": STRAIN_DOC},
            {"path": "extractions/rosin.md", "content": STRAIN_DOC},
        ]
        body = self.fixture.post("/admin/knowledge/reindex", {"files": files}).json()
        self.assertEqual(set(body["skipped"]), {"markets/lojas-sementes-brasil.md", "extractions/rosin.md"})
        chunks = self.chunks()
        self.assertEqual({c.source_file for c in chunks}, {"strains/ak-47.md"})
        text = "\n".join(c.content for c in chunks)
        self.assertIn("Floração de 53–63 dias", text)
        self.assertFalse(cp.is_blocked_text(text))
        self.assertTrue(all("netseeds" not in c.tags and "concentrado" not in c.tags for c in chunks))

    def test_prune_and_reindex_remove_what_was_indexed_before_the_policy(self):
        session = self.fixture.session
        session.add_all([
            KnowledgeChunk(id=uuid.uuid4(), content="Loja X vende sementes por R$ 90", embedding=[0.1] * 768,
                           source_file="markets/lojas-sementes-brasil.md", source_type="concept", tags=[]),
            KnowledgeChunk(id=uuid.uuid4(), content="Bubble hash: sacos de 25 a 220 µm", embedding=[0.1] * 768,
                           source_file="extractions/bubble-hash.md", source_type="concept", tags=[]),
            KnowledgeChunk(id=uuid.uuid4(), content="Disponível na NetSeeds.", embedding=[0.1] * 768,
                           source_file="strains/so-loja.md", source_type="strain", tags=[]),
        ])
        session.commit()
        keep = ["markets/lojas-sementes-brasil.md", "extractions/bubble-hash.md", "strains/so-loja.md"]
        pruned = self.fixture.post("/admin/knowledge/prune", {"keep_paths": keep}).json()
        self.assertEqual(pruned["chunks_deleted"], 2)  # os dois arquivos bloqueados saem mesmo listados
        # um arquivo que fica vazio depois do filtro apaga os chunks antigos dele
        self.fixture.post("/admin/knowledge/reindex", {"files": [
            {"path": "strains/so-loja.md", "content": "## Resumo\n\nDisponível na NetSeeds."}]})
        self.assertEqual(self.chunks(), [])

    def test_policy_audit_lists_what_is_left_and_is_empty_after_cleanup(self):
        session = self.fixture.session
        bad = [
            KnowledgeChunk(id=uuid.uuid4(), content="Loja X com frete grátis", embedding=[0.1] * 768,
                           source_file="markets/lojas-sementes-brasil.md", source_type="concept", tags=[]),
            KnowledgeChunk(id=uuid.uuid4(), content="Floração 60 dias. Disponível na NetSeeds.", embedding=[0.1] * 768,
                           source_file="strains/ak-47.md", source_type="strain", tags=[]),
            Strain(id=uuid.uuid4(), name="AK-47", slug="ak-47", summary="Disponível na NetSeeds.",
                   source_file="strains/ak-47.md"),
        ]
        session.add_all(bad)
        session.commit()
        self.assertEqual(self.fixture.client.get("/admin/knowledge/policy-audit").status_code, 403)
        audit = self.fixture.client.get("/admin/knowledge/policy-audit", headers={"X-Admin-Token": "token-de-teste"}).json()
        self.assertEqual(sorted((f["kind"], f["reason"]) for f in audit["findings"]),
                         [("chunk", "blocked_path"), ("chunk", "blocked_text"), ("strain", "blocked_text")])

        # o caminho de produção (push_brain.py): reindexa tudo, faz o prune e atualiza o catálogo
        self.fixture.post("/admin/knowledge/reindex", {"files": [{"path": "strains/ak-47.md", "content": STRAIN_DOC}]})
        self.fixture.post("/admin/knowledge/prune", {"keep_paths": ["strains/ak-47.md", "markets/lojas-sementes-brasil.md"]})
        self.fixture.post("/admin/strains/reindex", {"files": [{"path": "strains/ak-47.md", "content": STRAIN_DOC}]})
        audit = self.fixture.client.get("/admin/knowledge/policy-audit", headers={"X-Admin-Token": "token-de-teste"}).json()
        self.assertEqual(audit["findings"], [])
        self.assertGreater(audit["chunks_checked"], 0)

    def test_search_shows_stored_text_and_policy_flag(self):
        hits = [SimpleNamespace(source_file="strains/ak-47.md", content="Floração 60 dias."),
                SimpleNamespace(source_file="sources/netseeds-2026-04.md", content="Sementes a R$ 90")]
        self.fixture.enterContext(patch("app.api.v1.admin_knowledge.retrieve_chunks", new=AsyncMock(return_value=hits)))
        response = self.fixture.post("/admin/knowledge/search", {"query": "onde compro semente?"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([h["policy_ok"] for h in response.json()], [True, False])

    def test_strain_catalog_has_no_store_or_effects(self):
        doc = STRAIN_DOC.replace("breeder: Serious Seeds", "breeder: Seeds Genetics (marca própria)")
        response = self.fixture.post("/admin/strains/reindex", {"files": [{"path": "strains/ak-47.md", "content": doc}]})
        self.assertEqual(response.status_code, 200, response.text)
        strain = self.fixture.session.scalars(select(Strain)).one()
        self.assertIsNone(strain.breeder)
        self.assertEqual(strain.summary, "AK-47 é um híbrido da Serious Seeds. Floração de 53–63 dias.")


class ChatRetrievalTests(chat_support.ChatBase):
    def test_retrieved_chunks_are_filtered_before_the_prompt(self):
        old = SimpleNamespace(id=uuid.uuid4(), source_file="strains/ak-47.md", chunk_metadata={"title": "AK-47"},
                              content="Floração de 60 dias. Disponível na NetSeeds por R$ 120.\n\n"
                                      "## Efeitos e Usos\n\nUsos medicinais: ansiedade, insônia.")
        store = SimpleNamespace(id=uuid.uuid4(), source_file="markets/lojas-sementes-brasil.md",
                                chunk_metadata={"title": "Lojas"}, content="Compre na loja X com frete grátis.")
        self.enterContext(patch("app.services.rag.retrieve_chunks", new=AsyncMock(return_value=[old, store])))
        data = self.chat()
        prompt = self.llm.calls[0][0].content
        self.assertIn("Floração de 60 dias.", prompt)
        for gone in ("NetSeeds", "ansiedade", "loja X", "Efeitos e Usos"):
            self.assertNotIn(gone, prompt)
        knowledge = [s["id"] for s in data["sources"] if s["kind"] == "knowledge"]
        self.assertEqual(knowledge, [str(old.id)])  # o chunk bloqueado não vira fonte

    def test_topic_limits_are_in_every_bobby_prompt(self):
        self.chat()
        prompt = self.llm.calls[0][0].content
        self.assertIn(TOPIC_LIMITS, prompt)
        for rule in ("Não indique onde comprar ou vender", "Não ensine extração", "Não faça promessas terapêuticas",
                     "menor de 18 anos", "depende da lei local"):
            self.assertIn(rule, SYSTEM_BASE)
        self.assertIn(TOPIC_LIMITS, build_system_prompt())
        self.assertIn(TOPIC_LIMITS, _BRIEF_SYSTEM)
        # a defesa contra injeção de prompt continua
        self.assertIn("REGRAS DE SEGURANÇA", SYSTEM_BASE)


class OffensiveReportTests(chat_support.ChatBase):
    def test_offensive_report_and_alias(self):
        data = self.chat()
        target = {"conversation_id": data["conversation_id"], "message_id": data["message_id"]}
        response = self.client.post("/bob/feedback", json={**target, "reason": "offensive", "message": "Falou de venda"})
        self.assertEqual(response.status_code, 201, response.text)
        row = self.session.scalars(select(BobFeedback)).one()
        self.session.refresh(row)
        self.assertEqual((row.rating, row.reason), ("down", "offensive"))
        # o nome do relatório da loja também vale e é gravado como "offensive"
        again = self.client.post("/bob/feedback", json={**target, "reason": "inappropriate"})
        self.assertEqual(again.status_code, 201, again.text)
        self.session.refresh(row)
        self.assertEqual(row.reason, "offensive")
        admin = self.client.get("/admin/bob-feedback", headers=self.admin()).json()[0]
        self.assertEqual(admin["reason"], "offensive")
        self.assertEqual(self.client.post("/bob/feedback", json={**target, "rating": "up", "reason": "offensive"}).status_code, 422)


class AgeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sem.Base_()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        app = FastAPI()
        app.include_router(auth.router, prefix="/auth")
        app.include_router(users.router, prefix="/users")
        app.dependency_overrides[get_db] = lambda: self.fixture.db
        app.dependency_overrides[get_current_user] = lambda: self.fixture.owner
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.enterContext(patch.object(auth, "create_email_verification_otp", new=AsyncMock(return_value="123456")))
        self.enterContext(patch.object(auth, "send_verification_email", new=AsyncMock()))

    def user(self, email):
        self.fixture.session.expire_all()
        return self.fixture.session.scalars(select(User).where(User.email == email)).one()

    def register(self, email, **extra):
        return self.client.post("/auth/register", json={"email": email, "username": email.split("@")[0],
                                                        "password": "senha-segura-1", **extra})

    def test_register_records_confirmation_and_stays_optional(self):
        self.assertEqual(self.register("maior@letsgrow-teste.com.br", age_confirmed=True).status_code, 201)
        self.assertIsNotNone(self.user("maior@letsgrow-teste.com.br").age_confirmed_at)
        self.assertEqual(self.register("antigo@letsgrow-teste.com.br").status_code, 201)  # app antigo
        self.assertIsNone(self.user("antigo@letsgrow-teste.com.br").age_confirmed_at)
        refused = self.register("nao@letsgrow-teste.com.br", age_confirmed=False)
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.fixture.session.scalars(select(User).where(User.email == "nao@letsgrow-teste.com.br")).all(), [])

    def test_google_sign_in_records_confirmation_once(self):
        self.enterContext(patch.object(settings, "GOOGLE_CLIENT_ID", "cliente-teste"))
        verify = self.enterContext(patch.object(auth.google_id_token, "verify_oauth2_token",
                                                return_value={"sub": "g-1", "email": "google@letsgrow-teste.com.br", "name": "G"}))
        self.assertEqual(self.client.post("/auth/google", json={"id_token": "t"}).status_code, 200)
        self.assertIsNone(self.user("google@letsgrow-teste.com.br").age_confirmed_at)
        self.assertEqual(self.client.post("/auth/google", json={"id_token": "t", "age_confirmed": True}).status_code, 200)
        first = self.user("google@letsgrow-teste.com.br").age_confirmed_at
        self.assertIsNotNone(first)
        self.client.post("/auth/google", json={"id_token": "t", "age_confirmed": True})
        self.assertEqual(self.user("google@letsgrow-teste.com.br").age_confirmed_at, first)  # não muda depois de gravado
        self.assertEqual(verify.call_count, 3)

    def test_existing_account_confirms_in_profile(self):
        response = self.client.patch("/users/me", json={"age_confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["age_confirmed_at"])
        self.assertEqual(self.client.patch("/users/me", json={"age_confirmed": False}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
