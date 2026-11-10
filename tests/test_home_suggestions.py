"""Início v3 (UX item 20): sugestões do Bobby no daily-brief e próxima ação/foto em /plants/summaries."""
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_sem_api as sem
from app.api.v1 import plants
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.strain import Strain
from app.models.user import User
from app.services import home_suggestions
from app.services.bob_rules import BobStep
from app.services.daily_brief import DailyBrief
from app.services.home_suggestions import build_suggestions

run = sem.run
BRAZIL = timezone(timedelta(hours=-3))


class HomeBase(sem.Base_):
    def setUp(self):
        super().setUp()
        Base.metadata.create_all(self.engine, tables=[Strain.__table__])
        self.app = FastAPI()
        self.app.include_router(plants.router, prefix="/plants")
        self.app.dependency_overrides[sem.get_current_user] = lambda: self.current
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.api = TestClient(self.app)
        self.addCleanup(self.api.close)
        self.today = datetime.now(BRAZIL).date()
        self.brief = self.enterContext(patch(
            "app.services.daily_brief.generate_daily_brief",
            new=AsyncMock(return_value=DailyBrief(title="Bob", body="Resumo", urgent_count=0)),
        ))

    def steps(self, *steps):
        """Itens de atenção fixos para a planta da dona (o resto do motor fica real)."""
        async def fake(plant, events, today, **kwargs):
            return [s for s in steps if s.plant_id == plant.id]
        return self.enterContext(patch.object(home_suggestions, "compute_bob_steps", new=fake))

    def step(self, tier, action, message):
        return BobStep(tier=tier, action=action, message=message, plant_id=self.plant.id, reason="teste",
                       evidence=[{"kind": "event", "id": "e1", "observed_at": None, "source": "manual"}])

    def suggestions(self):
        response = self.api.get("/plants/daily-brief")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["suggestions"]

    def reading(self, hours_ago, temperature=26.5, humidity=60):
        self.session.add(EnvironmentReading(
            id=uuid.uuid4(), grow_id=self.grow.id, user_id=self.owner.id,
            observed_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            temperature_c=temperature, humidity_rh=humidity,
        ))
        self.session.commit()


class SuggestionTests(HomeBase):
    def test_no_plants_means_no_suggestions(self):
        self.current = User(id=uuid.uuid4(), email="nova@example.test", username="nova", plan="free")
        self.session.add(self.current)
        self.session.commit()
        self.assertEqual(self.suggestions(), [])

    def test_phase_and_day_without_sensors_cite_no_measurement(self):
        self.plant.flip_date = self.today - timedelta(days=21)
        self.session.commit()
        self.steps()
        items = self.suggestions()
        self.assertEqual(len(items), 1)
        phase = items[0]
        self.assertEqual(phase["label"], "Aurora · Floração, dia 22")
        self.assertIn("Aurora está em floração, dia 22 da fase", phase["prompt"])
        self.assertEqual((phase["plant_id"], phase["grow_id"]), (str(self.plant.id), str(self.grow.id)))
        self.assertEqual(phase["basis"]["kind"], "phase")
        self.assertEqual(phase["basis"]["evidence"][0]["kind"], "plant")
        self.assertFalse(any("°C" in s["prompt"] or "%" in s["prompt"] for s in items))

    def test_phase_without_registered_start_has_no_day(self):
        self.plant.current_phase = "veg"
        self.session.commit()
        self.steps()
        self.assertEqual(self.suggestions()[0]["label"], "Aurora · Vegetativo")
        self.plant.current_phase = "unknown"
        self.session.commit()
        self.assertEqual(self.suggestions(), [])

    def test_phase_day_from_the_fase_record(self):
        self.plant.current_phase = "veg"
        # meio-dia de Brasília, 4 dias atrás: o dia não muda com a hora em que o teste roda
        noon = datetime.combine(self.today - timedelta(days=4), datetime.min.time(), tzinfo=BRAZIL) + timedelta(hours=12)
        self.session.add(GrowEvent(id=uuid.uuid4(), plant_id=self.plant.id, event_type="veg",
                                   event_date=noon.astimezone(timezone.utc)))
        self.session.commit()
        self.steps()
        item = self.suggestions()[0]
        self.assertEqual(item["label"], "Aurora · Vegetativo, dia 5")
        self.assertEqual(item["basis"]["evidence"][0]["kind"], "event")

    def test_recent_reading_becomes_a_suggestion_and_old_one_does_not(self):
        self.steps()
        self.reading(hours_ago=30)
        self.assertFalse(any(s["basis"]["kind"] == "reading" for s in self.suggestions()))
        self.reading(hours_ago=2)
        reading = next(s for s in self.suggestions() if s["basis"]["kind"] == "reading")
        self.assertEqual(reading["label"], "Clima · Sala")
        self.assertIn("26,5 °C e 60% de umidade", reading["prompt"])
        self.assertEqual({e["kind"] for e in reading["basis"]["evidence"]}, {"environment_reading"})

    def test_attention_items_come_first_by_severity_and_the_list_stops_at_three(self):
        self.plant.flip_date = self.today - timedelta(days=10)
        self.session.commit()
        self.reading(hours_ago=1)
        self.steps(self.step("attention", "watering", "A última rega foi há 6 dias."),
                   self.step("critical", "temperature", "Temperatura em 32 °C."),
                   self.step("info", "tricomas", "Comece a olhar os tricomas."))
        items = self.suggestions()
        self.assertEqual([s["label"] for s in items], ["Temperatura · Aurora", "Rega · Aurora", "Aurora · Floração, dia 11"])
        self.assertEqual(items[0]["prompt"], "Sobre Aurora: Temperatura em 32 °C. O que você recomenda fazer agora?")
        self.assertEqual(items[0]["basis"], {"kind": "attention", "text": "Temperatura em 32 °C.", "tier": "critical",
                                             "evidence": [{"kind": "event", "id": "e1", "observed_at": None, "source": "manual"}]})

    def test_suggestions_only_use_the_account_own_plants(self):
        self.steps()
        self.other_plant.current_phase = "flower"
        self.other_plant.flip_date = self.today
        self.session.commit()
        self.assertTrue(all(s["plant_id"] == str(self.plant.id) for s in self.suggestions()))

    def test_brief_survives_a_suggestion_failure(self):
        self.enterContext(patch.object(plants, "logger"))
        with patch("app.services.home_suggestions.build_suggestions", new=AsyncMock(side_effect=RuntimeError("x"))):
            response = self.api.get("/plants/daily-brief")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["body"], response.json()["suggestions"]), ("Resumo", []))

    def test_service_returns_empty_for_unknown_user(self):
        self.assertEqual(run(build_suggestions(self.db, uuid.uuid4())), [])


