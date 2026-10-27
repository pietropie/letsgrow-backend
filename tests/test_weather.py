"""Clima do Início (UX item 11): /weather com máxima/mínima e grow_id, busca de cidade e local do ambiente.

Nada chama o Open-Meteo de verdade: `_open_meteo` (ou o httpx) é sempre substituído.
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_private_access as support
from app.api.v1 import grows, weather
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.services import bob_weather
from app.services.auth import get_current_user

REAL_OPEN_METEO = weather._open_meteo

FORECAST = {
    'current': {'temperature_2m': 31.24, 'apparent_temperature': 33.0, 'relative_humidity_2m': 45, 'uv_index': 7.1,
                'wind_speed_10m': 12.0, 'weather_code': 2, 'is_day': 1},
    'timezone': 'America/Sao_Paulo', 'utc_offset_seconds': -10800,
    'daily': {'time': ['2026-10-27'], 'temperature_2m_max': [32.46], 'temperature_2m_min': [18.04],
              'precipitation_probability_max': [20]},
}
GEOCODING = {'results': [
    {'name': 'Campinas', 'latitude': -22.90556, 'longitude': -47.06083, 'admin1': 'São Paulo', 'country': 'Brasil',
     'country_code': 'BR'},
    {'name': 'Campinas do Sul', 'latitude': -27.71667, 'longitude': -52.62472, 'admin1': 'Rio Grande do Sul',
     'country': 'Brasil', 'country_code': 'BR'},
    {'name': 'Campina', 'latitude': 45.08, 'longitude': 25.73, 'admin1': 'Prahova', 'country': 'Romênia',
     'country_code': 'RO'},
    {'name': 'Sem coordenada', 'admin1': 'X'},
]}


class WeatherTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        app = FastAPI()
        app.include_router(weather.router)
        app.include_router(grows.router, prefix='/grows')
        app.dependency_overrides[get_current_user] = lambda: self.fixture.user
        app.dependency_overrides[weather.grow_weather_owner] = lambda: self.fixture.user
        app.dependency_overrides[get_db] = lambda: support.AsyncSessionAdapter(self.session)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        weather._cache.clear()
        weather._places_cache.clear()
        self.addCleanup(weather._cache.clear)
        self.addCleanup(weather._places_cache.clear)
        self.open_meteo = self.enterContext(patch.object(weather, '_open_meteo', new=AsyncMock(return_value=FORECAST)))

    def grow(self, grow_id=None):
        self.session.expire_all()
        return self.session.get(Grow, grow_id or self.fixture.grow)

    def place(self, grow_id=None, **values):
        grow = self.grow(grow_id)
        for key, value in values.items():
            setattr(grow, key, value)
        self.session.commit()


class WeatherByCoordinateTests(WeatherTestCase):
    def test_returns_today_max_min_and_fetch_time_and_uses_cache(self):
        before = datetime.now(timezone.utc)
        first = self.client.get('/weather', params={'lat': -22.90556, 'lon': -47.06083})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual((body['temperature'], body['humidity'], body['weather_description']),
                         (31.2, 45, 'Parcialmente nublado'))
        self.assertEqual((body['temp_max'], body['temp_min'], body['precipitation_probability_max']), (32.5, 18.0, 20))
        self.assertGreaterEqual(datetime.fromisoformat(body['fetched_at'].replace('Z', '+00:00')), before)
        self.assertIsNone(body['weather_tip'])
        url, params = self.open_meteo.await_args.args[:2]
        self.assertEqual(url, weather.FORECAST_URL)
        self.assertEqual((params['latitude'], params['longitude']), (-22.91, -47.06))  # nunca a coordenada exata
        self.assertIn('temperature_2m_max', params['daily'])
        self.assertIn('temperature_2m_min', params['daily'])

        again = self.client.get('/weather', params={'lat': -22.9061, 'lon': -47.0612})  # mesmo quadrado de ~1 km
        self.assertEqual(again.json()['fetched_at'], body['fetched_at'])
        self.assertEqual(self.open_meteo.await_count, 1)

    def test_missing_daily_keeps_current_weather(self):
        self.open_meteo.return_value = {'current': FORECAST['current']}
        body = self.client.get('/weather', params={'lat': 1, 'lon': 2}).json()
        self.assertEqual((body['temperature'], body['temp_max'], body['temp_min']), (31.2, None, None))

    def test_requires_both_coordinates_or_a_grow(self):
        for params in ({}, {'lat': 1}, {'lon': 2}, {'lat': 91, 'lon': 0}, {'lat': 0, 'lon': 181}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get('/weather', params=params).status_code, 422)
        both = self.client.get('/weather', params={'lat': 1, 'lon': 2, 'grow_id': str(self.fixture.grow)})
        self.assertEqual(both.status_code, 422)
        self.open_meteo.assert_not_awaited()

    def test_upstream_timeout_and_error_become_504_and_502(self):
        for error, expected in ((httpx.ReadTimeout('lento'), 504), (httpx.ConnectError('fora'), 502)):
            with self.subTest(expected=expected), patch.object(weather, '_open_meteo', new=REAL_OPEN_METEO), \
                    patch('httpx.AsyncClient.get', new=AsyncMock(side_effect=error)):
                self.assertEqual(self.client.get('/weather', params={'lat': 3, 'lon': 4}).status_code, expected)
        self.assertEqual(weather._cache, {})  # erro não fica em cache


class WeatherByGrowTests(WeatherTestCase):
    def url(self, grow_id=None):
        return f'/weather?grow_id={grow_id or self.fixture.grow}'

    def test_uses_saved_location_of_own_grow(self):
        self.place(lat=-22.91, lon=-47.06, location_label='Campinas, SP')
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['temp_max'], 32.5)
        self.assertIsNone(response.json()['weather_tip'])  # sem leitura de dentro, sem dica
        params = self.open_meteo.await_args.args[1]
        self.assertEqual((params['latitude'], params['longitude']), (-22.91, -47.06))

    def test_grow_of_another_account_is_404_even_with_location(self):
        self.place(self.fixture.other_grow, lat=10.0, lon=20.0)
        self.assertEqual(self.client.get(self.url(self.fixture.other_grow)).status_code, 404)
        self.assertEqual(self.client.get(self.url(uuid.uuid4())).status_code, 404)
        self.open_meteo.assert_not_awaited()

    def test_grow_without_location_is_409(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 409)
        self.assertIn('Defina o local', response.text)
        self.open_meteo.assert_not_awaited()

    def test_grow_id_requires_login_but_coordinates_stay_public(self):
        app = FastAPI()
        app.include_router(weather.router)
        app.dependency_overrides[get_db] = lambda: support.AsyncSessionAdapter(self.session)
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get(self.url()).status_code, 401)
            # o app antigo manda um JWT qualquer junto com lat/lon: continua ignorado
            public = anonymous.get('/weather', params={'lat': 1, 'lon': 2}, headers={'Authorization': 'Bearer vencido'})
            self.assertEqual(public.status_code, 200, public.text)

    def add_reading(self, temperature, humidity, minutes_ago=13):
        observed = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        reading = EnvironmentReading(grow_id=self.fixture.grow, user_id=self.fixture.owner, observed_at=observed,
                                     temperature_c=temperature, humidity_rh=humidity, kind='manual',
                                     created_at=observed, updated_at=observed)
        self.session.add(reading)
        self.session.commit()
        return reading

    def test_tip_from_rules_with_weather_reading_and_target_sources(self):
        self.place(lat=-22.91, lon=-47.06, location_label='Campinas, SP', target_humidity_rh=55)
        # fora 31,2 °C e 45 % (≈41 % a 33 °C), dentro 33 °C e 70 %, faixa 55 %: R1
        reading = self.add_reading(33.0, 70.0)
        tip = self.client.get(self.url()).json()['weather_tip']
        self.assertEqual((tip['rule_id'], tip['severity'], tip['title']), ('R1', 'info', 'Lá fora está bem mais seco'))
        self.assertEqual(tip['body'], 'Fora: 45 % a 31,2 °C, o que dá cerca de 41 % na temperatura de dentro (33 °C). '
                                      'Em Ambiente de teste: 70 %, acima da sua faixa de 55 %. '
                                      'Renovar o ar pela exaustão tende a baixar a umidade dentro.')
        self.assertTrue(tip['cta_prompt'].startswith('Lá fora está bem mais seco.'))
        outdoor, indoor, target = tip['sources']
        self.assertEqual((outdoor['kind'], outdoor['id'], outdoor['title'], outdoor['grow_id']),
                         ('weather', 'open-meteo:-22.91,-47.06', 'Open-Meteo · Campinas, SP', str(self.fixture.grow)))
        self.assertEqual((indoor['kind'], indoor['id'], indoor['title'], indoor['scope']),
                         ('environment_reading', str(reading.id), 'Registro do ambiente · 33 °C · 70%', 'focus'))
        self.assertEqual((target['kind'], target['title']), ('target', 'Faixa definida · 55%'))

    def test_phase_comes_from_active_plants(self):
        # fora e dentro a 25 °C: 72 % lá fora dispara a R2 só na floração (limite 70 %, não 75 %)
        self.open_meteo.return_value = {**FORECAST, 'current': {**FORECAST['current'], 'temperature_2m': 25.0,
                                                                'relative_humidity_2m': 72}}
        self.place(lat=1.0, lon=2.0)
        self.add_reading(25.0, 55.0)
        self.assertIsNone(self.client.get(self.url()).json()['weather_tip'])  # vegetativa: R2 só a partir de 75 %
        plant = self.session.get(Plant, self.fixture.plant)
        plant.current_phase = 'flower'
        self.session.commit()
        weather._cache.clear()
        tip = self.client.get(self.url()).json()['weather_tip']
        self.assertEqual(tip['rule_id'], 'R2')
        self.assertIn('Na floração, puxar', tip['body'])
        self.assertEqual((tip['sources'][-1]['kind'], tip['sources'][-1]['plant_id']), ('plant', str(self.fixture.plant)))

    def test_old_reading_gives_no_tip(self):
        self.place(lat=1.0, lon=2.0, target_humidity_rh=55)
        self.add_reading(25.0, 70.0, minutes_ago=6 * 60 + 5)
        self.assertIsNone(self.client.get(self.url()).json()['weather_tip'])

    def test_outdoor_grow_has_no_tip(self):
        self.place(lat=1.0, lon=2.0, grow_type='outdoor', target_humidity_rh=55)
        self.add_reading(25.0, 70.0)
        rule = unittest.mock.Mock()
        with patch.object(bob_weather, 'WEATHER_TIP_RULES', [rule]):
            body = self.client.get(self.url()).json()
        self.assertIsNone(body['weather_tip'])
        rule.assert_not_called()


class PlacesTests(WeatherTestCase):
    def test_search_returns_rounded_places_with_short_label(self):
        self.open_meteo.return_value = GEOCODING
        response = self.client.get('/weather/places', params={'q': '  Campinas '})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [
            {'name': 'Campinas', 'admin1': 'São Paulo', 'country': 'Brasil', 'lat': -22.91, 'lon': -47.06,
             'label': 'Campinas, SP'},
            {'name': 'Campinas do Sul', 'admin1': 'Rio Grande do Sul', 'country': 'Brasil', 'lat': -27.72,
             'lon': -52.62, 'label': 'Campinas do Sul, RS'},
            {'name': 'Campina', 'admin1': 'Prahova', 'country': 'Romênia', 'lat': 45.08, 'lon': 25.73,
             'label': 'Campina, Prahova'},
        ])
        url, params = self.open_meteo.await_args.args[:2]
        self.assertEqual((url, params['name'], params['language']), (weather.GEOCODING_URL, 'Campinas', 'pt'))
        self.client.get('/weather/places', params={'q': 'campinas'})  # cache por texto normalizado
        self.assertEqual(self.open_meteo.await_count, 1)

    def test_no_results_is_empty_list(self):
        self.open_meteo.return_value = {'generationtime_ms': 0.5}
        self.assertEqual(self.client.get('/weather/places', params={'q': 'xyzxyz'}).json(), [])

    def test_query_size_is_limited(self):
        for q in ('a', '  a  ', 'x' * 81):
            with self.subTest(q=q):
                self.assertEqual(self.client.get('/weather/places', params={'q': q}).status_code, 422)
        self.open_meteo.assert_not_awaited()

    def test_requires_login(self):
        app = FastAPI()
        app.include_router(weather.router)
        with TestClient(app) as anonymous:
            self.assertIn(anonymous.get('/weather/places', params={'q': 'Campinas'}).status_code, (401, 403))
        self.open_meteo.assert_not_awaited()


class GrowLocationTests(WeatherTestCase):
    def patch_grow(self, body, grow_id=None):
        return self.client.patch(f'/grows/{grow_id or self.fixture.grow}', json=body)

    def test_patch_rounds_coordinates_and_cleans_label(self):
        response = self.patch_grow({'lat': -22.905561, 'lon': -47.060837, 'location_label': '  Campinas,   SP '})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['lat'], body['lon'], body['location_label']), (-22.91, -47.06, 'Campinas, SP'))
        grow = self.grow()
        self.assertEqual((grow.lat, grow.lon, grow.location_label), (-22.91, -47.06, 'Campinas, SP'))
        listed = self.client.get('/grows').json()
        mine = next(g for g in listed if g['id'] == str(self.fixture.grow))
        self.assertEqual((mine['lat'], mine['lon'], mine['location_label']), (-22.91, -47.06, 'Campinas, SP'))
        self.assertEqual(self.client.get(f'/grows/{self.fixture.grow}').json()['location_label'], 'Campinas, SP')

    def test_create_also_rounds(self):
        self.enterContext(patch.object(grows, 'check_grow_limit'))  # o plano grátis já tem o ambiente da fixture
        response = self.client.post('/grows', json={'name': 'Varanda', 'lat': 10.123456, 'lon': -20.987654})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual((response.json()['lat'], response.json()['lon']), (10.12, -20.99))

    def test_invalid_coordinates_are_rejected_and_nothing_changes(self):
        self.place(lat=1.0, lon=2.0, location_label='Antes')
        for body in ({'lat': 90.5, 'lon': 0}, {'lat': 0, 'lon': -180.01}, {'lat': 5},
                     {'lat': None, 'lon': 3}, {'location_label': 'x' * 121}):
            with self.subTest(body=body):
                self.assertEqual(self.patch_grow(body).status_code, 422)
        grow = self.grow()
        self.assertEqual((grow.lat, grow.lon, grow.location_label), (1.0, 2.0, 'Antes'))

    def test_clearing_coordinates_clears_label_and_label_alone_does_not_touch_them(self):
        self.place(lat=1.0, lon=2.0, location_label='Antes')
        self.assertEqual(self.patch_grow({'location_label': 'Sala de casa'}).json()['lat'], 1.0)
        body = self.patch_grow({'lat': None, 'lon': None}).json()
        self.assertEqual((body['lat'], body['lon'], body['location_label']), (None, None, None))

    def test_cannot_set_location_on_another_account_grow(self):
        self.assertEqual(self.patch_grow({'lat': 1, 'lon': 2}, self.fixture.other_grow).status_code, 404)
        self.assertIsNone(self.grow(self.fixture.other_grow).lat)


if __name__ == '__main__':
    unittest.main()
