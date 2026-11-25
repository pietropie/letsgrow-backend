"""Sensores do ambiente (tipo, PPM, faixas, painel, histórico) e relés do Hub pelo app.

Contratos: UX item 28/28b/28c, letsgrow-firmware/hub/RELAYS.md e docs/API-SENSORES-RELES.md.
O broker é simulado: nada conecta a um Mosquitto de verdade.
"""
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import test_private_access as support  # noqa: F401  registra JSONB -> JSON no SQLite
from app.api.v1 import grows, iot, sensors
from app.config import settings
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.grow import Grow, GrowIdealRange
from app.models.notification import NotificationPreference
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.sensor import (
    HubClaimCode, HubRelay, RelayCommand, RelayEvent, SensorDevice, SensorModel, SensorReading,
)
from app.models.user import User
from app.services import ideal_ranges, mqtt_broker, relays, sensor_catalog
from app.services.auth import get_current_user

TABLES = [User.__table__, Grow.__table__, Pot.__table__, Plant.__table__, SensorDevice.__table__,
          SensorReading.__table__, HubClaimCode.__table__, HubRelay.__table__, RelayCommand.__table__,
          RelayEvent.__table__, SensorModel.__table__, GrowIdealRange.__table__, EnvironmentReading.__table__,
          NotificationPreference.__table__, GrowEvent.__table__]
MAC = "209BA98A3EE0"


def _scalar(db, stmt):
    async def go():
        return (await db.execute(stmt)).scalar_one_or_none()
    return go()


class PureLogicTests(unittest.TestCase):
    def test_ppm_and_kind(self):
        self.assertEqual(sensor_catalog.ppm_from_ec(1.64), 820)
        self.assertIsNone(sensor_catalog.ppm_from_ec(None))
        self.assertEqual(sensor_catalog.kind_from_config("satellite", {"module_type": "solo"}), "soil")
        self.assertEqual(sensor_catalog.kind_from_config("satellite", {"module_type": "ambiente"}), "air")
        self.assertEqual(sensor_catalog.kind_from_config("satellite", {"module_type": "ppfd"}), "light")
        self.assertEqual(sensor_catalog.kind_from_config("satellite", {"module_type": "nutricao"}), "solution")
        self.assertEqual(sensor_catalog.kind_from_config("hub", {}), "hub")
        self.assertIsNone(sensor_catalog.kind_from_config("satellite", {}))
        for kind in sensor_catalog.KINDS.values():  # nenhum texto de tela cita placa ou MAC
            for text in (kind.label, kind.tag, kind.description, kind.default_name):
                self.assertNotRegex(text.lower(), r"esp32|mac|placa")

    def test_ranges_by_phase_custom_and_state(self):
        flower = ideal_ranges.effective("humidity_air", "flower", {})
        self.assertEqual((flower.min, flower.max, flower.source, flower.needs_review), (45, 60, "phase", False))
        self.assertTrue(ideal_ranges.effective("lux", "veg", {}).needs_review)
        custom = ideal_ranges.effective("humidity_air", "flower", {"humidity_air": (50, 65)})
        self.assertEqual((custom.min, custom.max, custom.source), (50, 65, "custom"))
        ec = ideal_ranges.effective("ec_ms_cm", "veg", {"ppm": (700, 1000)})
        self.assertEqual((ec.min, ec.max), (1.4, 2.0))
        self.assertIsNone(ideal_ranges.effective("temp_air", "drying", {}))
        band = ideal_ranges.effective("temp_air", "veg", {})  # 24–28: borda de 0,4
        self.assertEqual(ideal_ranges.range_state(26, band), "ok")
        self.assertEqual(ideal_ranges.range_state(27.8, band), "near")
        self.assertEqual(ideal_ranges.range_state(29, band), "out")
        self.assertEqual(ideal_ranges.range_state(None, band), "none")
        self.assertEqual(ideal_ranges.grow_phase(["veg", "flower", "flower", "unknown"]), ("flower", "plants"))
        self.assertEqual(ideal_ranges.grow_phase(["veg", "flower"]), ("flower", "plants"))  # empate: a mais avançada
        self.assertEqual(ideal_ranges.grow_phase(["unknown"]), ("unknown", "assumed"))
        self.assertEqual(ideal_ranges.grow_phase([]), ("unknown", "none"))

    def test_posix_tz_and_fans(self):
        self.assertEqual(relays.posix_tz("America/Sao_Paulo"), "<-03>3")
        self.assertEqual(relays.posix_tz("UTC"), "<+00>0")
        self.assertEqual(relays.posix_tz("Asia/Kolkata"), "<+0530>-5:30")
        self.assertEqual(relays.posix_tz("Nada/Nada"), "<-03>3")
        fans = relays.normalize_fans([{"id": "abcd1234", "watts": 25}, {"watts": 40}, {"id": "abcd1234"}])
        ids = [f["id"] for f in fans]
        self.assertEqual(ids[0], "abcd1234")
        self.assertEqual(len(set(ids)), 3)


class SensorsRelaysTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.owner, self.other = uuid.uuid4(), uuid.uuid4()
        self.grow, self.other_grow = uuid.uuid4(), uuid.uuid4()
        self.hub_id, self.plant_id = uuid.uuid4(), uuid.uuid4()

        @asynccontextmanager
        async def lifespan(_app):
            async with self.engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
            async with self.sessions() as db:
                db.add_all([User(id=self.owner, email="a@example.test", username="a"),
                            User(id=self.other, email="b@example.test", username="b")])
                await db.flush()
                db.add_all([
                    Grow(id=self.grow, user_id=self.owner, name="Sala", start_date=date.today(),
                         light_type="led_quantum_board", lighting_watts=720,
                         photoperiod_on_at=time(6, 0), photoperiod_light_minutes=18 * 60, photoperiod_hours="18/6",
                         exhaust_type="inline_fan", exhaust_cfm=150,
                         fans=[{"id": "fan00001", "watts": 25, "qty": 2}, {"id": "fan00002", "watts": 40}]),
                    Grow(id=self.other_grow, user_id=self.other, name="Alheio", start_date=date.today()),
                ])
                await db.flush()
                db.add(Plant(id=self.plant_id, user_id=self.owner, grow_id=self.grow, strain_name="Aurora",
                             pot_label="Vaso 1", current_phase="flower"))
                db.add(SensorDevice(id=self.hub_id, grow_id=self.grow, name="Hub 3EE0", esp32_mac=MAC,
                                    module_type="hub", is_paired=True, sensors_config={},
                                    is_online=True, last_seen_at=datetime.now(timezone.utc)))
                await db.commit()
            yield
            await self.engine.dispose()

        async def database():
            async with self.sessions() as db:
                yield db

        self.user = SimpleNamespace(id=self.owner, plan="cultivador")
        app = FastAPI(lifespan=lifespan)
        app.include_router(iot.router, prefix="/iot")
        app.include_router(grows.router, prefix="/grows")
        app.include_router(sensors.router, prefix="/sensors")
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.enterContext(patch.object(settings, "RATE_LIMIT_ENABLED", False))
        self.config = self.enterContext(patch.object(mqtt_broker, "publish_relay_config", AsyncMock()))
        self.clear = self.enterContext(patch.object(mqtt_broker, "clear_relay_config", AsyncMock()))
        self.publish = self.enterContext(patch.object(mqtt_broker, "publish_hub_command", AsyncMock()))
        self.client = self.enterContext(TestClient(app))

    # -- helpers -------------------------------------------------------------

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                result = await fn(db)
                await db.commit()
                return result
        return self.client.portal.call(go)

    def hub(self):
        return self.run_db(lambda db: db.get(SensorDevice, self.hub_id))

    def put(self, n, **body):
        return self.client.put(f"/iot/devices/{self.hub_id}/relays/{n}", json=body)

    def hub_message(self, handler, payload):
        async def go(db):
            hub = await db.get(SensorDevice, self.hub_id)
            return await handler(db, hub, payload)
        return self.run_db(go)

    def add_satellite(self, kind_config, plant=False, **fields):
        sid = uuid.uuid4()

        async def go(db):
            db.add(SensorDevice(id=sid, grow_id=self.grow, plant_id=self.plant_id if plant else None,
                                name=fields.pop("name", "Solo vaso 1"), esp32_mac=uuid.uuid4().hex[:12].upper(),
                                module_type="satellite", hub_mac=MAC, is_paired=True,
                                sensors_config={"module_type": kind_config}, is_online=True,
                                last_seen_at=datetime.now(timezone.utc)))
        self.run_db(go)
        return sid

    def add_reading(self, device_id, minutes_ago=1, **values):
        async def go(db):
            db.add(SensorReading(device_id=device_id,
                                 recorded_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **values))
        self.run_db(go)

    # -- relés: configuração ---------------------------------------------------

    def test_get_relays_creates_four_free_positions_and_lists_equipment(self):
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertEqual([r["n"] for r in body["relays"]], [1, 2, 3, 4])
        self.assertTrue(all(r["equip_kind"] is None and r["mode"] == "manual" for r in body["relays"]))
        self.assertEqual((body["online"], body["synced"], body["mains_voltage"], body["total_limit_w"]),
                         (True, True, 127, 1200))
        names = {e["key"]: e["name"] for e in body["equipment"]}
        self.assertEqual(names, {"light": "LED quantum board 720 W", "exhaust": "Exaustor em linha 150 CFM",
                                 "fan:fan00001": "Ventiladores 25 W (2)", "fan:fan00002": "Ventilador 40 W"})
        other = self.client.get(f"/iot/devices/{uuid.uuid4()}/relays")
        self.assertEqual(other.status_code, 404)

    def test_bind_light_schedule_publishes_retained_config(self):
        response = self.put(1, equip_kind="light", mode="schedule")
        self.assertEqual(response.status_code, 200, response.text)
        grow_id, mac, payload = self.config.await_args.args
        self.assertEqual((grow_id, mac), (self.grow, MAC))
        self.assertEqual(payload["rev"], 1)
        self.assertEqual(payload["tz"], "<-03>3")
        first = payload["relays"][0]
        self.assertEqual(first, {"n": 1, "equip": "light", "equip_ref": "light", "mode": "schedule",
                                 "schedule": {"on": "06:00", "off": "00:00"}})
        self.assertEqual(payload["relays"][1], {"n": 2, "equip": "none", "equip_ref": "", "mode": "manual"})
        body = response.json()
        relay = body["relays"][0]
        self.assertEqual((relay["equip_name"], relay["state"], relay["source"], relay["confirmed"]),
                         ("LED quantum board 720 W", 0, "rebind", False))
        self.assertFalse(body["synced"])  # até o config_ack
        self.assertIsNotNone(relay["schedule_lit"])

        self.hub_message(relays.handle_config_ack, {"rev": 1, "ok": True})
        self.assertTrue(self.client.get(f"/iot/devices/{self.hub_id}/relays").json()["synced"])

    def test_config_ack_refused_keeps_error(self):
        self.put(2, equip_kind="exhaust")
        self.hub_message(relays.handle_config_ack, {"rev": 1, "ok": False, "err": "config_invalid", "detail": "schedule"})
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertEqual((body["synced"], body["sync_error"]), (False, "schedule"))

    def test_equipment_in_use_and_move(self):
        self.put(1, equip_kind="fan", equip_key="fan:fan00001")
        conflict = self.put(4, equip_kind="fan", equip_key="fan:fan00001")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"], "equipment_in_use")
        self.assertEqual(conflict.json()["n"], 1)
        moved = self.put(4, equip_kind="fan", equip_key="fan:fan00001", move=True)
        self.assertEqual(moved.status_code, 200, moved.text)
        relays_ = {r["n"]: r["equip_key"] for r in moved.json()["relays"]}
        self.assertEqual((relays_[1], relays_[4]), (None, "fan:fan00001"))
        self.assertEqual(self.config.await_args.args[2]["rev"], 2)

    def test_validation_errors(self):
        self.assertEqual(self.put(1, equip_kind="fan", equip_key="fan:nada").json()["detail"], "equipment_not_found")
        self.assertEqual(self.put(1, mode="schedule").json()["detail"], "relay_free")
        self.put(1, equip_kind="light")
        self.assertEqual(self.put(1, mode="humidity").json()["detail"], "mode_not_allowed")
        self.put(2, equip_kind="exhaust")
        self.assertEqual(self.put(2, mode="humidity").json()["detail"], "humidity_required")
        self.assertEqual(self.put(2, mode="schedule").json()["detail"], "schedule_required")
        bad = self.put(2, humidity={"on_above": 60, "off_below": 70})
        self.assertEqual(bad.status_code, 422)
        ok = self.put(2, mode="humidity", humidity={"on_above": 70, "off_below": 60})
        self.assertEqual(ok.status_code, 200, ok.text)
        cfg = self.config.await_args.args[2]["relays"][1]
        self.assertEqual(cfg["humidity"], {"on_above": 70, "off_below": 60, "min_on_s": 60, "min_off_s": 60})

    def test_broker_down_rolls_back(self):
        self.config.side_effect = mqtt_broker.BrokerUnavailable("x")
        response = self.put(1, equip_kind="light")
        self.assertEqual((response.status_code, response.json()["detail"]), (503, "broker_unavailable"))
        self.config.side_effect = None
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertIsNone(body["relays"][0]["equip_key"])
        self.assertEqual(body["rev"], 0)

    def test_load_warnings_and_voltage(self):
        self.put(1, equip_kind="light")
        self.put(2, equip_kind="exhaust")
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertIn({"code": "missing_watts", "n": 2}, body["warnings"])
        self.client.patch("/grows/" + str(self.grow), json={"exhaust_watts": 600, "lighting_watts": 1100})
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertIn({"code": "over_output_limit", "n": 1}, body["warnings"])
        self.assertIn({"code": "over_total_limit", "n": None}, body["warnings"])
        patched = self.client.patch(f"/iot/devices/{self.hub_id}", json={"mains_voltage": 220})
        self.assertEqual(patched.json()["mains_voltage"], 220)
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertNotIn({"code": "over_total_limit", "n": None}, body["warnings"])  # 1700 W < 2000 W
        self.assertEqual(self.client.patch(f"/iot/devices/{self.hub_id}", json={"mains_voltage": 110}).status_code, 422)

    def test_removed_fan_unbinds_and_republishes(self):
        self.put(3, equip_kind="fan", equip_key="fan:fan00002")
        response = self.client.patch(f"/grows/{self.grow}", json={"fans": [{"id": "fan00001", "watts": 25, "qty": 2},
                                                                           {"watts": 60}]})
        self.assertEqual(response.status_code, 200, response.text)
        fans = response.json()["fans"]
        self.assertEqual(fans[0]["id"], "fan00001")
        self.assertTrue(fans[1]["id"])
        relay = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()["relays"][2]
        self.assertEqual((relay["equip_key"], relay["source"]), (None, "rebind"))
        self.assertEqual(self.config.await_args.args[2]["relays"][2]["equip_ref"], "")

    def test_photoperiod_change_republishes_light(self):
        self.put(1, equip_kind="light", mode="schedule")
        self.client.patch(f"/grows/{self.grow}", json={"photoperiod_on_at": "18:00", "photoperiod_light_minutes": 720})
        cfg = self.config.await_args.args[2]["relays"][0]
        self.assertEqual(cfg["schedule"], {"on": "18:00", "off": "06:00"})
        # 24/0: sempre ligada, 00:00–24:00 (o Hub não depende do relógio)
        self.client.patch(f"/grows/{self.grow}", json={"photoperiod_light_minutes": 1440})
        cfg = self.config.await_args.args[2]["relays"][0]
        self.assertEqual((cfg["mode"], cfg["schedule"]), ("schedule", {"on": "00:00", "off": "24:00"}))
        relay = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()["relays"][0]
        self.assertEqual((relay["schedule_lit"], relay["next_change_at"]), (True, None))

    # -- relés: comandos e mensagens do Hub ------------------------------------

    def test_command_flow_with_ack(self):
        self.assertEqual(self.client.post(f"/iot/devices/{self.hub_id}/relays/1/command", json={"state": 1}).json()["detail"],
                         "relay_free")
        self.put(1, equip_kind="light")
        accepted = self.client.post(f"/iot/devices/{self.hub_id}/relays/1/command", json={"state": 1})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        req_id = accepted.json()["req_id"]
        grow_id, mac, payload = self.publish.await_args.args
        self.assertEqual(payload, {"cmd": "relay_set", "req_id": req_id, "relay": 1, "state": 1, "duration_s": 0})
        self.assertEqual(self.client.get(f"/iot/relay-commands/{req_id}").json()["status"], "pending")

        ack = {"req_id": req_id, "ok": True, "relay": 1, "state": 1, "override": True, "err": None}
        self.hub_message(relays.handle_ack, ack)
        self.hub_message(relays.handle_ack, {**ack, "ok": False, "err": "bad_relay"})  # repetido: não muda
        done = self.client.get(f"/iot/relay-commands/{req_id}").json()
        self.assertEqual((done["status"], done["state"], done["override"]), ("confirmed", 1, True))
        relay = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()["relays"][0]
        self.assertEqual((relay["state"], relay["override"], relay["confirmed"]), (1, True, True))

        resume = self.client.post(f"/iot/devices/{self.hub_id}/relays/1/command", json={"action": "resume"})
        self.assertEqual(self.publish.await_args.args[2]["cmd"], "relay_resume")
        self.assertEqual(resume.status_code, 202)

    def test_command_timeout_and_offline(self):
        self.put(1, equip_kind="light")
        req_id = self.client.post(f"/iot/devices/{self.hub_id}/relays/1/command", json={"state": 0}).json()["req_id"]

        async def age(db):
            command = (await db.execute(select(RelayCommand))).scalar_one()
            command.created_at = datetime.now(timezone.utc) - timedelta(seconds=11)
        self.run_db(age)
        self.assertEqual(self.client.get(f"/iot/relay-commands/{req_id}").json()["status"], "timeout")

        self.hub_message(relays.handle_status, {"online": False, "serial": f"LG-{MAC}"})
        offline = self.client.post(f"/iot/devices/{self.hub_id}/relays/1/command", json={"state": 1})
        self.assertEqual((offline.status_code, offline.json()["detail"]), (409, "hub_offline"))
        self.user = SimpleNamespace(id=self.other, plan="cultivador")
        self.assertEqual(self.client.get(f"/iot/relay-commands/{req_id}").status_code, 404)

    def test_status_updates_clock_and_states(self):
        self.put(1, equip_kind="light")
        self.hub_message(relays.handle_config_ack, {"rev": 1, "ok": True})
        self.hub_message(relays.handle_status, {
            "online": True, "clock": "ntp", "cfg_rev": 1,
            "relays": [{"n": 1, "state": 1, "mode": "manual", "override": False, "equip": "light"}]})
        body = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()
        self.assertEqual((body["clock"], body["relays"][0]["state"], body["relays"][0]["confirmed"]), ("ntp", 1, True))

    def test_events_dedupe_by_seq_and_history(self):
        self.put(2, equip_kind="exhaust", mode="humidity", humidity={"on_above": 70, "off_below": 60})
        event = {"seq": 196612, "ts": int(datetime.now(timezone.utc).timestamp()), "up_s": 3605, "relay": 2, "state": 1, "source": "humidity",
                 "equip_ref": "exhaust", "hum": 71.4}
        self.assertTrue(self.hub_message(relays.handle_event, event))
        self.assertFalse(self.hub_message(relays.handle_event, event))  # o Hub reenviou
        no_clock = {"seq": 196613, "ts": 0, "up_s": 10, "relay": 0, "state": 0, "source": "no_clock"}
        self.assertTrue(self.hub_message(relays.handle_event, no_clock))
        boot = {"seq": 196614, "ts": 0, "up_s": 1, "relay": 0, "state": 0, "source": "boot"}
        self.assertTrue(self.hub_message(relays.handle_event, boot))

        stored = self.run_db(lambda db: _all(db, select(RelayEvent).order_by(RelayEvent.seq)))
        self.assertEqual([e.seq for e in stored], [196612, 196613, 196614])
        self.assertIsNone(stored[1].hub_ts)  # sem hora: vale o received_at
        notes = self.run_db(lambda db: _all(db, select(EnvironmentReading.notes).where(EnvironmentReading.kind == "relay")))
        self.assertEqual(len(notes), 2)  # boot não vai para o histórico
        self.assertIn("Exaustor (Exaustor em linha 150 CFM) ligado pela umidade (71 %) · relé 2", notes)
        self.assertTrue(any("sem hora" in n for n in notes))
        for note in notes:
            self.assertNotRegex(note.lower(), r"esp32|placa|mac\b")
        relay = self.client.get(f"/iot/devices/{self.hub_id}/relays").json()["relays"][1]
        self.assertEqual((relay["state"], relay["source"], relay["confirmed"]), (1, "humidity", True))

    def test_hub_moved_to_other_grow_detaches_relays(self):
        self.put(1, equip_kind="light")

        async def go(db):
            hub = await db.get(SensorDevice, self.hub_id)
            await relays.detach_hub(db, hub, self.grow)
        self.run_db(go)
        self.clear.assert_awaited_once_with(self.grow, MAC)
        rows = self.run_db(lambda db: _all(db, select(HubRelay)))
        self.assertEqual(rows, [])

    # -- sensores ----------------------------------------------------------------

    def test_device_response_kind_photo_and_discovery_name(self):
        sid = self.add_satellite("solo", plant=True)
        async def photo(db):
            db.add(SensorModel(key="solo", name="Satélite de solo", sensor_kind="soil",
                               photo_url="https://cdn.example.test/solo.png"))
        self.run_db(photo)
        listed = {d["id"]: d for d in self.client.get("/iot/devices").json()}
        sat = listed[str(sid)]
        self.assertEqual((sat["sensor_kind"], sat["kind_label"], sat["photo_url"]),
                         ("soil", "Solo", "https://cdn.example.test/solo.png"))
        hub = listed[str(self.hub_id)]
        self.assertEqual((hub["sensor_kind"], hub["photo_url"]), ("hub", None))

    def test_panel_cards_ranges_out_since_and_history(self):
        soil = self.add_satellite("solo", plant=True)
        air = self.add_satellite("ambiente", name="Ar da tenda")
        self.add_satellite("nutricao", name="Solução da rega")
        for minutes in (300, 200, 100):
            self.add_reading(soil, minutes_ago=minutes, soil_moisture_pct=50)
        self.add_reading(soil, minutes_ago=90, soil_moisture_pct=20)
        self.add_reading(soil, minutes_ago=2, soil_moisture_pct=21)
        self.add_reading(air, temp_air=25.4, humidity_air=59.5, vpd_kpa=1.2)
        self.add_reading(self.hub_id, ec_ms_cm=1.64, lux=40000)

        panel = self.client.get(f"/iot/grows/{self.grow}/sensors").json()
        # Fora da faixa: o solo e a luz medida pelo Hub (40 000 lx, abaixo da sugestão da floração)
        self.assertEqual((panel["phase"], panel["phase_basis"], panel["out_of_range_count"]), ("flower", "plants", 2))
        self.assertEqual(panel["hub"]["satellites_total"], 3)
        self.assertEqual({c["worst_state"] for c in panel["cards"][:2]}, {"out"})
        first = next(c for c in panel["cards"] if c["sensor_kind"] == "soil")
        self.assertEqual((first["sensor_kind"], first["worst_state"], first["placement"]["label"]),
                         ("soil", "out", "Vaso 1 · Aurora"))
        metric = first["metrics"][0]
        self.assertEqual((metric["value"], metric["range"]["min"], metric["state"]), (21, 35, "out"))
        since = datetime.fromisoformat(metric["out_of_range_since"])
        self.assertAlmostEqual((datetime.now(timezone.utc) - since).total_seconds() / 60, 90, delta=2)
        self.assertEqual((first["history"]["window"], len(first["history"]["buckets"])), ("3d", 18))

        kinds = {(c["source"], c["sensor_kind"]) for c in panel["cards"]}
        self.assertIn(("hub", "solution"), kinds)  # o Hub também mede
        self.assertIn(("hub", "light"), kinds)
        hub_solution = next(c for c in panel["cards"] if c["source"] == "hub" and c["sensor_kind"] == "solution")
        self.assertEqual(hub_solution["metrics"][0]["metric"], "ppm")
        self.assertEqual(hub_solution["metrics"][0]["value"], 820)
        waiting = next(c for c in panel["cards"] if c["name"] == "Solução da rega")
        self.assertEqual(waiting["status"], "waiting")
        air_card = next(c for c in panel["cards"] if c["name"] == "Ar da tenda")
        humidity = next(m for m in air_card["metrics"] if m["metric"] == "humidity_air")
        self.assertEqual((humidity["range"]["min"], humidity["range"]["max"], humidity["state"]), (45, 60, "near"))

        history = self.client.get(f"/sensors/{soil}/history", params={"metric": "soil_moisture_pct", "window": "24h"}).json()
        self.assertEqual((len(history["buckets"]), history["bucket_minutes"], history["min"], history["max"]),
                         (24, 60, 20, 50))
        self.assertEqual(history["buckets"][-1]["state"], "out")
        latest = self.client.get(f"/sensors/{self.hub_id}/latest").json()
        self.assertEqual(latest["ppm"], 820)
        self.assertEqual(self.client.get(f"/iot/grows/{self.other_grow}/sensors").status_code, 404)

    def test_ideal_ranges_api(self):
        body = self.client.get(f"/grows/{self.grow}/ideal-ranges").json()
        ranges_ = {r["metric"]: r for r in body["ranges"]}
        self.assertEqual((ranges_["humidity_air"]["min"], ranges_["humidity_air"]["source"]), (45, "phase"))
        self.assertTrue(ranges_["ppm"]["needs_review"])
        self.assertFalse(ranges_["ec_ms_cm"]["adjustable"])
        put = self.client.put(f"/grows/{self.grow}/ideal-ranges/ppm", json={"min": 700, "max": 1000})
        ranges_ = {r["metric"]: r for r in put.json()["ranges"]}
        self.assertEqual((ranges_["ppm"]["min"], ranges_["ppm"]["source"], ranges_["ppm"]["needs_review"]),
                         (700, "custom", False))
        self.assertEqual((ranges_["ec_ms_cm"]["min"], ranges_["ec_ms_cm"]["max"]), (1.4, 2.0))
        self.assertEqual(ranges_["ppm"]["suggested"]["min"], 800)
        self.assertEqual(self.client.put(f"/grows/{self.grow}/ideal-ranges/ppm", json={"min": 9, "max": 3}).status_code, 422)
        self.assertEqual(self.client.put(f"/grows/{self.grow}/ideal-ranges/ec_ms_cm", json={"min": 1, "max": 2}).status_code, 422)
        reset = self.client.delete(f"/grows/{self.grow}/ideal-ranges/ppm").json()
        self.assertEqual({r["metric"]: r for r in reset["ranges"]}["ppm"]["source"], "phase")
        self.assertEqual(self.client.get(f"/grows/{self.other_grow}/ideal-ranges").status_code, 404)


async def _all(db, stmt):
    return (await db.execute(stmt)).scalars().all()


if __name__ == "__main__":
    unittest.main()
