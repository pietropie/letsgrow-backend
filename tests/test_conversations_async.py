"""Rotas de conversa do Bobby com AsyncSession real (aiosqlite).

O adaptador síncrono dos outros testes recarrega atributos expirados sem erro. Em produção,
ler depois do commit um campo gerado pelo banco (updated_at com onupdate) fora do greenlet
gera MissingGreenlet: o PATCH salvava o título e respondia 500.
"""
import asyncio
import unittest
import uuid
from datetime import date
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import test_private_access as support  # noqa: F401  registra JSONB -> JSON no SQLite
from app.api.v1 import ai
from app.database import get_db
from app.models.base import Base
from app.models.bob import BobFeedback
from app.models.grow import Grow
from app.models.knowledge import AIConversation
from app.models.plant import Plant
from app.models.user import User
from app.services.auth import get_current_user

TABLES = [User.__table__, Grow.__table__, Plant.__table__, AIConversation.__table__, BobFeedback.__table__]


class ConversationAsyncTests(unittest.TestCase):
    def test_conversation_routes_on_async_session(self):
        asyncio.run(self._run())

    async def _run(self):
        engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=TABLES))
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

            owner, other = uuid.uuid4(), uuid.uuid4()
            mine, foreign = uuid.uuid4(), uuid.uuid4()
            async with sessions() as session:
                session.add_all([User(id=owner, email='a@example.test', username='a'),
                                 User(id=other, email='b@example.test', username='b')])
                await session.flush()
                session.add(Grow(id=uuid.uuid4(), user_id=owner, name='Tenda', start_date=date.today()))
                session.add_all([
                    AIConversation(id=mine, user_id=owner, title='Rega',
                                   messages=[{'id': 'm1', 'role': 'user', 'content': 'Quando regar?'}]),
                    AIConversation(id=foreign, user_id=other, title='Alheia', messages=[]),
                ])
                await session.commit()

            async def db():
                async with sessions() as session:
                    yield session

            app = FastAPI()
            app.include_router(ai.router, prefix='/ai')
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner, plan='free')
            app.dependency_overrides[get_db] = db
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                renamed = await client.patch(f'/ai/conversations/{mine}', json={'title': 'Rega da Aurora'})
                self.assertEqual(renamed.status_code, 200, renamed.text)
                body = renamed.json()
                self.assertEqual((body['id'], body['title'], body['message_count']), (str(mine), 'Rega da Aurora', 1))
                self.assertIsNotNone(body['updated_at'])

                listed = await client.get('/ai/conversations', params={'q': 'aurora'})
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(([c['id'] for c in listed.json()], listed.headers['x-total-count']), ([str(mine)], '1'))
                detail = await client.get(f'/ai/conversations/{mine}')
                self.assertEqual((detail.status_code, detail.json()['title']), (200, 'Rega da Aurora'))

                self.assertEqual((await client.patch(f'/ai/conversations/{foreign}', json={'title': 'x'})).status_code, 404)
                self.assertEqual((await client.delete(f'/ai/conversations/{foreign}')).status_code, 404)
                self.assertEqual((await client.delete(f'/ai/conversations/{mine}')).status_code, 204)
                self.assertEqual((await client.get(f'/ai/conversations/{mine}')).status_code, 404)
        finally:
            await engine.dispose()

    def test_chat_regenerate_stream_and_feedback_on_async_session(self):
        asyncio.run(self._run_chat())

    async def _run_chat(self):
        import test_sem_api as sem
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, patch

        from fastapi import Depends

        from app.api.v1 import bobby
        from test_bobby_chat import FakeLLM, sse_events

        engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[m.__table__ for m in sem.TABLES]))
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            owner = uuid.uuid4()
            async with sessions() as session:
                session.add(User(id=owner, email='a@example.test', username='a', plan='jardineiro'))
                await session.commit()

            async def db():
                async with sessions() as session:
                    yield session

            async def current(session: AsyncSession = Depends(get_db)):
                return await session.get(User, owner)  # como em produção: o usuário vem da sessão da requisição

            app = FastAPI()
            app.include_router(ai.router, prefix='/ai')
            app.include_router(bobby.router, prefix='/bob')
            app.dependency_overrides[get_current_user] = current
            app.dependency_overrides[get_db] = db
            llm = FakeLLM()
            with ExitStack() as stack:
                stack.enter_context(patch('app.services.rag.get_ai_context', new=AsyncMock(return_value=(None, llm, None))))
                stack.enter_context(patch('app.services.rag.retrieve_chunks', new=AsyncMock(return_value=[])))
                stack.enter_context(patch.object(ai, 'stream_session', new=sessions))
                stack.enter_context(patch('app.services.subscription._plan_limits',
                                          return_value={'ai_queries_per_month': 999999}))
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    chat = await client.post('/ai/chat', json={'message': 'Quando regar?'})
                    self.assertEqual(chat.status_code, 200, chat.text)
                    first = chat.json()
                    again = await client.post(f"/ai/chat/{first['message_id']}/regenerate")
                    self.assertEqual(again.status_code, 200, again.text)

                    stream = await client.post('/ai/chat/stream', json={'message': 'E a luz?',
                                                                       'conversation_id': first['conversation_id']})
                    self.assertEqual(stream.status_code, 200, stream.text)
                    events = sse_events(stream.text)
                    self.assertEqual(events[-1][0], 'done', events)

                    target = {'conversation_id': first['conversation_id'], 'message_id': events[-1][1]['message_id']}
                    up = await client.post('/bob/feedback', json={**target, 'rating': 'up'})
                    self.assertEqual(up.status_code, 201, up.text)
                    down = await client.post('/bob/feedback', json={**target, 'reason': 'missing_context'})
                    self.assertEqual((down.status_code, down.json()['id']), (201, up.json()['id']))

                    detail = (await client.get(f"/ai/conversations/{first['conversation_id']}")).json()
                    self.assertEqual(len(detail['messages']), 4)
                    self.assertEqual(detail['messages'][-1]['feedback'], {'rating': 'down', 'reason': 'missing_context'})
            async with sessions() as session:
                self.assertEqual((await session.get(User, owner)).ai_queries_this_month, 3)
        finally:
            await engine.dispose()


if __name__ == '__main__':
    unittest.main()
