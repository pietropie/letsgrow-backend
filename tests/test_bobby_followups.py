"""Bobby, segunda rodada (2026-11-12): sugerir genética sem recusar, cobrança só da planta do
assunto e PDF (receita, laudo) chegando ao modelo."""
import base64
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import test_sem_api as sem
from app.api.v1 import ai
from app.models.base import Base
from app.models.bob import BobRecommendation
from app.models.plant import Plant
from app.models.strain import Strain
from app.rag.prompts import SYSTEM_BASE, build_customer_context
from app.services.rag import follow_up_plants, prepare_chat

run = sem.run
PDF = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 receita").decode()


class PromptTests(unittest.TestCase):
    def test_suggesting_strains_is_not_refused(self):
        self.assertIn("nunca responda que não pode indicar genética", SYSTEM_BASE)
        self.assertIn("Se faltar o perfil", SYSTEM_BASE)
        self.assertIn("Se nesta conversa você já recusou antes, corrija agora", SYSTEM_BASE)
        self.assertIn("NÃO é recomendação terapêutica", SYSTEM_BASE)

    def test_follow_up_never_blocks_the_answer(self):
        self.assertIn("Nunca condicione a ajuda à resposta da cobrança", SYSTEM_BASE)
        self.assertNotIn("Só então gere novas recomendações", SYSTEM_BASE)


class FollowUpTests(sem.Base_):
    def setUp(self):
        super().setUp()
        Base.metadata.create_all(self.engine, tables=[Strain.__table__])
        self.blue = Plant(id=uuid.uuid4(), grow_id=self.grow.id, user_id=self.owner.id, strain_name="Blueberry",
                          current_phase="flower")
        self.session.add(self.blue)
        self.session.flush()
        now = datetime.now(timezone.utc)
        self.session.add(BobRecommendation(
            id=uuid.uuid4(), user_id=self.owner.id, plant_id=self.blue.id, plant_name="Blueberry", action="watering",
            recommended_at=now - timedelta(days=1), deadline=now + timedelta(days=1),
        ))
        self.session.commit()
        self.enterContext(patch("app.services.rag.retrieve_chunks", new=AsyncMock(return_value=[])))

    def plants(self, message, plant=None):
        return run(follow_up_plants(self.db, self.owner.id, plant, message))

    def test_which_plants_get_follow_ups(self):
        self.assertEqual(self.plants("O que é tricoma âmbar?"), set())
        self.assertEqual(self.plants("Posso começar o flush na blueberry?"), {self.blue.id})  # citada pelo nome
        self.assertEqual(self.plants("E a umidade?", plant=self.plant), {self.plant.id})  # planta em foco
        self.assertIsNone(self.plants("O que eu preciso fazer hoje?"))  # pergunta sobre o cultivo: todas
        self.assertEqual(self.plants("Tenho uma planta de outra conta chamada X"), set())

    def test_pending_watering_only_in_the_conversation_about_that_plant(self):
        other_topic = run(build_customer_context(self.db, self.owner.id, follow_up_plant_ids=set()))
        self.assertNotIn("PENDENTES", other_topic)
        self.assertNotIn("watering para Blueberry", other_topic)
        self.assertIn("Blueberry", other_topic)  # a planta continua conhecida, só sem cobrança
        about_blue = run(build_customer_context(self.db, self.owner.id, follow_up_plant_ids={self.blue.id}))
        self.assertIn("watering para Blueberry", about_blue)
        self.assertIn("sem condicionar a ajuda", about_blue)
        self.assertIn("watering para Blueberry", run(build_customer_context(self.db, self.owner.id)))  # padrão: todas

    def test_chat_on_another_subject_carries_no_follow_up(self):
        prepared = run(prepare_chat(self.db, self.owner.id, [], "O que é tricoma âmbar?"))
        self.assertNotIn("watering para Blueberry", prepared.text_only_messages[0].content)
        prepared = run(prepare_chat(self.db, self.owner.id, [], "A Blueberry precisa de rega?"))
        self.assertIn("watering para Blueberry", prepared.text_only_messages[0].content)

    def test_catalog_stays_while_the_conversation_is_about_the_profile(self):
        self.session.add(Strain(id=uuid.uuid4(), name="Cheese", slug="cheese", cbd_pct="~1%", source_file="strains/cheese.md"))
        self.session.commit()
        history = [{"role": "user", "content": "O médico deu uma orientação sobre a quantidade de THC."},
                   {"role": "assistant", "content": "Não posso indicar strain para fins medicinais."}]
        prepared = run(prepare_chat(self.db, self.owner.id, history, "E quais você sugere então?"))
        self.assertIn("## Catálogo de genéticas da base", prepared.text_only_messages[0].content)

    def test_pdf_goes_to_the_model_as_a_document(self):
        prepared = run(prepare_chat(self.db, self.owner.id, [], "Veja a receita", images=[PDF], image_keys=["u/receita.pdf"]))
        blocks = prepared.lc_messages[-1].content
        self.assertEqual(blocks[1], {"type": "media", "mime_type": "application/pdf",
                                     "data": PDF.split(",", 1)[1]})
        self.assertEqual([s["title"] for s in prepared.sources if s["kind"] == "photo"], ["PDF enviado"])
        prepared = run(prepare_chat(self.db, self.owner.id, [], "Foto", images=["data:image/jpeg;base64,AAAA"]))
        self.assertEqual(prepared.lc_messages[-1].content[1]["type"], "image_url")


class AttachmentUploadTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid.uuid4())

    def photos(self, key, data):
        with patch.object(ai.storage, "get_bytes", return_value=data):
            return run(ai._photos(self.user, None, [key]))

    def test_uploaded_pdf_is_read_and_sent(self):
        uris, keys = self.photos(f"{self.user.id}/receita.pdf", b"%PDF-1.4")
        self.assertTrue(uris[0].startswith("data:application/pdf;base64,"))
        self.assertEqual(keys, [f"{self.user.id}/receita.pdf"])

    def test_inline_pdf_is_accepted(self):
        uris, _ = run(ai._photos(self.user, [PDF, "data:text/plain;base64,AAAA"], None))
        self.assertEqual(uris, [PDF])

    def test_too_big_or_unsupported_files_are_refused(self):
        with patch.object(ai, "MAX_ATTACHMENT_BYTES", 10):
            with self.assertRaises(HTTPException) as big:
                self.photos(f"{self.user.id}/receita.pdf", b"x" * 11)
        self.assertEqual(big.exception.status_code, 413)
        with self.assertRaises(HTTPException) as other:
            self.photos(f"{self.user.id}/planilha.xlsx", b"x")
        self.assertEqual(other.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
