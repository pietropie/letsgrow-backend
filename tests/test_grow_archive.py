"""Arquivar e restaurar ambiente com as plantas; tipo dos registros do ambiente (UX-CORRECOES item 3)."""
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_private_access as support
from app.api.v1 import environment_readings, grows, plants
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.services.auth import get_current_user


class Db(support.AsyncSessionAdapter):
    async def delete(self, obj):
        self.session.delete(obj)


class FailingCommit(support.AsyncSessionAdapter):
    """Simula falha ao gravar: tudo o que a rota fez antes precisa ser descartado."""
    async def commit(self):
        raise RuntimeError('falha ao gravar')


class GrowArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        self.grow = self.fixture.grow
        self.active = self.fixture.plant  # planta ativa criada pelo fixture
        self.second = uuid.uuid4()
        self.earlier = uuid.uuid4()       # arquivada antes, separadamente
        self.session.add_all([
            Plant(id=self.second, grow_id=self.grow, user_id=self.fixture.owner, strain_name='Sol'),
            Plant(id=self.earlier, grow_id=self.grow, user_id=self.fixture.owner, strain_name='Antiga', is_active=False),
        ])
        self.session.commit()
        self.db = support.AsyncSessionAdapter(self.session)
        app = FastAPI()
        app.include_router(grows.router, prefix='/grows')
        app.include_router(environment_readings.router, prefix='/grows')
        app.include_router(plants.router, prefix='/plants')
        app.dependency_overrides[get_current_user] = lambda: self.fixture.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        # Limites do plano ficam fora destes testes (exceto no teste dedicado).
        self.grow_limit = self.enterContext(patch('app.api.v1.grows.check_grow_limit'))
        self.plant_limit = self.enterContext(patch('app.api.v1.grows.check_plant_limit'))

    def state(self):
        self.session.expire_all()
        g = self.session.get(Grow, self.grow)
        rows = {p.id: (p.is_active, p.archived_with_grow_id) for p in self.session.scalars(select(Plant).where(Plant.grow_id == self.grow))}
        return g.status, g.archived_at, rows

    def test_archive_takes_active_plants_along_and_restore_brings_back_only_those(self):
        response = self.client.delete(f'/grows/{self.grow}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()['status'], response.json()['plants_affected']), ('abandoned', 2))
        status, archived_at, rows = self.state()
        self.assertEqual(status, 'abandoned')
        self.assertIsNotNone(archived_at)
        self.assertEqual(rows[self.active], (False, self.grow))
        self.assertEqual(rows[self.second], (False, self.grow))
        self.assertEqual(rows[self.earlier], (False, None))
        listed = {g['id']: g for g in self.client.get('/grows').json()}
        self.assertIsNotNone(listed[str(self.grow)]['archived_at'])

        again = self.client.delete(f'/grows/{self.grow}').json()
        self.assertEqual(again['plants_affected'], 0)

        restored = self.client.post(f'/grows/{self.grow}/restore')
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual((restored.json()['status'], restored.json()['plants_affected']), ('active', 2))
        status, archived_at, rows = self.state()
        self.assertEqual((status, archived_at), ('active', None))
        self.assertEqual(rows[self.active], (True, None))
        self.assertEqual(rows[self.second], (True, None))
        self.assertEqual(rows[self.earlier], (False, None))  # arquivada antes: continua arquivada

    def test_patch_status_uses_the_same_rules(self):
        self.assertEqual(self.client.patch(f'/grows/{self.grow}', json={'status': 'abandoned'}).json()['status'], 'abandoned')
        self.assertEqual(self.state()[2][self.second], (False, self.grow))
        self.assertEqual(self.client.patch(f'/grows/{self.grow}', json={'status': 'active'}).json()['status'], 'active')
        self.assertEqual(self.state()[2][self.second], (True, None))
        self.assertEqual(self.client.patch(f'/grows/{self.grow}', json={'status': 'qualquer'}).status_code, 422)

    def test_plant_reactivated_or_archived_by_hand_is_not_touched_by_restore(self):
        self.client.delete(f'/grows/{self.grow}')
        # Arquivar uma planta de novo pela rota de planta desfaz o vínculo com o ambiente.
        self.client.delete(f'/plants/{self.second}')
        self.client.post(f'/grows/{self.grow}/restore')
        rows = self.state()[2]
        self.assertEqual(rows[self.active], (True, None))
        self.assertEqual(rows[self.second], (False, None))

    def test_failure_while_saving_leaves_no_partial_archive(self):
        self.db = FailingCommit(self.session)
        with self.assertRaises(RuntimeError):
            self.client.delete(f'/grows/{self.grow}')
        self.session.rollback()  # o get_db real faz rollback quando a rota falha
        status, archived_at, rows = self.state()
        self.assertEqual((status, archived_at), ('active', None))
        self.assertEqual(rows[self.active], (True, None))
        self.assertEqual(rows[self.second], (True, None))

    def test_restore_over_the_plan_limit_restores_nothing(self):
        self.client.delete(f'/grows/{self.grow}')
        self.plant_limit.side_effect = HTTPException(status_code=403, detail='Limite de plantas atingido para o plano FREE.')
        response = self.client.post(f'/grows/{self.grow}/restore')
        self.assertEqual(response.status_code, 403)
        self.session.rollback()
        status, _, rows = self.state()
        self.assertEqual(status, 'abandoned')
        self.assertEqual(rows[self.second], (False, self.grow))

    def test_only_the_owner_can_archive_or_restore(self):
        other = self.fixture.other_grow
        self.assertEqual(self.client.delete(f'/grows/{other}').status_code, 404)
        self.assertEqual(self.client.post(f'/grows/{other}/restore').status_code, 404)
        self.session.expire_all()
        self.assertEqual(self.session.get(Grow, other).status, 'active')
        self.assertTrue(self.session.get(Plant, self.fixture.other_plant).is_active)


class ReadingKindTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        app = FastAPI()
        app.include_router(grows.router, prefix='/grows')
        app.include_router(environment_readings.router, prefix='/grows')
        app.dependency_overrides[get_current_user] = lambda: self.fixture.user
        app.dependency_overrides[get_db] = lambda: Db(self.session)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f'/grows/{self.fixture.grow}/readings'

    def test_kind_is_exposed_and_automatic_records_are_read_only(self):
        now = datetime.now(timezone.utc).isoformat()
        manual = self.client.post(self.url, json={'observed_at': now, 'temperature_c': 24, 'notes': 'Potência do exaustor manual'}).json()
        self.assertEqual(manual['kind'], 'manual')
        self.client.patch(f'/grows/{self.fixture.grow}', json={'exhaust_power_pct': 65})
        items = self.client.get(self.url).json()['items']
        auto = next(i for i in items if i['kind'] == 'exhaust_power')
        self.assertEqual(auto['notes'], 'Potência do exaustor registrada como 65%')

        for call in (lambda: self.client.patch(f"{self.url}/{auto['id']}", json={'notes': 'outra'}),
                     lambda: self.client.delete(f"{self.url}/{auto['id']}")):
            response = call()
            self.assertEqual(response.status_code, 409)
            self.assertIn('não pode ser editado nem apagado', response.json()['detail'])
        self.assertEqual(self.client.patch(f"{self.url}/{manual['id']}", json={'notes': 'ok'}).status_code, 200)
        self.assertEqual(self.client.delete(f"{self.url}/{manual['id']}").status_code, 204)

    def test_history_is_paginated_newest_first(self):
        for hour in range(5):
            self.client.post(self.url, json={'observed_at': f'2026-10-1{hour}T12:00:00Z', 'notes': f'n{hour}'})
        page = self.client.get(self.url, params={'limit': 2, 'offset': 1}).json()
        self.assertEqual(page['total'], 5)
        self.assertEqual([i['notes'] for i in page['items']], ['n3', 'n2'])

    def test_backfill_rule_matches_only_exhaust_notes(self):
        """Mesma condição da migração b4c5d6e7f8a9, aplicada às leituras gravadas antes do campo."""
        from sqlalchemy import text
        for notes, temp in [('Potência do exaustor ajustada para 50%', None), ('Potência do exaustor ok', 25.0), ('Nota', None)]:
            self.session.add(EnvironmentReading(grow_id=self.fixture.grow, user_id=self.fixture.owner,
                                                observed_at=datetime.now(timezone.utc), notes=notes, temperature_c=temp))
        self.session.commit()
        self.session.execute(text(
            "UPDATE environment_readings SET kind = 'exhaust_power' "
            "WHERE notes LIKE 'Potência do exaustor %' AND temperature_c IS NULL AND humidity_rh IS NULL"
        ))
        kinds = {r.notes: r.kind for r in self.session.scalars(select(EnvironmentReading))}
        self.assertEqual(kinds, {'Potência do exaustor ajustada para 50%': 'exhaust_power',
                                 'Potência do exaustor ok': 'manual', 'Nota': 'manual'})


if __name__ == '__main__':
    unittest.main()
