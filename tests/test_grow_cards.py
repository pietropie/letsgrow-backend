"""Ambientes v2 (UX item 22): GET /grows com tudo o que o cartão do ambiente mostra.

Roda com AsyncSession real (aiosqlite), como test_grow_detail: a lista agora lê
plantas, vasos e leituras de cada ambiente, e um lazy load daria MissingGreenlet.
"""
import asyncio
import unittest
import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

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
from app.services import home_suggestions
from app.services.auth import get_current_user
from app.services.bob_rules import BobStep

TABLES = [User.__table__, Grow.__table__, Pot.__table__, Plant.__table__, GrowEvent.__table__, Strain.__table__,
          EnvironmentReading.__table__, SensorDevice.__table__, SensorReading.__table__]


class GrowCardTests(unittest.TestCase):
    def test_grow_list_carries_the_whole_card(self):
        asyncio.run(self._run())

    async def _run(self):
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=TABLES))
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

            owner, other = uuid.uuid4(), uuid.uuid4()
            sala, varanda, alheio = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            gelato, blue, arquivada = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            now = datetime.now(timezone.utc)
            async with sessions() as session:
                session.add_all([User(id=owner, email="a@example.test", username="a"),
                                 User(id=other, email="b@example.test", username="b")])
                await session.flush()
                session.add_all([
                    Grow(id=sala, user_id=owner, name="Sala", start_date=date.today() - timedelta(days=47),
                         tent_width_cm=80, tent_depth_cm=80, tent_height_cm=160, photoperiod_on_at=time(6, 0),
                         photoperiod_light_minutes=1080, target_temp_c=25, target_humidity_rh=55,
                         created_at=now),
                    Grow(id=varanda, user_id=owner, name="Varanda", grow_type="outdoor", start_date=date.today(),
                         created_at=now - timedelta(days=1)),
                    Grow(id=alheio, user_id=other, name="Alheio", start_date=date.today()),
                ])
                await session.flush()
                session.add_all([
                    Plant(id=gelato, grow_id=sala, user_id=owner, strain_name="Gelato", current_phase="flower",
                          created_at=now - timedelta(days=2)),
                    Plant(id=blue, grow_id=sala, user_id=owner, strain_name="Blueberry", current_phase="flower",
                          created_at=now),
                    Plant(id=arquivada, grow_id=sala, user_id=owner, strain_name="Velha", current_phase="veg",
                          is_active=False),
                    Plant(grow_id=alheio, user_id=other, strain_name="Privada"),
                    Pot(grow_id=sala, label="A"), Pot(grow_id=sala, label="B"), Pot(grow_id=sala, label="C"),
                    EnvironmentReading(grow_id=sala, user_id=owner, observed_at=now - timedelta(minutes=30),
                                       temperature_c=26.0, humidity_rh=58.0),
                ])
                await session.commit()

            def fake_steps(plant, events, today, **kwargs):
                async def run():
                    if plant.id == gelato:
                        return [BobStep(tier="attention", action="watering", message="Rega atrasada.",
                                        plant_id=plant.id, reason="t")]
                    if plant.id == blue:
                        return [BobStep(tier="critical", action="temperature", message="Temperatura alta.",
                                        plant_id=plant.id, reason="t"),
                                BobStep(tier="info", action="tricomas", message="Olhe os tricomas.",
                                        plant_id=plant.id, reason="t")]
                    return []
                return run()

            async def db():
                async with sessions() as session:
                    yield session

            app = FastAPI()
            app.include_router(grows.router, prefix="/grows")
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner, plan="free")
            app.dependency_overrides[get_db] = db
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            with patch.object(home_suggestions, "compute_bob_steps", new=fake_steps):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get("/grows")
            self.assertEqual(response.status_code, 200, response.text)
            cards = {g["name"]: g for g in response.json()}
            self.assertEqual(set(cards), {"Sala", "Varanda"})  # só os da conta

            card = cards["Sala"]
            self.assertEqual((card["tent_width_cm"], card["tent_depth_cm"], card["tent_height_cm"]), (80, 80, 160))
            self.assertEqual((card["target_temp_c"], card["target_humidity_rh"]), (25, 55))
            self.assertEqual(card["photoperiod_on_at"], "06:00:00")
            self.assertEqual((card["active_plant_count"], card["pot_count"]), (2, 3))
            self.assertEqual(card["active_phases"], {"flower": 2})  # a arquivada não conta
            self.assertEqual(card["days_running"], 47)
            env = card["env"]
            self.assertEqual((env["temperature"]["value"], env["humidity"]["value"]), (26.0, 58.0))
            self.assertEqual(env["temperature"]["source"], "manual")
            self.assertIsNotNone(env["temperature"]["observed_at"])
            self.assertEqual(card["attention_count"], 2)  # a dica "info" não é pendência
            self.assertEqual({k: card["next_action"][k] for k in ("action", "tier", "plant_id", "plant_name")},
                             {"action": "temperature", "tier": "critical", "plant_id": str(blue),
                              "plant_name": "Blueberry"})

            empty = cards["Varanda"]
            self.assertIsNone(empty["env"]["temperature"]["value"])  # sem leitura: nada estimado
            self.assertEqual((empty["pot_count"], empty["attention_count"], empty["active_phases"]), (0, 0, {}))
            self.assertIsNone(empty["next_action"])
            self.assertIsNone(empty["target_temp_c"])
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
