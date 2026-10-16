import unittest
from unittest.mock import patch

import test_private_access as support
from app.models.plant import Plant


class PlantIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.enterContext(patch('app.api.v1.plants.check_plant_limit'))

    def test_minimal_registration_preserves_unknown_values(self):
        response = self.client.post('/plants', json={
            'display_name': '  Planta da janela  ', 'grow_id': str(self.fixture.grow),
        })
        self.assertEqual(response.status_code, 201, response.text)
        plant = response.json()
        self.assertEqual(plant['display_name'], 'Planta da janela')
        self.assertEqual(plant['strain_name'], '')
        for field in ['strain_type', 'seed_type', 'current_phase']:
            self.assertEqual(plant[field], 'unknown')
        for field in ['genetics', 'germination_date', 'flip_date']:
            self.assertIsNone(plant[field])
        self.assertEqual(self.client.get('/plants/' + plant['id']).json()['display_name'], 'Planta da janela')

    def test_legacy_identity_and_explicit_details_still_work(self):
        response = self.client.post('/plants', json={
            'strain_name': 'Variedade conhecida', 'grow_id': str(self.fixture.grow),
            'strain_type': 'photo', 'seed_type': 'regular', 'current_phase': 'veg',
            'flip_date': '2026-10-01',
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()['display_name'])
        self.assertEqual(response.json()['strain_type'], 'photo')
        self.assertEqual(response.json()['flip_date'], '2026-10-01')

    def test_invalid_identity_and_foreign_environment_are_rejected(self):
        for fields in [{'display_name': '  '}, {'display_name': 'a' * 101}]:
            response = self.client.post('/plants', json={'grow_id': str(self.fixture.grow), **fields})
            self.assertEqual(response.status_code, 422)
        response = self.client.post('/plants', json={'display_name': 'Teste', 'grow_id': str(self.fixture.other_grow)})
        self.assertEqual(response.status_code, 404)

    def test_edit_does_not_overwrite_unknowns_and_diary_uses_name(self):
        response = self.client.patch(f'/plants/{self.fixture.plant}', json={'display_name': 'Janela', 'strain_name': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['genetics'], None)
        diary = self.client.get('/diary').json()[0]
        self.assertEqual(diary['plant_name'], 'Janela')
        self.assertEqual(diary['strain_name'], '')
        for fields in [{'display_name': None}, {'grow_id': None}, {'strain_name': None}]:
            response = self.client.patch(f'/plants/{self.fixture.plant}', json=fields)
            self.assertEqual(response.status_code, 422, response.text)
        plant = self.fixture.session.get(Plant, self.fixture.plant)
        self.assertEqual(plant.display_name, 'Janela')


if __name__ == '__main__':
    unittest.main()
