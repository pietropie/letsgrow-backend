"""Anexos do chat do Bobby: 30 dias de guarda, estado na conversa e "Guardar no calendário"."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

import test_sem_api as sem
from app.models.event import GrowEvent
from app.models.knowledge import AIConversation
from app.services import chat_attachments

run = sem.run


class AttachmentTests(sem.Base_):
    def setUp(self):
        super().setUp()
        self.objects = {}  # MinIO de mentira: chave → bytes
        self.enterContext(patch("app.services.storage.get_bytes", side_effect=lambda b, k: self.objects[k]))
        self.enterContext(patch("app.services.storage.put_bytes",
                                side_effect=lambda b, k, data, ct: self.objects.__setitem__(k, data)))
        self.enterContext(patch("app.services.storage.delete_object", side_effect=lambda b, k: self.objects.pop(k, None)))
        self.enterContext(patch("app.services.storage.presign_download", side_effect=lambda b, k: f"https://s3/{k}"))
        self.enterContext(patch("app.services.storage.count_objects", side_effect=lambda b: len(self.objects)))
        self.old_photo = f"{self.owner.id}/antiga.jpg"
        self.new_photo = f"{self.owner.id}/nova.jpg"
        self.pdf = f"{self.owner.id}/receita.pdf"
        for key in (self.old_photo, self.new_photo, self.pdf):
            self.objects[key] = b"bytes"
        now = datetime.now(timezone.utc)
        self.old_sent = now - timedelta(days=35)
        self.new_sent = now - timedelta(days=2)
        self.conversation = AIConversation(
            id=uuid.uuid4(), user_id=self.owner.id, plant_id=self.plant.id, title="Fotos",
            messages=[
                {"id": "m1", "role": "user", "content": "Olha a folha", "timestamp": self.old_sent.isoformat(),
                 "photo_keys": [self.old_photo]},
                {"id": "a1", "role": "assistant", "content": "Parece ok.", "timestamp": self.old_sent.isoformat()},
                {"id": "m2", "role": "user", "content": "E agora?", "timestamp": self.new_sent.isoformat(),
                 "photo_keys": [self.new_photo, self.pdf]},
                {"id": "a2", "role": "assistant", "content": "Melhorou.", "timestamp": self.new_sent.isoformat()},
            ],
        )
        self.session.add(self.conversation)
        self.session.commit()

    def detail(self):
        response = self.client.get(f"/ai/conversations/{self.conversation.id}")
        self.assertEqual(response.status_code, 200, response.text)
        return {m["id"]: m for m in response.json()["messages"]}

    def save(self, **body):
        payload = {"message_id": "m2", "photo_key": self.new_photo, "plant_id": str(self.plant.id), **body}
        return self.client.post(f"/ai/conversations/{self.conversation.id}/attachments/save", json=payload)

    # ── estado e retenção ──────────────────────────────────────────────────

    def test_conversation_shows_each_attachment_with_its_expiry(self):
        attachments = {a["key"]: a for a in self.detail()["m2"]["attachments"]}
        photo, pdf = attachments[self.new_photo], attachments[self.pdf]
        self.assertEqual((photo["kind"], photo["available"], photo["can_save"]), ("image", True, True))
        self.assertEqual(photo["url"], f"https://s3/{self.new_photo}")
        self.assertEqual(datetime.fromisoformat(photo["expires_at"]), self.new_sent + timedelta(days=30))
        self.assertEqual((pdf["kind"], pdf["can_save"]), ("pdf", False))
        self.assertNotIn("attachments", self.detail()["a2"])

    def test_attachments_leave_after_30_days_and_the_message_stays(self):
        self.assertEqual(run(chat_attachments.expire_attachments(self.db)), 1)
        self.assertNotIn(self.old_photo, self.objects)
        self.assertIn(self.new_photo, self.objects)  # 2 dias: fica
        message = self.detail()["m1"]
        self.assertEqual(message["content"], "Olha a folha")
        self.assertNotIn("photo_keys", message)
        self.assertEqual(message["attachments"], [{
            "key": self.old_photo, "kind": "image", "available": False, "url": None,
            "expires_at": (self.old_sent + timedelta(days=30)).isoformat(), "can_save": False, "saved_event_id": None,
        }])
        self.assertEqual(run(chat_attachments.expire_attachments(self.db)), 0)  # nada mais a apagar

    def test_each_run_deletes_at_most_the_backup_safe_amount(self):
        self.session.scalars(select(AIConversation)).one().messages = [
            {"id": "x", "role": "user", "timestamp": self.old_sent.isoformat(),
             "photo_keys": [f"{self.owner.id}/{i}.jpg" for i in range(3)]},
        ]
        self.session.commit()
        with patch.object(chat_attachments, "delete_cap", return_value=2):
            self.assertEqual(run(chat_attachments.expire_attachments(self.db)), 2)
            self.assertEqual(run(chat_attachments.expire_attachments(self.db)), 1)

    def test_cap_is_a_tenth_of_the_bucket_with_a_floor_of_ten(self):
        with patch("app.services.storage.count_objects", return_value=50):
            self.assertEqual(chat_attachments.delete_cap(), 10)
        with patch("app.services.storage.count_objects", return_value=2000):
            self.assertEqual(chat_attachments.delete_cap(), 200)  # metade da trava do backup (20%)

    def test_regenerating_a_question_whose_attachment_expired_asks_to_resend(self):
        conversation = self.session.scalars(select(AIConversation)).one()
        conversation.messages = conversation.messages[:2]  # a resposta da pergunta antiga é a última
        self.session.commit()
        run(chat_attachments.expire_attachments(self.db))
        response = self.client.post(f"/ai/chat/a1/regenerate", json={})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("expirou", response.json()["detail"])

    def test_deleting_the_conversation_deletes_its_attachments(self):
        self.assertEqual(self.client.delete(f"/ai/conversations/{self.conversation.id}").status_code, 204)
        self.assertNotIn(self.new_photo, self.objects)
        self.assertNotIn(self.pdf, self.objects)

    # ── guardar no calendário ──────────────────────────────────────────────

    def test_save_creates_a_plant_photo_record_outside_retention(self):
        response = self.save()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        event = self.session.get(GrowEvent, uuid.UUID(body["event_id"]))
        self.assertEqual((event.plant_id, event.event_type, event.notes), (self.plant.id, "note", "Foto da conversa com o Bobby"))
        copy = event.photo_keys[0]
        self.assertNotEqual(copy, self.new_photo)  # cópia própria: a retenção do chat não a apaga
        self.assertTrue(copy.startswith(f"{self.owner.id}/") and copy.endswith(".jpg"))
        self.assertEqual(body["photo_url"], f"https://s3/{copy}")
        self.assertEqual(event.event_date.replace(tzinfo=timezone.utc), self.new_sent)  # o dia em que foi enviada
        attachment = next(a for a in self.detail()["m2"]["attachments"] if a["key"] == self.new_photo)
        self.assertEqual(attachment["saved_event_id"], body["event_id"])

        self.assertEqual(self.save().json()["event_id"], body["event_id"])  # de novo: o mesmo registro
        with patch.object(chat_attachments, "RETENTION_DAYS", 1):
            run(chat_attachments.expire_attachments(self.db))
        self.assertIn(copy, self.objects)  # a foto guardada fica

    def test_save_with_another_date(self):
        when = "2026-11-10T12:00:00Z"
        body = self.save(event_date=when).json()
        self.assertTrue(body["event_date"].startswith("2026-11-10T12:00:00"))

    def test_save_refusals(self):
        self.assertEqual(self.save(photo_key=self.pdf).status_code, 422)  # PDF não vai para o calendário
        self.assertEqual(self.save(plant_id=str(self.other_plant.id)).status_code, 404)  # planta de outra conta
        self.assertEqual(self.save(photo_key=f"{self.owner.id}/nao-existe.jpg").status_code, 404)
        self.assertEqual(self.save(message_id="a2").status_code, 404)  # resposta do Bobby não tem anexo
        run(chat_attachments.expire_attachments(self.db))
        expired = self.save(message_id="m1", photo_key=self.old_photo)
        self.assertEqual(expired.status_code, 410, expired.text)
        self.current = self.other
        self.assertEqual(self.save().status_code, 404)  # conversa de outra conta


if __name__ == "__main__":
    unittest.main()
