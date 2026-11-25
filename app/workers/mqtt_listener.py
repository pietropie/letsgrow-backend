"""
MQTT listener -- started as an asyncio background task from app lifespan.

Subscribed topics
-----------------
1. letsgrow/+/+/telemetry
   Telemetry from any device (hub or standalone).
   Topic format: letsgrow/{grow_id}/{device_mac}/telemetry

2. letsgrow/+/hub/+/discovery
   Satellite discovery published by a hub when it detects a new ESP-NOW peer
   in pairing mode.
   Topic format: letsgrow/{grow_id}/hub/{hub_mac}/discovery

3. letsgrow/+/+/status (retained, LWT) and letsgrow/+/+/relay/+ (ack, config_ack, event)
   Hub status and relays: letsgrow/{grow_id}/LG-{MAC}/... (contract: letsgrow-firmware/hub/RELAYS.md).
   Handled by app/services/relays.py; the grow in the topic must be the Hub's grow.

Telemetry payload (JSON):
{
  "device_mac": "AA:BB:CC:DD:EE:FF",   # optional -- overrides topic segment
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

Discovery payload (JSON):
{
  "satellite_mac": "AA:BB:CC:DD:EE:01",
  "firmware_version": "1.0.0",          # optional
  "sensors_config": {"soil": true}      # optional
}

On receiving a discovery message the listener creates a SensorDevice with
is_paired=False so the grower can later assign it to a plant via
PATCH /iot/devices/{id}.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import aiomqtt

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.sensor import SensorDevice, SensorReading
from app.services import mqtt_broker, relays, sensor_catalog
from sqlalchemy import select

logger = logging.getLogger(__name__)

TOPIC_TELEMETRY = "letsgrow/+/+/telemetry"
TOPIC_DISCOVERY = "letsgrow/+/hub/+/discovery"
TOPIC_STATUS = "letsgrow/+/+/status"
TOPIC_RELAY = "letsgrow/+/+/relay/+"
RELAY_HANDLERS = {
    "ack": relays.handle_ack,
    "config_ack": relays.handle_config_ack,
    "event": relays.handle_event,
}


# ---------------------------------------------------------------------------
# Telemetry handler
# ---------------------------------------------------------------------------

async def _handle_telemetry(mac: str, payload: dict) -> None:
    # The hub can forward satellite readings; in that case the payload carries
    # the real device MAC so we look up the satellite, not the hub.
    effective_mac = payload.get("device_mac") or mac

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SensorDevice).where(SensorDevice.esp32_mac == effective_mac)
        )
        device = result.scalar_one_or_none()
        if not device:
            logger.warning("Telemetry from unknown device MAC: %s", effective_mac)
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


# ---------------------------------------------------------------------------
# Discovery handler
# ---------------------------------------------------------------------------

async def _handle_discovery(hub_mac: str, payload: dict, grow_id: uuid.UUID | None = None) -> None:
    """
    Called when a hub publishes to letsgrow/{grow_id}/hub/{hub_mac}/discovery.

    Creates a SensorDevice record for the satellite if it does not already
    exist, with is_paired=False so the grower can assign it to a plant later.
    """
    satellite_mac: str | None = payload.get("satellite_mac")
    if not satellite_mac:
        logger.warning("Discovery payload missing 'satellite_mac' from hub %s", hub_mac)
        return

    # Normalize MAC to uppercase, no separators variant is fine; keep as-is.
    satellite_mac = satellite_mac.upper()

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(SensorDevice).where(SensorDevice.esp32_mac == satellite_mac)
        )
        if existing.scalar_one_or_none():
            logger.debug(
                "Discovery: satellite %s already registered, skipping.", satellite_mac
            )
            return

        config = payload.get("sensors_config") or {}
        kind = sensor_catalog.kind_from_config("satellite", config)
        meta = sensor_catalog.KINDS.get(kind) if kind else None
        device = SensorDevice(
            # Nome provisório pelo tipo (sem MAC na tela) até o grower dar um nome
            name=meta.default_name if meta else "Satélite novo",
            sensor_kind=kind,
            esp32_mac=satellite_mac,
            grow_id=grow_id,
            module_type="satellite",
            hub_mac=hub_mac.upper(),
            is_paired=False,
            sensors_config=config,
            firmware_version=payload.get("firmware_version"),
            is_online=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)
        logger.info(
            "Discovery: created pending satellite device %s (id=%s) paired to hub %s",
            satellite_mac,
            device.id,
            hub_mac,
        )


# ---------------------------------------------------------------------------
# Hub: status e relés
# ---------------------------------------------------------------------------

async def _handle_hub_message(grow_segment: str, serial: str, kind: str, payload: dict) -> None:
    """kind = "status" ou o subtópico de relay/ (ack, config_ack, event)."""
    if not serial.startswith("LG-"):
        return
    mac = mqtt_broker.normalize_mac(serial[3:])
    if mac is None:
        return
    async with AsyncSessionLocal() as db:
        hub = (await db.execute(
            select(SensorDevice).where(SensorDevice.esp32_mac == mac, SensorDevice.module_type == "hub")
        )).scalar_one_or_none()
        if hub is None or str(hub.grow_id) != grow_segment:
            logger.debug("Hub message for unknown hub/grow: %s %s", serial, grow_segment)
            return
        if kind == "status":
            await relays.handle_status(db, hub, payload)
        else:
            handler = RELAY_HANDLERS.get(kind)
            if handler is None:
                return
            await handler(db, hub, payload)
        await db.commit()


# ---------------------------------------------------------------------------
# Main listener loop
# ---------------------------------------------------------------------------

async def run_mqtt_listener() -> None:
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.MQTT_HOST,
                port=settings.MQTT_PORT,
                username=settings.MQTT_USERNAME or None,
                password=settings.MQTT_PASSWORD or None,
            ) as client:
                logger.info(
                    "MQTT listener connected to %s:%d",
                    settings.MQTT_HOST,
                    settings.MQTT_PORT,
                )
                await client.subscribe(TOPIC_TELEMETRY)
                await client.subscribe(TOPIC_DISCOVERY)
                await client.subscribe(TOPIC_STATUS, qos=1)
                await client.subscribe(TOPIC_RELAY, qos=1)

                async for message in client.messages:
                    topic = str(message.topic)
                    parts = topic.split("/")

                    if not message.payload:
                        continue  # retido apagado
                    try:
                        payload = json.loads(message.payload)
                    except Exception as exc:
                        logger.error(
                            "Failed to parse MQTT payload on %s: %s", topic, exc
                        )
                        continue

                    try:
                        # letsgrow/{grow_id}/hub/{hub_mac}/discovery  -> 5 parts
                        if (
                            len(parts) == 5
                            and parts[2] == "hub"
                            and parts[4] == "discovery"
                        ):
                            hub_mac = parts[3]
                            parsed_grow_id = None
                            try:
                                parsed_grow_id = uuid.UUID(parts[1])
                            except ValueError:
                                parsed_grow_id = None
                            await _handle_discovery(hub_mac, payload, parsed_grow_id)

                        # letsgrow/{grow_id}/{device_mac}/telemetry   -> 4 parts
                        elif len(parts) == 4 and parts[3] == "telemetry":
                            mac = parts[2]
                            await _handle_telemetry(mac, payload)

                        # letsgrow/{grow_id}/LG-{MAC}/status           -> 4 parts
                        elif len(parts) == 4 and parts[3] == "status" and isinstance(payload, dict):
                            await _handle_hub_message(parts[1], parts[2], "status", payload)

                        # letsgrow/{grow_id}/LG-{MAC}/relay/{sub}      -> 5 parts
                        elif len(parts) == 5 and parts[3] == "relay" and isinstance(payload, dict):
                            await _handle_hub_message(parts[1], parts[2], parts[4], payload)

                        else:
                            logger.debug("Unhandled MQTT topic: %s", topic)

                    except Exception as exc:
                        logger.error(
                            "Error processing MQTT message on %s: %s", topic, exc
                        )

        except Exception as exc:
            logger.error(
                "MQTT connection lost: %s -- reconnecting in 5s", exc
            )
            await asyncio.sleep(5)
