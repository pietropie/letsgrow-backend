"""GET /grows/{id} com AsyncSession real (aiosqlite).

Os demais testes usam um adaptador síncrono, em que relações lazy carregam
sem erro. Em produção a sessão é assíncrona e um lazy load fora do greenlet
gera MissingGreenlet (HTTP 500), por isso esta rota roda com AsyncSession.
"""
import asyncio
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import test_private_access as support  # noqa: F401  registra JSONB -> JSON no SQLite
from app.api.v1 import grows
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.sensor import SensorDevice, SensorReading
from app.models.strain import Strain
from app.models.user import User
from app.services.auth import get_current_user

TABLES = [User.__table__, Grow.__table__, Pot.__table__, Plant.__table__, GrowEvent.__table__, Strain.__table__,
          EnvironmentReading.__table__, SensorDevice.__table__, SensorReading.__table__]


class GrowDetailTests(unittest.TestCase):
    def test_detail_with_plants_on_async_session(self):
        asyncio.run(self._run())

    async def _run(self):
        engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=TABLES))
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

            owner, other = uuid.uuid4(), uuid.uuid4()
            grow_id, other_grow = uuid.uuid4(), uuid.uuid4()
            older, newer = uuid.uuid4(), uuid.uuid4()
            now = datetime.now(timezone.utc)
            async with sessions() as session:
                session.add_all([User(id=owner, email='a@example.test', username='a'),
                                 User(id=other, email='b@example.test', username='b')])
                await session.flush()
                session.add_all([Grow(id=grow_id, user_id=owner, name='Tenda 80', start_date=date.today()),
                                 Grow(id=other_grow, user_id=other, name='Alheio', start_date=date.today())])
                await session.flush()
                session.add_all([
                    Plant(id=older, grow_id=grow_id, user_id=owner, strain_name='Gelato', current_phase='flower',
                          created_at=now - timedelta(days=2)),
                    Plant(id=newer, grow_id=grow_id, user_id=owner, strain_name='Blueberry', display_name='Blue',
                          current_phase='veg', created_at=now),
                    Plant(grow_id=other_grow, user_id=other, strain_name='Privada'),
                ])
                await session.commit()

            async def db():
                async with sessions() as session:
                    yield session

            app = FastAPI()
            app.include_router(grows.router, prefix='/grows')
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner, plan='free')
            app.dependency_overrides[get_db] = db
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                response = await client.get(f'/grows/{grow_id}')
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body['name'], 'Tenda 80')
                self.assertEqual([p['id'] for p in body['plants']], [str(newer), str(older)])
                self.assertEqual(body['plants'][0]['display_name'], 'Blue')
                self.assertEqual(body['plants'][1]['strain_name'], 'Gelato')
                self.assertIn('env', body)

                response = await client.get(f'/grows/{other_grow}')
                self.assertEqual(response.status_code, 404)
        finally:
            await engine.dispose()


if __name__ == '__main__':
    unittest.main()