class SummaryTests(HomeBase):
    def summary(self):
        response = self.api.get("/plants/summaries")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()[str(self.plant.id)]

    def photo_event(self, days_ago, keys):
        event = GrowEvent(id=uuid.uuid4(), plant_id=self.plant.id, event_type="note", photo_keys=keys,
                          event_date=datetime.now(timezone.utc) - timedelta(days=days_ago))
        self.session.add(event)
        self.session.commit()
        return event

    def test_latest_own_photo_with_the_strain_image_as_fallback(self):
        self.steps()
        self.session.add(Strain(id=uuid.uuid4(), name="Aurora", slug="aurora", image_url="https://img.example/aurora.jpg",
                                source_file="strains/aurora.md"))
        self.session.commit()
        self.enterContext(patch.object(plants, "presign_download", side_effect=lambda bucket, key: f"https://s3/{key}"))
        body = self.summary()
        self.assertIsNone(body["latest_photo_url"])
        self.assertEqual(body["cover_image_url"], body["strain_image_url"])
        self.assertIsNotNone(body["strain_image_url"])

        older = self.photo_event(5, [f"{self.owner.id}/antiga.jpg"])
        newer = self.photo_event(1, [f"{self.other.id}/alheia.jpg", f"{self.owner.id}/nova.jpg"])
        self.photo_event(0, [f"{self.other.id}/so-alheia.jpg"])  # chave de outra conta nunca aparece
        body = self.summary()
        self.assertEqual(body["latest_photo_url"], f"https://s3/{self.owner.id}/nova.jpg")
        self.assertEqual(body["cover_image_url"], body["latest_photo_url"])
        self.assertEqual(body["latest_photo_event_id"], str(newer.id))
        self.assertNotEqual(body["latest_photo_event_id"], str(older.id))

    def test_next_action_last_event_and_phase_day(self):
        self.plant.flip_date = self.today - timedelta(days=2)
        self.session.commit()
        self.steps(self.step("info", "tricomas", "Comece a olhar os tricomas."))
        body = self.summary()
        self.assertIsNone(body["next_action"])  # dica informativa não é pendência
        self.assertIsNone(body["last_event"])
        self.assertEqual(body["phase_day"], 3)

        self.steps(self.step("attention", "watering", "A última rega foi há 6 dias."),
                   self.step("anomaly", "humidity", "Umidade em 78%."))
        self.session.add(GrowEvent(id=uuid.uuid4(), plant_id=self.plant.id, event_type="watering",
                                   event_date=datetime.now(timezone.utc) - timedelta(days=6)))
        self.session.commit()
        body = self.summary()
        self.assertEqual({k: body["next_action"][k] for k in ("action", "tier", "message")},
                         {"action": "humidity", "tier": "anomaly", "message": "Umidade em 78%."})
        self.assertEqual((body["last_event"]["event_type"], body["last_event"]["label"]), ("watering", "Rega"))


if __name__ == "__main__":
    unittest.main()
