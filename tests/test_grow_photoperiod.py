"""Programação de luz com precisão de minuto (UX item 5): photoperiod_light_minutes."""
import importlib.util
import pathlib
import unittest
import uuid
from datetime import date
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_private_access as support
from app.api.v1 import grows
from app.database import get_db
from app.models.base import Base
from app.models.grow import Grow
from app.models.sensor import SensorDevice, SensorReading
from app.services.auth import get_current_user
from app.services.photoperiod import format_photoperiod, minutes_from_hours_text

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / 'migrations' / 'versions' / 'c5d6e7f8a9b0_grow_photoperiod_minutes.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('photoperiod_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PhotoperiodTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        app = FastAPI()
        app.include_router(grows.router, prefix='/grows')
        app.dependency_overrides[get_current_user] = lambda: self.fixture.user
        app.dependency_overrides[get_db] = lambda: support.AsyncSessionAdapter(self.session)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f'/grows/{self.fixture.grow}'

    def patch(self, **body):
        return self.client.patch(self.url, json=body)

    def test_derived_text_for_whole_hours_and_minutes(self):
        self.assertEqual([format_photoperiod(m) for m in (1080, 720, 1440, 1065, 30, 1439)],
                         ['18/6', '12/12', '24/0', '17h45/6h15', '0h30/23h30', '23h59/0h01'])
        self.assertEqual(max(len(format_photoperiod(m)) for m in range(1, 1441)), 11)  # "10h05/13h55"; coluna String(16)
        response = self.patch(photoperiod_on_at='06:00:42', photoperiod_light_minutes=1065, photoperiod_hours='ignorado')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['photoperiod_on_at'], body['photoperiod_light_minutes'], body['photoperiod_hours']),
                         ('06:00:00', 1065, '17h45/6h15'))
        detail = self.client.get(self.url).json()
        self.assertEqual((detail['photoperiod_light_minutes'], detail['photoperiod_hours']), (1065, '17h45/6h15'))
        summary = next(g for g in self.client.get('/grows').json() if g['id'] == str(self.fixture.grow))
        self.assertEqual((summary['photoperiod_light_minutes'], summary['photoperiod_hours']), (1065, '17h45/6h15'))

    def test_minutes_must_be_between_1_and_1440(self):
        self.patch(photoperiod_on_at='06:00')
        for value in (0, -1, 1441, 12.5, 'muito'):
            with self.subTest(value=value):
                self.assertEqual(self.patch(photoperiod_light_minutes=value).status_code, 422)
        for value in (1, 1440):
            with self.subTest(value=value):
                self.assertEqual(self.patch(photoperiod_light_minutes=value).status_code, 200)

    def test_on_at_is_required_except_for_24_0(self):
        response = self.patch(photoperiod_light_minutes=1080)
        self.assertEqual(response.status_code, 422)
        self.assertIn('photoperiod_on_at', response.json()['detail'])
        self.assertEqual(self.patch(photoperiod_light_minutes=1440).json()['photoperiod_hours'], '24/0')
        self.assertEqual(self.patch(photoperiod_on_at='18:30').status_code, 200)
        # Hora já salva: basta mandar a duração.
        self.assertEqual(self.patch(photoperiod_light_minutes=720).json()['photoperiod_hours'], '12/12')
        # Apagar a hora deixaria a duração sem referência.
        self.assertEqual(self.patch(photoperiod_on_at=None).status_code, 422)
        cleared = self.patch(photoperiod_on_at=None, photoperiod_light_minutes=None).json()
        self.assertEqual((cleared['photoperiod_on_at'], cleared['photoperiod_light_minutes'], cleared['photoperiod_hours']), (None, None, None))

    def test_null_minutes_return_to_not_informed(self):
        self.patch(photoperiod_on_at='06:00', photoperiod_light_minutes=1080)
        body = self.patch(photoperiod_light_minutes=None).json()
        self.assertEqual((body['photoperiod_light_minutes'], body['photoperiod_hours'], body['photoperiod_on_at']), (None, None, '06:00:00'))

    def test_old_clients_sending_only_hours_text(self):
        body = self.patch(photoperiod_hours='18/6').json()
        self.assertEqual((body['photoperiod_hours'], body['photoperiod_light_minutes']), ('18/6', 1080))
        body = self.patch(photoperiod_hours='custom').json()
        self.assertEqual((body['photoperiod_hours'], body['photoperiod_light_minutes']), ('custom', None))
        self.assertEqual(self.patch(photoperiod_hours='17 horas').status_code, 422)
        self.enterContext(patch('app.api.v1.grows.check_grow_limit'))
        created = self.client.post('/grows', json={'name': 'Novo', 'photoperiod_hours': '12/12'})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()['photoperiod_light_minutes'], 720)
        self.assertEqual(self.client.post('/grows', json={'name': 'Sem hora', 'photoperiod_light_minutes': 600}).status_code, 422)

    def test_only_the_owner_can_change_the_schedule(self):
        response = self.client.patch(f'/grows/{self.fixture.other_grow}', json={'photoperiod_light_minutes': 1440})
        self.assertEqual(response.status_code, 404)
        self.session.expire_all()
        self.assertIsNone(self.session.get(Grow, self.fixture.other_grow).photoperiod_light_minutes)

    def test_migration_backfills_n_m_and_keeps_custom(self):
        cases = {'18/6': 1080, '24/0': 1440, '12/12': 720, 'custom': None, '0/24': None, '17/6': None, None: None}
        ids = {}
        for text in cases:
            gid = uuid.uuid4()
            ids[gid] = text
            self.session.add(Grow(id=gid, user_id=self.fixture.owner, name=f'g{text}', start_date=date.today(), photoperiod_hours=text))
        self.session.commit()
        migration = load_migration()
        with self.fixture.engine.begin() as conn:
            migration.backfill(conn)
        self.session.expire_all()
        for gid, text in ids.items():
            grow = self.session.get(Grow, gid)
            self.assertEqual(grow.photoperiod_light_minutes, cases[text], text)
            self.assertEqual(grow.photoperiod_hours, text)  # o texto não muda na migração
        self.assertEqual(migration.minutes_from_hours_text('18/6'), minutes_from_hours_text('18/6'))


if __name__ == '__main__':
    unittest.main()
