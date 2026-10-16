import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import test_private_access as support
from app.models.base import Base
from app.models.event import GrowEvent
from app.models.sensor import SensorDevice, SensorReading
from app.services.grow_env import latest_grow_env, latest_grow_measurements
from app.api.v1.plants import _compute_plant_summary
from app.models.plant import Plant


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PrivateAccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.session = self.fixture.session
        self.db = support.AsyncSessionAdapter(self.session)
        Base.metadata.create_all(self.fixture.engine, tables=[SensorDevice.__table__, SensorReading.__table__])
        self.now = datetime.now(timezone.utc)
        self.sensor = SensorDevice(id=uuid.uuid4(), name='Teste', esp32_mac='00:00:00:00:00:01', grow_id=self.fixture.grow)
        self.session.add(self.sensor)
        self.session.commit()

    def sensor_reading(self, hours, **values):
        reading = SensorReading(device_id=self.sensor.id, recorded_at=self.now - timedelta(hours=hours), **values)
        self.session.add(reading)
        self.session.commit()
        return reading

    def manual(self, hours, **values):
        event = GrowEvent(plant_id=self.fixture.plant, event_type='environment', event_date=self.now - timedelta(hours=hours), **values)
        self.session.add(event)
        self.session.commit()
        return event

    def readings(self):
        return asyncio.run(latest_grow_measurements(self.db, self.fixture.grow, now=self.now))

    def test_mixed_sources_keep_separate_dates(self):
        sensor = self.sensor_reading(2, temp_air=27)
        manual = self.manual(3, humidity_rh=58)
        result = self.readings()
        self.assertEqual(result['temperature'].source, 'sensor')
        self.assertEqual(result['temperature'].source_id, sensor.id)
        self.assertEqual(result['temperature'].observed_at, sensor.recorded_at)
        self.assertEqual(result['humidity'].source, 'manual')
        self.assertEqual(result['humidity'].source_id, manual.id)
        self.assertEqual(result['humidity'].observed_at, manual.event_date)
        snapshot = asyncio.run(latest_grow_env(self.db, self.fixture.grow))
        self.assertIsNone(snapshot.recorded_at)
        self.assertEqual(snapshot.temp_c, 27)
        self.assertEqual(snapshot.humidity_rh, 58)

    def test_newer_manual_reading_wins_over_old_sensor(self):
        self.sensor_reading(5, temp_air=25, humidity_air=60)
        event = self.manual(1, temperature_c=28)
        result = self.readings()
        self.assertEqual(result['temperature'].value, 28)
        self.assertEqual(result['temperature'].source_id, event.id)
        self.assertEqual(result['humidity'].value, 60)

    def test_partial_latest_reading_does_not_erase_other_indicators(self):
        self.sensor_reading(5, temp_air=25, humidity_air=60, lux=120, vpd_kpa=1.2)
        self.sensor_reading(1, temp_air=26)
        result = self.readings()
        self.assertEqual([result[key].value for key in ['temperature', 'humidity', 'light', 'vpd']], [26, 60, 120, 1.2])

    def test_future_and_non_finite_values_do_not_replace_valid_readings(self):
        self.sensor_reading(5, temp_air=0)
        self.sensor_reading(1, temp_air=float('inf'))
        self.manual(-1, temperature_c=99)
        self.assertEqual(self.readings()['temperature'].value, 0)

    def test_missing_values_have_no_fabricated_source(self):
        for reading in self.readings().values():
            self.assertIsNone(reading.value)
            self.assertIsNone(reading.source)
            self.assertIsNone(reading.observed_at)

    def test_other_grows_do_not_supply_measurements(self):
        self.sensor.grow_id = self.fixture.other_grow
        # Conflicting legacy links must not override the direct grow assignment.
        self.sensor.plant_id = self.fixture.plant
        self.session.commit()
        self.sensor_reading(1, temp_air=35)
        self.assertIsNone(self.readings()['temperature'].value)
        self.sensor.grow_id = None
        self.session.commit()
        self.assertEqual(self.readings()['temperature'].value, 35)

    def test_sensor_wins_tie_and_summary_uses_same_resolver(self):
        self.manual(2, temperature_c=26)
        self.sensor_reading(2, temp_air=27)
        self.assertEqual(self.readings()['temperature'].value, 27)
        plant = self.session.get(Plant, self.fixture.plant)
        summary = asyncio.run(_compute_plant_summary(self.db, plant))
        self.assertEqual(summary.temperature.value, 27)
        self.assertEqual(summary.last_temperature_c, summary.temperature.value)
        self.assertEqual(summary.last_temperature_at, summary.temperature.observed_at)
