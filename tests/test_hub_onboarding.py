"""Onboarding do Hub por BLE e comandos MQTT para o Hub (contrato hub-onboarding-ble).

Cobre o código de vínculo (formato, validade, uso único, grow de outra conta), a
ativação pelo Hub (senha MQTT, SensorDevice do Hub, reativação, broker fora do
ar, limite de tentativas), o pair_open e o pair_confirm. O broker é simulado:
nada conecta a um Mosquitto de verdade.
"""
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import test_private_access as support  # noqa: F401  registra JSONB -> JSON no SQLite
from app.api.v1 import iot
from app.config import settings
from app.database import get_db
from app.models.base import Base
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.sensor import HubClaimCode, SensorDevice
from app.models.user import User
from app.services import mqtt_broker, rate_limit
from app.services.auth import get_current_user

TABLES = [User.__table__, Grow.__table__, Pot.__table__, Plant.__table__, SensorDevice.__table__,
          HubClaimCode.__table__]
MAC = "A4CF12AB34CD"


class HubOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.owner, self.other = uuid.uuid4(), uuid.uuid4()
        self.grow, self.grow2, self.other_grow = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        @asynccontextmanager
        async def lifespan(_app):
            async with self.engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
            async with self.sessions() as db:
                db.add_all([User(id=self.owner, email="a@example.test", username="a"),
                            User(id=self.other, email="b@example.test", username="b")])
                await db.flush()
                db.add_all([Grow(id=self.grow, user_id=self.owner, name="Tenda 80", start_date=date.today()),
                            Grow(id=self.grow2, user_id=self.owner, name="Tenda 60", start_date=date.today()),
                            Grow(id=self.other_grow, user_id=self.other, name="Alheio", start_date=date.today())])
                await db.commit()
            yield
            await self.engine.dispose()

        async def database():
            async with self.sessions() as db:
                yield db

        self.user = SimpleNamespace(id=self.owner, plan="cultivador")
        app = FastAPI(lifespan=lifespan)
        app.include_router(iot.router, prefix="/iot")
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.enterContext(patch.object(iot, "check_sensor_access", lambda user: None))
        self.enterContext(patch.object(settings, "RATE_LIMIT_ENABLED", False))
        self.upsert = self.enterContext(patch.object(mqtt_broker, "upsert_hub_client", AsyncMock()))
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

    def claim(self, grow_id=None):
        response = self.client.post("/iot/hubs/claim-codes", json={"grow_id": str(grow_id or self.grow)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def activate(self, code, mac=MAC, **extra):
        return self.client.post("/iot/hubs/activate", json={"mac": mac, "code": code, "fw": "1.2.0", **extra})

    def hub(self, mac=MAC):
        return self.run_db(lambda db: _scalar(db, select(SensorDevice).where(SensorDevice.esp32_mac == mac)))

    # -- códigos -------------------------------------------------------------

    def test_claim_code_format_ttl_and_grow_ownership(self):
        body = self.claim()
        code = body["code"]
        self.assertEqual(len(code), 8)
        self.assertTrue(set(code) <= set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789"))
        expires = datetime.fromisoformat(body["expires_at"])
        left = expires - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=14) < left <= timedelta(minutes=15))
        stored = self.run_db(lambda db: _scalar(db, select(HubClaimCode)))
        self.assertNotEqual(stored.code_hash, code)  # guardamos só o hash
        self.assertEqual(stored.grow_id, self.grow)
        other = self.client.post("/iot/hubs/claim-codes", json={"grow_id": str(self.other_grow)})
        self.assertEqual(other.status_code, 404)

    def test_activation_creates_hub_and_mqtt_user(self):
        code = self.claim()["code"]
        response = self.activate(code.lower(), mac="a4:cf:12:ab:34:cd")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["grow_id"], str(self.grow))
        self.assertGreaterEqual(len(body["mqtt_password"]), 24)
        self.assertEqual(body["mqtt_host"], settings.MQTT_PUBLIC_HOST)
        self.assertEqual(body["mqtt_port"], 8883)
        self.upsert.assert_awaited_once_with(MAC, self.grow, body["mqtt_password"])

        hub = self.hub()
        self.assertEqual((hub.module_type, hub.is_paired, hub.grow_id, hub.firmware_version),
                         ("hub", True, self.grow, "1.2.0"))
        used = self.run_db(lambda db: _scalar(db, select(HubClaimCode)))
        self.assertIsNotNone(used.used_at)
        self.assertEqual(used.used_by_mac, MAC)

        listed = self.client.get("/iot/devices").json()
        self.assertEqual([(d["esp32_mac"], d["module_type"]) for d in listed], [(MAC, "hub")])

    def test_code_is_single_use(self):
        code = self.claim()["code"]
        self.assertEqual(self.activate(code).status_code, 200)
        again = self.activate(code)
        self.assertEqual((again.status_code, again.json()), (400, {"detail": "code_invalid"}))
        other_mac = self.activate(code, mac="A4CF12AB34CE")
        self.assertEqual(other_mac.json(), {"detail": "code_invalid"})
        self.assertEqual(self.upsert.await_count, 1)

    def test_expired_unknown_and_malformed(self):
        code = self.claim()["code"]

        async def expire(db):
            claim = await _scalar(db, select(HubClaimCode))
            claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.run_db(expire)
        expired = self.activate(code)
        self.assertEqual((expired.status_code, expired.json()), (410, {"detail": "code_expired"}))

        self.assertEqual(self.activate("ZZZZ2345").json(), {"detail": "code_invalid"})
        self.assertEqual(self.activate("ABCD1234").json(), {"detail": "code_invalid"})  # '1' fora do alfabeto
        self.assertEqual(self.activate("ABC").json(), {"detail": "bad_request"})
        self.assertEqual(self.activate(code, mac="xyz").status_code, 400)
        self.assertEqual(self.activate(code, mac="xyz").json(), {"detail": "bad_request"})
        self.upsert.assert_not_awaited()
        self.assertIsNone(self.hub())

    def test_reactivation_rotates_password_and_keeps_device(self):
        first = self.activate(self.claim()["code"]).json()
        hub = self.hub()
        self.client.patch(f"/iot/devices/{hub.id}", json={"name": "Hub da sala"})

        second = self.activate(self.claim(self.grow2)["code"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(second.json()["mqtt_password"], first["mqtt_password"])
        self.assertEqual(second.json()["grow_id"], str(self.grow2))
        again = self.hub()
        self.assertEqual((again.id, again.name, again.grow_id), (hub.id, "Hub da sala", self.grow2))
        self.assertEqual(self.upsert.await_args.args, (MAC, self.grow2, second.json()["mqtt_password"]))

    def test_broker_down_keeps_code_usable(self):
        code = self.claim()["code"]
        self.upsert.side_effect = mqtt_broker.BrokerUnavailable("dynsec")
        down = self.activate(code)
        self.assertEqual((down.status_code, down.json()), (503, {"detail": "broker_unavailable"}))
        self.assertIsNone(self.hub())
        self.upsert.side_effect = None
        self.assertEqual(self.activate(code).status_code, 200)

    def test_activation_attempts_are_limited_per_mac(self):
        counter = rate_limit._MemoryCounter()

        async def hit(key, seconds, now):
            return counter.hit(key, seconds, now)

        with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
                patch.object(settings, "RATE_LIMIT_HUB_ACTIVATE_MAC", "2/600"), \
                patch.object(rate_limit, "_hit", hit):
            self.assertEqual(self.activate("ZZZZ2345").status_code, 400)
            self.assertEqual(self.activate("ZZZZ2346").status_code, 400)
            blocked = self.activate(self.claim()["code"])
            self.assertEqual((blocked.status_code, blocked.json()), (429, {"detail": "too_many_attempts"}))
            self.assertIn("retry-after", blocked.headers)
            # outro MAC continua liberado
            self.assertEqual(self.activate("ZZZZ2347", mac="A4CF12AB34CE").status_code, 400)

    # -- comandos para o Hub -------------------------------------------------

    def test_pairing_publishes_pair_open(self):
        self.activate(self.claim()["code"])
        hub = self.hub()
        response = self.client.post(f"/iot/devices/{hub.id}/pairing", json={"ttl_s": 90})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ttl_s"], 90)
        self.publish.assert_awaited_once_with(self.grow, MAC, {"cmd": "pair_open", "ttl_s": 90})
        self.assertEqual(mqtt_broker.hub_command_topic(self.grow, MAC), f"letsgrow/{self.grow}/LG-{MAC}/cmd")

        self.publish.reset_mock()
        self.assertEqual(self.client.post(f"/iot/devices/{hub.id}/pairing").json()["ttl_s"], 60)
        self.assertEqual(self.client.post(f"/iot/devices/{hub.id}/pairing", json={"ttl_s": 5000}).status_code, 422)

        self.publish.side_effect = mqtt_broker.BrokerUnavailable("publish")
        self.assertEqual(self.client.post(f"/iot/devices/{hub.id}/pairing").status_code, 503)

    def test_pairing_rejects_foreign_hub_and_non_hub(self):
        self.activate(self.claim()["code"])
        hub = self.hub()
        self.user = SimpleNamespace(id=self.other, plan="cultivador")
        self.assertEqual(self.client.post(f"/iot/devices/{hub.id}/pairing").status_code, 404)
        self.user = SimpleNamespace(id=self.owner, plan="cultivador")

        async def standalone(db):
            device = SensorDevice(name="Solo", esp32_mac="112233445566", grow_id=self.grow,
                                  module_type="standalone", is_paired=True, sensors_config={})
            db.add(device)
            await db.flush()
            return device.id
        device_id = self.run_db(standalone)
        self.assertEqual(self.client.post(f"/iot/devices/{device_id}/pairing").status_code, 409)
        self.publish.assert_not_awaited()

    def test_confirming_pending_satellite_publishes_pair_confirm(self):
        self.activate(self.claim()["code"])

        async def pending(db):
            sat = SensorDevice(name="Satellite", esp32_mac="AABBCCDDEE01", module_type="satellite",
                               hub_mac=MAC, is_paired=False, sensors_config={})
            db.add(sat)
            await db.flush()
            return sat.id
        sat_id = self.run_db(pending)

        listed = self.client.get("/iot/devices").json()
        self.assertIn(str(sat_id), [d["id"] for d in listed])

        response = self.client.patch(f"/iot/devices/{sat_id}", json={"grow_id": str(self.grow), "name": "Solo 1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_paired"])
        self.publish.assert_awaited_once_with(self.grow, MAC, {"cmd": "pair_confirm", "mac": "AABBCCDDEE01"})

        # renomear depois não manda de novo
        self.publish.reset_mock()
        self.client.patch(f"/iot/devices/{sat_id}", json={"name": "Solo vaso 1"})
        self.publish.assert_not_awaited()

    def test_pair_confirm_failure_keeps_satellite_pending(self):
        self.activate(self.claim()["code"])

        async def pending(db):
            sat = SensorDevice(name="Satellite", esp32_mac="AABBCCDDEE02", module_type="satellite",
                               hub_mac=MAC, is_paired=False, sensors_config={})
            db.add(sat)
            await db.flush()
            return sat.id
        sat_id = self.run_db(pending)
        self.publish.side_effect = mqtt_broker.BrokerUnavailable("publish")
        response = self.client.patch(f"/iot/devices/{sat_id}", json={"grow_id": str(self.grow)})
        self.assertEqual((response.status_code, response.json()), (503, {"detail": "broker_unavailable"}))
        sat = self.run_db(lambda db: _scalar(db, select(SensorDevice).where(SensorDevice.id == sat_id)))
        self.assertFalse(sat.is_paired)


class DynsecCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_hub_acl_matches_contract(self):
        grow = uuid.uuid4()
        commands = mqtt_broker.build_hub_client_commands(MAC, grow, "s3cret-password-xyz")
        names = [c["command"] for c in commands]
        self.assertEqual(names[:3], ["deleteClient", "deleteRole", "createRole"])
        self.assertEqual(names[-2:], ["createClient", "addClientRole"])
        acls = {(c["acltype"], c["topic"]) for c in commands if c["command"] == "addRoleACL"}
        topics = {f"letsgrow/{grow}/LG-{MAC}/#", f"letsgrow/{grow}/hub/{MAC}/#", f"letsgrow/{grow}/+/telemetry"}
        self.assertEqual({t for _, t in acls}, topics)
        self.assertEqual({a for a, _ in acls}, {"publishClientSend", "publishClientReceive", "subscribePattern"})
        self.assertTrue(all(c["allow"] for c in commands if c["command"] == "addRoleACL"))
        client = commands[-2]
        self.assertEqual((client["username"], client["password"]), (MAC, "s3cret-password-xyz"))
        self.assertEqual(commands[-1]["rolename"], f"hub-{MAC}")

    async def test_upsert_ignores_missing_on_delete_but_fails_on_create(self):
        grow = uuid.uuid4()
        count = len(mqtt_broker.build_hub_client_commands(MAC, grow, "x" * 32))

        def responses(error_on):
            out = [{"command": "deleteClient", "error": "Client not found"},
                   {"command": "deleteRole", "error": "Role not found"}]
            out += [{"command": "addRoleACL"}] * (count - 4)
            out += [{"command": "createClient"}, {"command": "addClientRole"}]
            if error_on:
                out[-2] = {"command": error_on, "error": "Client already exists"}
            return out

        with patch.object(mqtt_broker, "_dynsec", AsyncMock(return_value=responses(None))) as dynsec:
            await mqtt_broker.upsert_hub_client(MAC, grow, "x" * 32)
            self.assertEqual(dynsec.await_args.args[0][0], {"command": "deleteClient", "username": MAC})
        with patch.object(mqtt_broker, "_dynsec", AsyncMock(return_value=responses("createClient"))):
            with self.assertRaises(mqtt_broker.BrokerUnavailable):
                await mqtt_broker.upsert_hub_client(MAC, grow, "x" * 32)
        with patch.object(mqtt_broker, "_dynsec", AsyncMock(return_value=[])):
            with self.assertRaises(mqtt_broker.BrokerUnavailable):
                await mqtt_broker.upsert_hub_client(MAC, grow, "x" * 32)

    def test_mac_normalization_and_password(self):
        self.assertEqual(mqtt_broker.normalize_mac("a4:cf:12:ab:34:cd"), MAC)
        self.assertEqual(mqtt_broker.normalize_mac("A4-CF-12-AB-34-CD"), MAC)
        self.assertIsNone(mqtt_broker.normalize_mac("A4CF12AB34"))
        self.assertIsNone(mqtt_broker.normalize_mac("G4CF12AB34CD"))
        password = mqtt_broker.generate_mqtt_password()
        self.assertGreaterEqual(len(password), 24)
        self.assertFalse(set(password) & set(": ,"))


async def _scalar(db, statement):
    return (await db.execute(statement)).scalar_one_or_none()


if __name__ == "__main__":
    unittest.main()
