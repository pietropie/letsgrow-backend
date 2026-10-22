"""Chat do Bobby: ids de mensagem, fontes, gerar de novo, SSE, conversas e avaliacao por resposta.

LLM, base de conhecimento (pgvector) e armazenamento sao simulados; nada externo e chamado.
"""
import asyncio
import json
import uuid
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

import test_sem_api as sem
from app.api.v1 import ai
from app.models.bob import BobFeedback
from app.models.credits import CreditQuote
from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.knowledge import AIConversation
from app.models.user import User

run = sem.run


class FakeLLM:
    def __init__(self, reply="Resposta do Bobby", pieces=("Rega ", "amanhã ", "cedo."), error=None):
        self.reply, self.pieces, self.error = reply, list(pieces), error
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.reply)

    async def astream(self, messages):
        self.calls.append(messages)
        for piece in self.pieces:
            if isinstance(piece, Exception):
                raise piece
            yield SimpleNamespace(content=piece)


class FakeRequest:
    """Request do Starlette so com o que o gerador usa: desconecta depois de N consultas."""
    def __init__(self, disconnect_after: int | None = None):
        self.disconnect_after, self.checks = disconnect_after, 0

    async def is_disconnected(self):
        self.checks += 1
        return self.disconnect_after is not None and self.checks > self.disconnect_after


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class ChatBase(sem.Base_):
    def setUp(self):
        super().setUp()
        self.llm = FakeLLM()
        self.chunk = SimpleNamespace(id=uuid.uuid4(), content="Regue quando o vaso estiver leve.",
                                     source_file="wiki/rega-em-solo.md", chunk_metadata={"title": "Rega em solo"})
        self.enterContext(patch("app.services.rag.get_ai_context", new=AsyncMock(side_effect=lambda db: (None, self.llm, None))))
        self.enterContext(patch("app.services.rag.retrieve_chunks", new=AsyncMock(side_effect=lambda *a, **k: [self.chunk])))

        @asynccontextmanager
        async def session():
            yield self.db
        self.enterContext(patch.object(ai, "stream_session", new=session))

        self.event = GrowEvent(id=uuid.uuid4(), plant_id=self.plant.id, event_type="watering",
                               event_date=self.now - timedelta(days=1), ph_in=6.2)
        self.foreign_event = GrowEvent(id=uuid.uuid4(), plant_id=self.other_plant.id, event_type="watering",
                                       event_date=self.now - timedelta(days=1), ph_in=9.0)
        self.reading = EnvironmentReading(id=uuid.uuid4(), grow_id=self.grow.id, user_id=self.owner.id,
                                          observed_at=self.now - timedelta(hours=2), temperature_c=24.0, humidity_rh=55)
        self.session.add_all([self.event, self.foreign_event, self.reading])
        self.session.commit()

    def chat(self, **body):
        response = self.client.post("/ai/chat", json={"message": "Como está a Aurora?", **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stored(self, conversation_id) -> list[dict]:
        self.session.expire_all()
        return self.session.get(AIConversation, uuid.UUID(conversation_id)).messages

    def queries(self) -> int:
        self.session.expire_all()
        return self.session.get(User, self.owner.id).ai_queries_this_month


class ChatMessageTests(ChatBase):
    def test_reply_has_message_ids_and_only_injected_sources(self):
        data = self.chat(plant_id=str(self.plant.id))
        self.assertEqual(data["reply"], "Resposta do Bobby")
        messages = self.stored(data["conversation_id"])
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual((messages[0]["id"], messages[1]["id"]), (data["user_message_id"], data["message_id"]))

        sources = {(s["kind"], s["id"]): s for s in data["sources"]}
        event = sources[("event", str(self.event.id))]
        self.assertEqual((event["title"], event["scope"]), ("Rega · pH 6,2", "focus"))
        self.assertEqual(sources[("knowledge", str(self.chunk.id))]["title"], "Guia · Rega em solo")
        self.assertIn(("environment_reading", str(self.reading.id)), sources)
        self.assertNotIn(("event", str(self.foreign_event.id)), sources)  # dado de outra conta nunca entra
        self.assertEqual(data["sources"][0]["scope"], "focus")             # foco antes do contexto geral
        self.assertEqual(messages[1]["sources"], data["sources"])
        prompt = self.llm.calls[0][0].content
        self.assertIn("6.2", prompt)
        self.assertIn("Regue quando o vaso estiver leve.", prompt)
        self.assertNotIn("9.0", prompt)
        self.assertEqual(self.queries(), 1)

    def test_follow_up_keeps_conversation_and_other_users_cannot_reach_it(self):
        first = self.chat()
        second = self.chat(conversation_id=first["conversation_id"], message="E a rega?")
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertEqual(len(self.stored(first["conversation_id"])), 4)
        self.assertEqual(self.client.post("/ai/chat", json={"message": "x", "plant_id": str(self.other_plant.id)}).status_code, 404)
        self.current = self.other
        self.assertEqual(self.client.get(f"/ai/conversations/{first['conversation_id']}").status_code, 404)
        stranger = self.chat(conversation_id=first["conversation_id"])  # nao entra na conversa alheia
        self.assertNotEqual(stranger["conversation_id"], first["conversation_id"])
        self.assertEqual(len(self.stored(first["conversation_id"])), 4)

    def test_photo_keys_use_own_uploads(self):
        key = f"{self.owner.id}/{uuid.uuid4()}.jpg"
        with patch("app.services.storage.get_bytes", return_value=b"\xff\xd8jpeg") as get_bytes:
            foreign = f"{self.other.id}/{uuid.uuid4()}.jpg"
            self.assertEqual(self.client.post("/ai/chat", json={"message": "Olha", "photo_keys": [foreign]}).status_code, 404)
            both = self.client.post("/ai/chat", json={"message": "Olha", "photo_keys": [key], "images": ["data:image/png;base64,AA=="]})
            self.assertEqual(both.status_code, 422)
            data = self.chat(message="O que é essa mancha?", photo_keys=[key])
        get_bytes.assert_called_once_with("events", key)
        self.assertEqual(data["sources"][0], {"kind": "photo", "id": key, "title": "Foto enviada", "date": None,
                                              "scope": "message", "plant_id": None, "grow_id": None})
        blocks = self.llm.calls[-1][-1].content
        self.assertEqual(blocks[1]["image_url"]["url"][:23], "data:image/jpeg;base64,")
        self.assertEqual(self.stored(data["conversation_id"])[0]["photo_keys"], [key])

    def test_llm_failure_saves_nothing(self):
        self.llm.error = RuntimeError("IA fora do ar")
        with self.assertRaises(RuntimeError):
            self.client.post("/ai/chat", json={"message": "Oi"})
        self.assertEqual(self.session.scalar(select(func.count(AIConversation.id))), 0)
        self.assertEqual(self.queries(), 0)


class RegenerateTests(ChatBase):
    def test_regenerates_last_answer_as_new_message(self):
        first = self.chat(plant_id=str(self.plant.id))
        self.llm.reply = "Outra resposta"
        again = self.client.post(f"/ai/chat/{first['message_id']}/regenerate")
        self.assertEqual(again.status_code, 200, again.text)
        data = again.json()
        self.assertEqual((data["reply"], data["conversation_id"]), ("Outra resposta", first["conversation_id"]))
        self.assertNotEqual(data["message_id"], first["message_id"])
        self.assertEqual(data["user_message_id"], first["user_message_id"])
        self.assertTrue(any(s["id"] == str(self.event.id) for s in data["sources"]))
        messages = self.stored(first["conversation_id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["previous"][0]["content"], "Resposta do Bobby")
        self.assertEqual(self.llm.calls[-1][-1].content, "Como está a Aurora?")
        self.assertEqual(self.queries(), 2)  # conta como mensagem nova

        self.assertEqual(self.client.post(f"/ai/chat/{first['message_id']}/regenerate").status_code, 404)
        self.assertEqual(self.client.post(f"/ai/chat/{first['user_message_id']}/regenerate").status_code, 409)
        self.chat(conversation_id=first["conversation_id"], message="E agora?")
        self.assertEqual(self.client.post(f"/ai/chat/{data['message_id']}/regenerate").status_code, 409)
        self.current = self.other
        self.assertEqual(self.client.post(f"/ai/chat/{data['message_id']}/regenerate").status_code, 404)

    def test_base64_photo_question_cannot_be_regenerated(self):
        data = self.chat(images=["data:image/png;base64,AA=="])
        response = self.client.post(f"/ai/chat/{data['message_id']}/regenerate")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.queries(), 1)

    def test_regenerate_follows_credit_rules(self):
        first = self.chat()
        CreditTests = sem.CreditTests
        CreditTests.enable(self, monthly=10, cost=3)
        missing = self.client.post(f"/ai/chat/{first['message_id']}/regenerate")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (402, "quote_required"))
        quote = CreditTests.quote(self)
        self.llm.error = RuntimeError("fora do ar")
        with self.assertRaises(RuntimeError):
            self.client.post(f"/ai/chat/{first['message_id']}/regenerate", json={"quote_id": quote["quote_id"]})
        self.assertEqual(self.client.get("/credits/balance").json()["available"], 10)
        self.llm.error = None
        quote = CreditTests.quote(self)
        ok = self.client.post(f"/ai/chat/{first['message_id']}/regenerate", json={"quote_id": quote["quote_id"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.client.get("/credits/balance").json()["available"], 7)


class StreamTests(ChatBase):
    def test_stream_events_and_saved_answer(self):
        response = self.client.post("/ai/chat/stream", json={"message": "Quando regar?", "plant_id": str(self.plant.id)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual((response.headers["cache-control"], response.headers["x-accel-buffering"]), ("no-cache", "no"))
        events = sse_events(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names, ["status", "delta", "delta", "delta", "sources", "done"])
        status = events[0][1]
        self.assertEqual(status["records"], 2)
        self.assertEqual(status["text"], "lendo 2 registros da Aurora")
        self.assertEqual("".join(d["text"] for n, d in events if n == "delta"), "Rega amanhã cedo.")
        done = events[-1][1]
        messages = self.stored(done["conversation_id"])
        self.assertEqual((messages[-1]["id"], messages[-1]["content"]), (done["message_id"], "Rega amanhã cedo."))
        self.assertEqual(messages[-1]["sources"], events[-2][1]["sources"])
        self.assertEqual(self.queries(), 1)

    def test_validation_errors_come_before_the_stream(self):
        self.assertEqual(self.client.post("/ai/chat/stream", json={"message": "x", "plant_id": str(self.other_plant.id)}).status_code, 404)
        self.current = self.other
        other_conv = AIConversation(user_id=self.owner.id, messages=[], title="Minha")
        self.session.add(other_conv)
        self.session.commit()
        self.assertEqual(self.client.post("/ai/chat/stream", json={"message": "x", "conversation_id": str(other_conv.id)}).status_code, 404)
        sem.CreditTests.enable(self)
        missing = self.client.post("/ai/chat/stream", json={"message": "x"})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (402, "quote_required"))

    def test_provider_error_becomes_error_event_and_releases(self):
        sem.CreditTests.enable(self)
        quote = sem.CreditTests.quote(self)
        self.llm.pieces = ["Começo", RuntimeError("caiu")]
        response = self.client.post("/ai/chat/stream", json={"message": "x", "quote_id": quote["quote_id"]})
        names = [name for name, _ in sse_events(response.text)]
        self.assertEqual(names, ["status", "delta", "error"])
        self.assertEqual(self.client.get("/credits/balance").json()["available"], 10)
        self.assertEqual(self.session.scalar(select(func.count(AIConversation.id))), 0)
        self.assertEqual(self.queries(), 0)

    def test_client_disconnect_cancels_without_saving_or_charging(self):
        sem.CreditTests.enable(self)
        quote = sem.CreditTests.quote(self)
        reservation = run(sem.credits_service_reserve(self.db, self.owner, quote["quote_id"]))
        self.assertEqual(self.client.get("/credits/balance").json()["available"], 7)

        async def consume():
            out = []
            async for chunk in ai.stream_events(
                FakeRequest(disconnect_after=1), user_id=self.owner.id, message="Quando regar?", conversation_id=None,
                grow_id=None, plant_id=self.plant.id, images=[], keys=[], reservation_id=reservation.id,
            ):
                out.append(chunk)
            return out

        names = [name for name, _ in sse_events("".join(run(consume())))]
        self.assertEqual(names, ["status", "delta"])
        self.assertEqual(self.session.scalar(select(func.count(AIConversation.id))), 0)
        self.assertEqual(self.queries(), 0)
        self.session.expire_all()
        self.assertEqual(self.session.get(CreditQuote, reservation.id).status, "released")
        self.assertEqual(self.client.get("/credits/balance").json()["available"], 10)

    def test_generator_closed_mid_stream_releases(self):
        sem.CreditTests.enable(self)
        quote = sem.CreditTests.quote(self)
        reservation = run(sem.credits_service_reserve(self.db, self.owner, quote["quote_id"]))

        async def close_early():
            gen = ai.stream_events(
                FakeRequest(), user_id=self.owner.id, message="Oi", conversation_id=None, grow_id=None,
                plant_id=None, images=[], keys=[], reservation_id=reservation.id,
            )
            await gen.__anext__()  # status
            await gen.__anext__()  # primeiro delta
            await gen.aclose()     # o servidor fecha o gerador quando o cliente some

        run(close_early())
        self.session.expire_all()
        self.assertEqual(self.session.get(CreditQuote, reservation.id).status, "released")
        self.assertEqual(self.session.scalar(select(func.count(AIConversation.id))), 0)


class ConversationTests(ChatBase):
    def add(self, title, text, minutes_ago, user=None):
        conv = AIConversation(user_id=(user or self.owner).id, title=title,
                              messages=[{"id": str(uuid.uuid4()), "role": "user", "content": text}])
        self.session.add(conv)
        self.session.flush()
        conv.updated_at = self.now - timedelta(minutes=minutes_ago)
        self.session.commit()
        return conv

    def test_pagination_search_rename_and_delete(self):
        a = self.add("Rega da Aurora", "quanto regar", 30)
        b = self.add("Flora", "Nutrição na semana 3", 20)
        c = self.add("Tricomas", "quando colher", 10)
        self.add("Alheia", "nutrição", 5, user=self.other)

        page = self.client.get("/ai/conversations", params={"limit": 2})
        self.assertEqual(page.headers["x-total-count"], "3")
        self.assertEqual([i["id"] for i in page.json()], [str(c.id), str(b.id)])
        self.assertEqual([i["id"] for i in self.client.get("/ai/conversations", params={"limit": 2, "offset": 2}).json()], [str(a.id)])
        found = self.client.get("/ai/conversations", params={"q": "NUTRICAO"})
        self.assertEqual(([i["id"] for i in found.json()], found.headers["x-total-count"]), ([str(b.id)], "1"))
        self.assertEqual([i["id"] for i in self.client.get("/ai/conversations", params={"q": "aurora"}).json()], [str(a.id)])
        self.assertEqual(self.client.get("/ai/conversations", params={"limit": 0}).status_code, 422)

        renamed = self.client.patch(f"/ai/conversations/{a.id}", json={"title": "  Rega  "})
        self.assertEqual((renamed.status_code, renamed.json()["title"]), (200, "Rega"))
        self.assertEqual(self.client.patch(f"/ai/conversations/{a.id}", json={"title": "  "}).status_code, 422)

        self.session.add(BobFeedback(user_id=self.owner.id, conversation_id=a.id, message_id="m", rating="up", status="new"))
        self.session.commit()
        self.current = self.other
        self.assertEqual(self.client.patch(f"/ai/conversations/{a.id}", json={"title": "x"}).status_code, 404)
        self.assertEqual(self.client.delete(f"/ai/conversations/{a.id}").status_code, 404)
        self.current = self.owner
        self.assertEqual(self.client.delete(f"/ai/conversations/{a.id}").status_code, 204)
        self.assertEqual(self.client.get(f"/ai/conversations/{a.id}").status_code, 404)
        self.assertEqual(self.session.scalar(select(func.count(BobFeedback.id))), 0)
        self.assertEqual(self.client.get("/ai/conversations").headers["x-total-count"], "2")


class MessageFeedbackTests(ChatBase):
    def test_rating_and_reason_per_answer(self):
        data = self.chat()
        target = {"conversation_id": data["conversation_id"], "message_id": data["message_id"]}
        up = self.client.post("/bob/feedback", json={**target, "rating": "up"})
        self.assertEqual(up.status_code, 201, up.text)
        down = self.client.post("/bob/feedback", json={**target, "reason": "wrong_record", "message": "A rega foi ontem"})
        self.assertEqual((down.status_code, down.json()["id"]), (201, up.json()["id"]))  # mesma avaliacao, atualizada
        row = self.session.scalars(select(BobFeedback)).one()
        self.session.refresh(row)
        self.assertEqual((row.rating, row.reason, row.message), ("down", "wrong_record", "A rega foi ontem"))

        admin = self.client.get("/admin/bob-feedback", headers=self.admin()).json()[0]
        self.assertEqual((admin["rating"], admin["reason"], admin["message_id"]), ("down", "wrong_record", data["message_id"]))

        bad = [
            {"rating": "up"},                                     # sem a resposta avaliada
            {**target, "rating": "up", "reason": "other"},        # motivo so em "nao gostei"
            {**target, "reason": "outro"},                        # motivo fora da lista
            {"message_id": data["message_id"], "rating": "up"},   # falta conversation_id
            {"message": "   "},                                    # nada a registrar
        ]
        for body in bad:
            self.assertEqual(self.client.post("/bob/feedback", json=body).status_code, 422, body)
        user_msg = {**target, "message_id": data["user_message_id"], "rating": "down"}
        self.assertEqual(self.client.post("/bob/feedback", json=user_msg).status_code, 404)
        self.assertEqual(self.client.post("/bob/feedback", json={**target, "message_id": "nao-existe", "rating": "up"}).status_code, 404)
        self.current = self.other
        self.assertEqual(self.client.post("/bob/feedback", json={**target, "rating": "up"}).status_code, 404)

    def test_free_text_correction_still_works(self):
        response = self.client.post("/bob/feedback", json={"message": "O pH estava errado", "plant_id": str(self.plant.id)})
        self.assertEqual(response.status_code, 201, response.text)
        row = self.session.scalars(select(BobFeedback)).one()
        self.assertEqual((row.rating, row.message_id), (None, None))


if __name__ == "__main__":
    import unittest
    unittest.main()
