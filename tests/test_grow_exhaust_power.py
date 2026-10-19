"""Potência do exaustor registrada no ambiente (UX-CORRECOES 2b): só registro, não comando."""
import asyncio
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import test_private_access as support
from app.api.v1 import environment_readings, grows
from app.database import get_db
from app.models.environment_reading import EnvironmentReading
from app.models.base import Base
from app.models.grow import Grow
from app.models.sensor import SensorDevice, SensorReading
from app.rag.prompts import build_grow_context
from app.services.auth import get_current_user


class ExhaustPowerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        # As respostas de grow trazem as medições do ambiente (inclui sensores).
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        self.user = self.fixture.user
        app = FastAPI()
        app.include_router(grows.router, prefix='/grows')
        app.include_router(environment_readings.router, prefix='/grows')
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: support.AsyncSessionAdapter(self.session)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def patch(self, grow, **body):
        return self.client.patch(f'/grows/{grow}', json=body)

    def history_notes(self, grow):
        rows = self.client.get(f'/grows/{grow}/readings').json()['items']
        return [r['notes'] for r in rows]

    def test_accepts_0_to_100_in_steps_of_5_only(self):
        for value in (0, 5, 65, 100):
            with self.subTest(value=value):
                response = self.patch(self.fixture.grow, exhaust_power_pct=value)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['exhaust_power_pct'], value)
        for value in (-5, 105, 67, 2.5, 'alto'):
            with self.subTest(value=value):
                self.assertEqual(self.patch(self.fixture.grow, exhaust_power_pct=value).status_code, 422)
        self.assertEqual(self.session.get(Grow, self.fixture.grow).exhaust_power_pct, 100)

    def test_only_the_owner_can_record(self):
        response = self.patch(self.fixture.other_grow, exhaust_power_pct=50)
        self.assertEqual(response.status_code, 404)
        other = self.session.get(Grow, self.fixture.other_grow)
        self.assertIsNone(other.exhaust_power_pct)
        self.assertEqual(self.session.scalar(select(func.count(EnvironmentReading.id))), 0)

    def test_history_record_only_when_the_value_changes(self):
        self.patch(self.fixture.grow, exhaust_power_pct=60)
        self.patch(self.fixture.grow, exhaust_power_pct=60)       # mesmo valor: sem registro
        self.patch(self.fixture.grow, name='Sala nova')           # outro campo: sem registro
        self.patch(self.fixture.grow, exhaust_power_pct=65)
        self.assertEqual(self.history_notes(self.fixture.grow), [
            'Potência do exaustor ajustada para 65%',
            'Potência do exaustor registrada como 60%',
        ])
        rows = self.session.scalars(select(EnvironmentReading)).all()
        self.assertTrue(all(r.temperature_c is None and r.humidity_rh is None for r in rows))
        self.assertTrue(all(r.grow_id == self.fixture.grow and r.user_id == self.fixture.owner for r in rows))

    def test_updated_at_is_set_by_the_server_and_returned_by_get(self):
        first = self.patch(self.fixture.grow, exhaust_power_pct=40).json()
        self.assertIsNotNone(first['exhaust_power_updated_at'])
        # O cliente não define o horário: o campo é ignorado no corpo.
        same = self.patch(self.fixture.grow, exhaust_power_pct=40, exhaust_power_updated_at='2020-01-01T00:00:00Z').json()
        self.assertEqual(same['exhaust_power_updated_at'], first['exhaust_power_updated_at'])
        changed = self.patch(self.fixture.grow, exhaust_power_pct=45).json()
        self.assertGreaterEqual(changed['exhaust_power_updated_at'], first['exhaust_power_updated_at'])
        detail = self.client.get(f'/grows/{self.fixture.grow}').json()
        self.assertEqual(detail['exhaust_power_pct'], 45)
        self.assertEqual(detail['exhaust_power_updated_at'], changed['exhaust_power_updated_at'])

    def test_unrecorded_power_is_null_and_bobby_sees_the_record(self):
        self.assertIsNone(self.client.get(f'/grows/{self.fixture.grow}').json()['exhaust_power_pct'])
        self.patch(self.fixture.grow, exhaust_power_pct=65)
        grow = self.session.get(Grow, self.fixture.grow)
        context = asyncio.run(build_grow_context(support.AsyncSessionAdapter(self.session), grow))
        self.assertIn('Exaustor: potencia registrada 65%', context)
        self.assertIn('o app nao controla o exaustor', context)
        self.assertIn('Potência do exaustor registrada como 65%', context)


if __name__ == '__main__':
    unittest.main()
