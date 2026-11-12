"""Receita ou perfil de canabinoides no Bobby (pedido do usuário, 2026-11-12).

O comportamento depende do LLM; aqui conferimos o que o backend controla: as regras do
prompt, o nome, a ordem da resposta e o catálogo de genéticas que entra no contexto.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import test_sem_api as sem
from app.models.base import Base
from app.models.strain import Strain
from app.rag.prompts import SYSTEM_BASE, TOPIC_LIMITS, build_system_prompt
from app.services import daily_brief
from app.services.rag import prepare_chat
from app.services.strain_catalog import catalog_for_prompt, wants_strain_catalog

run = sem.run
PRESCRIPTION = "Com base nessa receita médica, quais strains seriam legais para eu plantar?"


class PromptRulesTests(unittest.TestCase):
    def test_name_is_bobby(self):
        self.assertIn("Você é o Bobby", SYSTEM_BASE)
        self.assertNotIn("Você é o Bob,", SYSTEM_BASE)
        self.assertIn("SOMENTE o Bobby", SYSTEM_BASE)
        self.assertIn("Você é o Bobby", daily_brief._BRIEF_SYSTEM)
        self.assertTrue(daily_brief._build_cta_prompt("ok", "Aurora", []).startswith("Bobby,"))

    def test_answer_first_then_follow_up(self):
        self.assertIn("Responda PRIMEIRO o que a pessoa perguntou", SYSTEM_BASE)
        self.assertNotIn("SEMPRE pergunte primeiro", SYSTEM_BASE)
        self.assertLess(SYSTEM_BASE.index("ORDEM DA RESPOSTA"), SYSTEM_BASE.index("HIERARQUIA DE PRIORIDADE"))

    def test_prescription_rules(self):
        for rule in (
            "Resuma em uma linha só o perfil pedido",
            "Não repita nome, documento, endereço, CID ou diagnóstico do paciente, nem dados do médico",
            "SOMENTE do \"Catálogo de genéticas da base\"",
            "Nunca sugira genética de fora do catálogo nem invente teores",
            "diga isso com clareza",
            "só laudo de laboratório confirma",
            "a dose\n   e o uso são definidos com o médico",
        ):
            self.assertIn(rule, SYSTEM_BASE, rule)

    def test_limits_keep_no_therapy_promise_and_short_legality(self):
        self.assertIn("Não faça promessas terapêuticas", TOPIC_LIMITS)
        self.assertIn("não indique dose", TOPIC_LIMITS)
        self.assertIn("Legalidade: no máximo uma linha, sem sermão", TOPIC_LIMITS)
        self.assertNotIn("sugira buscar orientação jurídica", TOPIC_LIMITS)
        # compra, venda e extração continuam fora
        self.assertIn("Não indique onde comprar ou vender cannabis", TOPIC_LIMITS)
        self.assertIn("Não ensine extração", TOPIC_LIMITS)

    def test_catalog_section_only_when_given(self):
        self.assertNotIn("Catálogo de genéticas da base\n", build_system_prompt())
        prompt = build_system_prompt(strain_catalog="- Aurora · CBD 1%")
        self.assertIn("## Catálogo de genéticas da base", prompt)
        self.assertIn("Só estas podem ser sugeridas", prompt)

    def test_trigger(self):
        for message in (PRESCRIPTION, "Tenho um laudo pedindo CBD 20:1", "qual genética combina com espaço pequeno pra plantar?",
                        "quero THC baixo"):
            self.assertTrue(wants_strain_catalog(message), message)
        for message in ("como regar a Aurora?", "minha planta tem manchas amarelas", ""):
            self.assertFalse(wants_strain_catalog(message), message)


class CatalogContextTests(sem.Base_):
    def setUp(self):
        super().setUp()
        Base.metadata.create_all(self.engine, tables=[Strain.__table__])
        self.session.add_all([
            Strain(id=uuid.uuid4(), name="Gelato", slug="gelato", strain_type="photo", genetics="hybrid",
                   breeder="Seed Co", thc_pct="20–25%", cbd_pct="<0.1%", dominant_terpene="Limoneno",
                   flowering_days=63, height_cm="100–150 cm", source_file="strains/gelato.md"),
            Strain(id=uuid.uuid4(), name="Cheese", slug="cheese", strain_type="auto", genetics="indica",
                   thc_pct="18%", cbd_pct="~1%", dominant_terpene="Mirceno", flowering_days=56,
                   source_file="strains/cheese.md"),
            Strain(id=uuid.uuid4(), name="Misteriosa", slug="misteriosa", source_file="strains/misteriosa.md"),
        ])
        self.session.commit()
        self.enterContext(patch("app.services.rag.retrieve_chunks", new=AsyncMock(return_value=[])))

    def test_catalog_lists_only_declared_data_highest_cbd_first(self):
        lines = run(catalog_for_prompt(self.db)).splitlines()
        self.assertEqual(lines[0], "- Cheese · automática · índica · THC 18% · CBD ~1% · terpeno dominante: Mirceno · flora ~56 dias")
        self.assertEqual(lines[1], "- Gelato (Seed Co) · fotoperíodo · híbrida · THC 20–25% · CBD <0.1% · "
                                   "terpeno dominante: Limoneno · flora ~63 dias · altura 100–150 cm")
        self.assertEqual(lines[2], "- Misteriosa · THC não informado · CBD não informado · terpeno não informado")

    def test_prescription_question_gets_the_catalog_and_a_source(self):
        prepared = run(prepare_chat(self.db, self.owner.id, [], PRESCRIPTION))
        system = prepared.text_only_messages[0].content
        self.assertIn("## Catálogo de genéticas da base", system)
        self.assertIn("- Cheese · automática", system)
        self.assertIn("strain-catalog", [s["id"] for s in prepared.sources])

    def test_other_questions_do_not_carry_the_catalog(self):
        prepared = run(prepare_chat(self.db, self.owner.id, [], "Como está a umidade da Sala?"))
        self.assertNotIn("## Catálogo de genéticas da base", prepared.text_only_messages[0].content)
        self.assertNotIn("strain-catalog", [s["id"] for s in prepared.sources])


if __name__ == "__main__":
    unittest.main()
