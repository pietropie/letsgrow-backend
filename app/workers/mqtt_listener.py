"""
MQTT listener — started as an asyncio background task from app lifespan.
Subscribes to letsgrow/+/+/telemetry and persists readings to PostgreSQL.

Topic format: letsgrow/{grow_id}/{device_mac}/telemetry
Payload (JSON):
{
  "temp_air": 25.4,
  "humidity_air": 55.2,
  "co2_ppm": 900,
  "soil_moisture_pct": 42.0,
  "ph_solution": 6.2,
  "ec_ms_cm": 1.8,
  "vpd_kpa": 0.95,
  "lux": 32000,
  "temp_root": 22.0,
  "light_leak": false
}
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiomqtt

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.sensor import SensorDevice, SensorReading
from sqlalchemy import select

logger = logging.getLogger(__name__)

TOPIC = "letsgrow/+/+/telemetry"


async def _handle_telemetry(mac: str, payload: dict) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SensorDevice).where(SensorDevice.esp32_mac == mac)
        )
        device = result.scalar_one_or_none()
        if not device:
            logger.warning("Unknown device MAC: %s", mac)
            return

        now = datetime.now(timezone.utc)
        reading = SensorReading(
            device_id=device.id,
            recorded_at=payload.get("recorded_at") or now,
            temp_air=payload.get("temp_air"),
            humidity_air=payload.get("humidity_air"),
            co2_ppm=payload.get("co2_ppm"),
            soil_moisture_pct=payload.get("soil_moisture_pct"),
            ph_solution=payload.get("ph_solution"),
            ec_ms_cm=payload.get("ec_ms_cm"),
            vpd_kpa=payload.get("vpd_kpa"),
            lux=payload.get("lux"),
            temp_root=payload.get("temp_root"),
            light_leak=payload.get("light_leak"),
        )
        db.add(reading)

        device.is_online = True
        device.last_seen_at = now

        await db.commit()


async def run_mqtt_listener() -> None:
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.MQTT_HOST,
                port=settings.MQTT_PORT,
                username=settings.MQTT_USERNAME or None,
                password=settings.MQTT_PASSWORD or None,
            ) as client:
                logger.info("MQTT listener connected to %s:%d", settings.MQTT_HOST, settings.MQTT_PORT)
                await client.subscribe(TOPIC)

                async for message in client.messages:
                    topic_parts = str(message.topic).split("/")
                    if len(topic_parts) < 4:
                        continue
                    mac = topic_parts[2]
                    try:
                        payload = json.loads(message.payload)
                        await _handle_telemetry(mac, payload)
                    except Exception as exc:
                        logger.error("Error processing MQTT message: %s", exc)

        except Exception as exc:
            logger.error("MQTT connection lost: %s — reconnecting in 5s", exc)
            await asyncio.sleep(5)
