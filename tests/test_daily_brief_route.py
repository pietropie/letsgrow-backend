"""GET /plants/daily-brief no app completo: em produção respondia 422.

A rota fixa estava declarada depois de GET /plants/{plant_id}; o FastAPI testa as rotas na
ordem e lia "daily-brief" como um UUID inválido.
"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.auth import get_current_user


class DailyBriefRouteTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid.uuid4())
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_daily_brief_is_not_taken_by_plant_id_route(self):
        brief = SimpleNamespace(title='Bom dia', body='Nada urgente hoje.', urgent_count=0, severity='ok',
                                plant_id=None, plant_name=None, cta_prompt='', generated_at='2026-10-27T11:00:00Z',
                                reason_tags=[])
        with patch('app.services.daily_brief.generate_daily_brief', new=AsyncMock(return_value=brief)) as generate:
            response = self.client.get('/api/v1/plants/daily-brief')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()['title'], response.json()['severity']), ('Bom dia', 'ok'))
        self.assertEqual(generate.await_args.args[1], self.user.id)

    def test_fixed_plant_routes_come_before_plant_id(self):
        # Qualquer GET fixo em /plants que caia em /{plant_id} responde 422 com loc path.plant_id.
        for path in ('/api/v1/plants/daily-brief', '/api/v1/plants/bob-alerts', '/api/v1/plants/summaries',
                     '/api/v1/plants/grow-labels'):
            with self.subTest(path=path), patch('app.services.daily_brief.generate_daily_brief',
                                                new=AsyncMock(side_effect=RuntimeError('fim do teste'))):
                try:
                    response = self.client.get(path)
                except Exception:
                    continue  # chegou ao endpoint certo (o banco é None aqui)
                self.assertNotIn('plant_id', response.text if response.status_code == 422 else '')


if __name__ == '__main__':
    unittest.main()
