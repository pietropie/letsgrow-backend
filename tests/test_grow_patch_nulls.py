"""PATCH /grows/{id}: null enviado de propósito apaga; campo ausente não muda (UX item 4)."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_private_access as support
from app.api.v1 import grows
from app.database import get_db
from app.models.base import Base
from app.models.grow import Grow
from app.models.sensor import SensorDevice, SensorReading
from app.services.auth import get_current_user


class GrowPatchNullTests(unittest.TestCase):
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
        self.saved = {'carbon_filter': True, 'dehumidifier': False, 'exhaust_cfm': 400, 'target_temp_c': 24.5,
                      'notes': 'Sala do fundo', 'fans': [{'location': 'copa', 'watts': 25}], 'exhaust_power_pct': 50}
        self.assertEqual(self.client.patch(self.url, json=self.saved).status_code, 200)

    def grow(self):
        self.session.expire_all()
        return self.session.get(Grow, self.fixture.grow)

    def test_null_clears_optional_fields_and_dont_know_on_booleans(self):
        response = self.client.patch(self.url, json={'carbon_filter': None, 'exhaust_cfm': None, 'target_temp_c': None, 'notes': None})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([body[k] for k in ('carbon_filter', 'exhaust_cfm', 'target_temp_c', 'notes')], [None, None, None, None])
        g = self.grow()
        self.assertIsNone(g.carbon_filter)  # "Não sei" substitui uma resposta já dada
        self.assertIsNone(g.exhaust_cfm)
        self.assertFalse(g.dehumidifier)    # não enviado: continua False

    def test_absent_fields_are_not_changed(self):
        self.client.patch(self.url, json={'name': 'Sala nova'})
        g = self.grow()
        self.assertEqual(g.name, 'Sala nova')
        self.assertEqual((g.carbon_filter, g.dehumidifier, g.exhaust_cfm, g.target_temp_c, g.notes, g.exhaust_power_pct),
                         (True, False, 400, 24.5, 'Sala do fundo', 50))
        self.assertEqual(g.fans, [{'location': 'copa', 'watts': 25}])

    def test_null_on_required_fields_is_rejected_and_nothing_changes(self):
        for field in ('name', 'grow_type', 'status', 'has_environment_sensors', 'exhaust_power_pct'):
            with self.subTest(field=field):
                response = self.client.patch(self.url, json={field: None, 'notes': 'não deve gravar'})
                self.assertEqual(response.status_code, 422)
                self.assertIn('não podem ficar vazios', response.text)
                self.assertIn(field, response.text)
        g = self.grow()
        self.assertEqual((g.notes, g.exhaust_power_pct, g.status), ('Sala do fundo', 50, 'active'))

    def test_fans_empty_list_and_null_are_distinct(self):
        self.assertEqual(self.client.patch(self.url, json={'fans': []}).json()['fans'], [])
        self.assertEqual(self.grow().fans, [])           # sem ventiladores
        self.assertIsNone(self.client.patch(self.url, json={'fans': None}).json()['fans'])
        self.assertIsNone(self.grow().fans)              # não informado


if __name__ == '__main__':
    unittest.main()
