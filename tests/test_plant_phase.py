"""Fase da planta pelos registros FASE (QA 2026-10-18, falha 3) e data de renovacao da cota (falha 6)."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_private_access as support
from app.models.plant import Plant
from app.services.subscription import next_ai_quota_reset


class PlantPhaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.enterContext(patch('app.api.v1.plants.check_plant_limit'))

    def record(self, event_type, when='2026-10-10T12:00:00Z'):
        response = self.client.post(f'/plants/{self.fixture.plant}/events', json={'event_type': event_type, 'event_date': when})
        self.assertEqual(response.status_code, 201, response.text)
        return self.client.get(f'/plants/{self.fixture.plant}').json()

    def test_new_plant_follows_phase_records(self):
        plant = self.client.get(f'/plants/{self.fixture.plant}').json()
        self.assertEqual(plant['current_phase'], 'unknown')
        plant = self.record('germination', '2026-09-01T12:00:00Z')
        self.assertEqual((plant['current_phase'], plant['germination_date']), ('germination', '2026-09-01'))
        self.assertEqual(self.record('seedling')['current_phase'], 'seedling')
        self.assertEqual(self.record('veg')['current_phase'], 'veg')
        plant = self.record('flip', '2026-10-12T12:00:00Z')
        self.assertEqual((plant['current_phase'], plant['flip_date']), ('flower', '2026-10-12'))
        plant = self.record('harvest', '2026-12-01T12:00:00Z')
        self.assertEqual((plant['current_phase'], plant['harvest_date'], plant['is_active']), ('harvest', '2026-12-01', False))

    def test_flip_works_from_unknown_phase(self):
        plant = self.record('flip')
        self.assertEqual((plant['current_phase'], plant['flip_date']), ('flower', '2026-10-10'))

    def test_late_record_does_not_move_phase_back(self):
        self.record('flip', '2026-10-10T12:00:00Z')
        plant = self.record('germination', '2026-08-01T12:00:00Z')
        self.assertEqual(plant['current_phase'], 'flower')
        self.assertEqual(plant['germination_date'], '2026-08-01')  # data vazia ainda e preenchida
        self.assertEqual(plant['flip_date'], '2026-10-10')

    def test_other_records_and_manual_edit(self):
        self.assertEqual(self.record('note')['current_phase'], 'unknown')
        self.record('flip')
        # A ficha corrige a fase pelo PATCH, inclusive para tras
        response = self.client.patch(f'/plants/{self.fixture.plant}', json={'current_phase': 'veg'})
        self.assertEqual(response.json()['current_phase'], 'veg')

    def test_multi_plant_diary_applies_same_rule(self):
        response = self.client.post('/diary', json={'plant_ids': [str(self.fixture.plant)], 'event_type': 'veg', 'event_date': '2026-10-10T12:00:00Z'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.fixture.session.get(Plant, self.fixture.plant).current_phase, 'veg')


class QuotaRenewalTests(unittest.TestCase):
    def test_renews_on_first_day_of_next_month(self):
        now = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)
        self.assertEqual(next_ai_quota_reset(datetime(2026, 10, 16, tzinfo=timezone.utc), now), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(next_ai_quota_reset(datetime(2026, 9, 1), now), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(next_ai_quota_reset(datetime(2026, 12, 3, tzinfo=timezone.utc), datetime(2026, 12, 5, tzinfo=timezone.utc)), datetime(2027, 1, 1, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()
