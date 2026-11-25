"""
Relés do Hub controlados pelo app (UX item 28b/28c; contrato reles-app-plano.md).

O Hub manda nos relés: a programação e a regra de umidade rodam nele. O servidor guarda o
vínculo de cada posição (1–4) com um equipamento do ambiente, publica a configuração em
`letsgrow/{grow}/LG-{MAC}/relay/config` (retida), repassa os comandos manuais e registra
o que o Hub confirma (relay/ack, relay/config_ack, relay/event, status).

Equipamentos são colunas do ambiente (`Grow`): a luz (`light`), o exaustor (`exhaust`) e
cada ventilador da lista `fans` (`fan:<id>`). A chave do equipamento é também o
`equip_ref` mandado ao Hub; quando ela muda numa posição, o Hub desliga a saída antes
(evento `rebind`), então o servidor não manda `relay_set off` separado.

Textos que vão para a tela não citam placa, MAC nem modelo de microcontrolador.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.notification import NotificationPreference
from app.models.sensor import HubRelay, RelayCommand, RelayEvent, SensorDevice
from app.services import mqtt_broker

logger = logging.getLogger(__name__)

POSITIONS = (1, 2, 3, 4)
EQUIP_KINDS = ("light", "exhaust", "fan")
MODES = ("manual", "schedule", "humidity")
OUTPUT_LIMIT_W = 1000
TOTAL_LIMIT_W = {127: 1200, 220: 2000}
DEFAULT_VOLTAGE = 127
ONLINE_WINDOW = timedelta(seconds=150)      # heartbeat do Hub a cada 60 s
COMMAND_TIMEOUT = timedelta(seconds=10)
MAX_DURATION_S = 86_400
DEFAULT_TZ = "America/Sao_Paulo"
_FAN_ID = re.compile(r"^[a-z0-9]{4,12}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

LIGHT_LABEL = {
    "led_quantum_board": "LED quantum board",
    "led_cob": "LED COB",
    "hps_hid": "Lâmpada HPS",
    "cmh": "Lâmpada CMH",
    "fluorescent": "Fluorescente",
    "other": "Luminária",
}
EXHAUST_LABEL = {"inline_fan": "Exaustor em linha", "axial_fan": "Exaustor axial", "pc_fans": "Coolers"}
KIND_LABEL = {"light": "Iluminação", "exhaust": "Exaustão", "fan": "Ventilação"}

SOURCE_TEXT = {
    "manual": "pelo app",
    "schedule": "pela programação",
    "humidity": "pela umidade",
    "override_end": "no fim da pausa manual",
    "timer": "no fim do tempo do comando",
    "boot": "ao religar o Hub",
    "failsafe": "por segurança, com o sensor sem leitura",
    "no_clock": "porque o Hub está sem hora",
    "clock_ok": "quando o Hub acertou a hora",
    "rebind": "ao trocar o equipamento da posição",
    "config": "pela configuração nova",
    "config_lost": "porque o Hub perdeu a configuração",
}
HUB_EVENT_TEXT = {
    "config_lost": "O Hub perdeu a configuração dos relés: as saídas ficaram desligadas até ela voltar.",
    "no_clock": "O Hub está sem hora: a luz programada fica desligada até ele acertar o relógio.",
}


# ---------------------------------------------------------------------------
# Equipamentos do ambiente
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Equipment:
    kind: str
    key: str
    name: str
    watts: int | None
    plural: bool = False


def normalize_fans(fans: list | None, previous: list | None = None) -> list | None:
    """Garante um `id` estável em cada ventilador. Mantém o id enviado (se válido);
    item sem id é ventilador novo. Campos desconhecidos continuam como estão."""
    if fans is None:
        return None
    used: set[str] = set()
    out = []
    for item in fans:
        if not isinstance(item, dict):
            continue
        fan = dict(item)
        fid = fan.get("id")
        if not (isinstance(fid, str) and _FAN_ID.match(fid)) or fid in used:
            fid = secrets.token_hex(4)
            while fid in used:
                fid = secrets.token_hex(4)
        fan["id"] = fid
        used.add(fid)
        out.append(fan)
    return out


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def equipment(grow: Grow) -> list[Equipment]:
    """Equipamentos do ambiente que podem ir numa posição de relé."""
    items: list[Equipment] = []
    if grow.light_type and grow.light_type != "none":
        label = LIGHT_LABEL.get(grow.light_type, "Luminária")
        watts = grow.lighting_watts
        items.append(Equipment("light", "light", f"{label} {watts} W" if watts else label, watts))
    if grow.exhaust_type and grow.exhaust_type != "none":
        label = EXHAUST_LABEL.get(grow.exhaust_type, "Exaustor")
        name = f"{label} {grow.exhaust_cfm} CFM" if grow.exhaust_cfm else label
        items.append(Equipment("exhaust", "exhaust", name, grow.exhaust_watts))
    for fan in grow.fans or []:
        if not isinstance(fan, dict) or not fan.get("id"):
            continue
        watts = _int(fan.get("watts"))
        qty = max(_int(fan.get("qty")) or 1, 1)
        base = f"Ventilador {watts} W" if watts else "Ventilador"
        name = f"Ventiladores {watts} W ({qty})" if qty > 1 and watts else (f"Ventiladores ({qty})" if qty > 1 else base)
        items.append(Equipment("fan", f"fan:{fan['id']}", name, watts * qty if watts else None, qty > 1))
    return items


def equipment_by_key(grow: Grow) -> dict[str, Equipment]:
    return {e.key: e for e in equipment(grow)}


def total_limit(voltage: int | None) -> int:
    return TOTAL_LIMIT_W.get(voltage or DEFAULT_VOLTAGE, TOTAL_LIMIT_W[DEFAULT_VOLTAGE])


def load_warnings(relays: list[HubRelay], equip: dict[str, Equipment], voltage: int | None) -> list[dict]:
    """Avisos de carga (avisam, não bloqueiam): 1.000 W por saída; soma até 1.200 W em
    127 V e 2.000 W em 220 V (tomada de 10 A)."""
    warnings: list[dict] = []
    total = 0
    for relay in sorted(relays, key=lambda r: r.n):
        item = equip.get(relay.equip_key or "")
        if item is None:
            continue
        if item.watts is None:
            warnings.append({"code": "missing_watts", "n": relay.n})
            continue
        total += item.watts
        if item.watts > OUTPUT_LIMIT_W:
            warnings.append({"code": "over_output_limit", "n": relay.n})
    if total > total_limit(voltage):
        warnings.append({"code": "over_total_limit", "n": None})
    return warnings


# ---------------------------------------------------------------------------
# Fuso e programação
# ---------------------------------------------------------------------------

def posix_tz(name: str | None, now: datetime | None = None) -> str:
    """Fuso IANA -> POSIX fixo para o Hub ("America/Sao_Paulo" -> "<-03>3").
    Usa o deslocamento de agora (o Brasil não tem horário de verão desde 2019)."""
    try:
        zone = ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TZ)
    offset = (now or datetime.now(timezone.utc)).astimezone(zone).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    hours, rest = divmod(abs(minutes), 60)
    sign = "-" if minutes < 0 else "+"
    abbr = f"<{sign}{hours:02d}{rest:02d}>" if rest else f"<{sign}{hours:02d}>"
    posix = f"{hours}:{rest:02d}" if rest else f"{hours}"
    return abbr + ("" if minutes < 0 else "-" if minutes > 0 else "") + posix


async def user_tz(db: AsyncSession, user_id: uuid.UUID) -> str:
    name = (await db.execute(
        select(NotificationPreference.timezone).where(NotificationPreference.user_id == user_id)
    )).scalar_one_or_none()
    try:
        ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TZ
    return name or DEFAULT_TZ


def _hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def light_schedule(grow: Grow) -> tuple[str, str] | None:
    """Horários da luz pelo fotoperíodo do ambiente (UX item 5). 24/0 vira 00:00–24:00
    (sempre ligada, sem depender do relógio do Hub). None sem programação completa."""
    minutes = grow.photoperiod_light_minutes
    if minutes and minutes >= 1440:
        return "00:00", "24:00"
    if grow.photoperiod_on_at is None or not minutes:
        return None
    start = grow.photoperiod_on_at.hour * 60 + grow.photoperiod_on_at.minute
    end = (start + minutes) % 1440
    return _hhmm(grow.photoperiod_on_at), f"{end // 60:02d}:{end % 60:02d}"


def relay_schedule(relay: HubRelay, grow: Grow) -> tuple[str, str] | None:
    if relay.equip_kind == "light":
        return light_schedule(grow)
    if relay.schedule_on and relay.schedule_off:
        return relay.schedule_on, relay.schedule_off
    return None


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def schedule_now(on: str, off: str, tz_name: str, now: datetime | None = None) -> tuple[bool, datetime | None]:
    """(programação diz ligado agora?, próxima borda em UTC). "24:00" no off = fim do dia;
    00:00–24:00 fica sempre ligado e não tem borda."""
    zone = ZoneInfo(tz_name)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    current = local.hour * 60 + local.minute
    start, end = _minutes(on), _minutes(off)
    if start == 0 and end == 1440:
        return True, None
    lit = start <= current < end if start < end else (current >= start or current < end)
    target = end if lit else start
    delta = (target - current) % 1440 or 1440
    edge = (local.replace(second=0, microsecond=0) + timedelta(minutes=delta)).astimezone(timezone.utc)
    return lit, edge


def valid_hhmm(value: str | None) -> bool:
    return bool(value and _HHMM.match(value))


# ---------------------------------------------------------------------------
# Hub: posições, estado online e configuração publicada
# ---------------------------------------------------------------------------

def hub_mac(hub: SensorDevice) -> str | None:
    return mqtt_broker.normalize_mac(hub.esp32_mac)


def hub_online(hub: SensorDevice, now: datetime | None = None) -> bool:
    if not hub.is_online or hub.last_seen_at is None:
        return False
    seen = hub.last_seen_at if hub.last_seen_at.tzinfo else hub.last_seen_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - seen <= ONLINE_WINDOW


async def hub_relays(db: AsyncSession, hub: SensorDevice, create: bool = True) -> list[HubRelay]:
    """As 4 posições do Hub (cria as que faltam, livres e em manual)."""
    rows = (await db.execute(
        select(HubRelay).where(HubRelay.hub_device_id == hub.id).order_by(HubRelay.n)
    )).scalars().all()
    by_n = {r.n: r for r in rows}
    if create and hub.grow_id is not None:
        for n in POSITIONS:
            if n not in by_n:
                relay = HubRelay(hub_device_id=hub.id, grow_id=hub.grow_id, n=n, mode="manual",
                                 min_on_s=60, min_off_s=60, override=False, state_confirmed=False)
                db.add(relay)
                by_n[n] = relay
        await db.flush()
    return [by_n[n] for n in sorted(by_n)]


def effective_mode(relay: HubRelay, grow: Grow) -> str:
    """Modo mandado ao Hub: posição livre ou programação sem horário viram manual."""
    if relay.equip_key is None:
        return "manual"
    if relay.mode == "schedule" and relay_schedule(relay, grow) is None:
        return "manual"
    if relay.mode == "humidity" and (relay.hum_on_above is None or relay.hum_off_below is None):
        return "manual"
    return relay.mode


def build_config(hub: SensorDevice, relays: list[HubRelay], grow: Grow, tz_name: str) -> dict:
    """JSON do relay/config (contrato do firmware, hub/hub_relay.h)."""
    items = []
    for relay in sorted(relays, key=lambda r: r.n):
        mode = effective_mode(relay, grow)
        item: dict = {
            "n": relay.n,
            "equip": relay.equip_kind or "none",
            "equip_ref": relay.equip_key or "",
            "mode": mode,
        }
        if mode == "schedule":
            on, off = relay_schedule(relay, grow)
            item["schedule"] = {"on": on, "off": off}
        if mode == "humidity":
            item["humidity"] = {
                "on_above": relay.hum_on_above, "off_below": relay.hum_off_below,
                "min_on_s": relay.min_on_s, "min_off_s": relay.min_off_s,
            }
        if relay.failsafe in ("on", "off"):
            item["failsafe"] = relay.failsafe
        items.append(item)
    return {"rev": hub.relay_rev, "tz": posix_tz(tz_name), "relays": items}


async def publish_config(db: AsyncSession, hub: SensorDevice, grow: Grow, relays: list[HubRelay] | None = None) -> None:
    """Sobe o rev e publica a configuração retida. Levanta BrokerUnavailable."""
    mac = hub_mac(hub)
    if mac is None or hub.grow_id is None:
        return
    relays = relays if relays is not None else await hub_relays(db, hub)
    hub.relay_rev = (hub.relay_rev or 0) + 1
    hub.relay_sync_error = None
    payload = build_config(hub, relays, grow, await user_tz(db, grow.user_id))
    await mqtt_broker.publish_relay_config(hub.grow_id, mac, payload)


def mark_rebind(relay: HubRelay, now: datetime) -> None:
    """Equipamento da posição mudou: o Hub vai desligar a saída (rebind)."""
    relay.state = 0
    relay.override = False
    relay.state_source = "rebind"
    relay.state_at = now
    relay.state_confirmed = False


async def sync_grow_relays(db: AsyncSession, grow: Grow, schedule_changed: bool = False) -> None:
    """Depois de mudar o ambiente: solta posições cujo equipamento saiu e republica a
    configuração dos Hubs afetados. Falha do broker não desfaz a mudança do ambiente:
    a configuração fica pendente (synced=false) e sai na próxima publicação."""
    relays = (await db.execute(select(HubRelay).where(HubRelay.grow_id == grow.id))).scalars().all()
    if not relays:
        return
    keys = equipment_by_key(grow)
    now = datetime.now(timezone.utc)
    affected: set[uuid.UUID] = set()
    for relay in relays:
        if relay.equip_key and relay.equip_key not in keys:
            relay.equip_kind = relay.equip_key = None
            relay.mode = "manual"
            mark_rebind(relay, now)
            affected.add(relay.hub_device_id)
        elif schedule_changed and relay.equip_kind == "light" and relay.mode == "schedule":
            affected.add(relay.hub_device_id)
    if not affected:
        return
    await db.flush()
    hubs = (await db.execute(select(SensorDevice).where(SensorDevice.id.in_(affected)))).scalars().all()
    for hub in hubs:
        try:
            await publish_config(db, hub, grow, [r for r in relays if r.hub_device_id == hub.id])
        except mqtt_broker.BrokerUnavailable:
            hub.relay_sync_error = "publish_failed"
            logger.warning("Configuração dos relés do Hub %s ficou pendente (broker fora do ar)", hub.id)


async def detach_hub(db: AsyncSession, hub: SensorDevice, old_grow_id: uuid.UUID | None) -> None:
    """Hub saiu do ambiente (reativação em outro grow ou remoção): solta as posições e
    apaga a configuração retida no tópico antigo (melhor esforço)."""
    relays = (await db.execute(select(HubRelay).where(HubRelay.hub_device_id == hub.id))).scalars().all()
    for relay in relays:
        await db.delete(relay)
    mac = hub_mac(hub)
    if old_grow_id is not None and mac is not None:
        try:
            await mqtt_broker.clear_relay_config(old_grow_id, mac)
        except mqtt_broker.BrokerUnavailable:
            logger.warning("Não foi possível apagar a configuração retida do Hub %s", hub.id)


# ---------------------------------------------------------------------------
# Mensagens do Hub (listener MQTT)
# ---------------------------------------------------------------------------

def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _bit(value) -> int | None:
    if value in (0, 1, True, False):
        return int(bool(value))
    return None


async def _relay(db: AsyncSession, hub: SensorDevice, n) -> HubRelay | None:
    if n not in POSITIONS:
        return None
    return (await db.execute(
        select(HubRelay).where(HubRelay.hub_device_id == hub.id, HubRelay.n == n)
    )).scalar_one_or_none()


def _set_state(relay: HubRelay, state: int, override: bool | None, source: str | None, at: datetime) -> None:
    if relay.state != state or not relay.state_confirmed:
        relay.state_at = at
    relay.state = state
    if override is not None:
        relay.override = override
    if source:
        relay.state_source = source
    relay.state_confirmed = True


async def handle_status(db: AsyncSession, hub: SensorDevice, payload: dict) -> None:
    """status (retido) e LWT: online, relógio, rev aplicado e estado de cada posição."""
    now = datetime.now(timezone.utc)
    online = bool(payload.get("online"))
    hub.is_online = online
    if not online:
        return
    hub.last_seen_at = now
    clock = payload.get("clock")
    if isinstance(clock, str):
        hub.clock_source = clock[:8]
    rev = payload.get("cfg_rev")
    if isinstance(rev, int) and rev == hub.relay_rev and rev > 0:
        hub.relay_synced_rev = rev
    for item in payload.get("relays") or []:
        if not isinstance(item, dict):
            continue
        relay = await _relay(db, hub, item.get("n"))
        state = _bit(item.get("state"))
        if relay is None or state is None:
            continue
        # Estado pendente de rebind só vale depois que o Hub aplicou a configuração nova
        if not relay.state_confirmed and relay.state_source == "rebind" and hub.relay_synced_rev != hub.relay_rev:
            continue
        _set_state(relay, state, bool(item.get("override")), None, now)


async def handle_config_ack(db: AsyncSession, hub: SensorDevice, payload: dict) -> None:
    rev = payload.get("rev")
    if not isinstance(rev, int):
        return
    if payload.get("ok"):
        if rev == hub.relay_rev:
            hub.relay_synced_rev = rev
            hub.relay_sync_error = None
    elif rev == hub.relay_rev or rev == 0:
        detail = payload.get("detail") or payload.get("err") or "config_invalid"
        hub.relay_sync_error = str(detail)[:40]


async def handle_ack(db: AsyncSession, hub: SensorDevice, payload: dict) -> None:
    """relay/ack: fecha o comando (idempotente: só um pending muda) e grava o estado."""
    try:
        req_id = uuid.UUID(str(payload.get("req_id")))
    except ValueError:
        return
    now = datetime.now(timezone.utc)
    ok = bool(payload.get("ok"))
    state = _bit(payload.get("state"))
    override = payload.get("override")
    err = payload.get("err")
    await db.execute(
        update(RelayCommand)
        .where(RelayCommand.req_id == req_id, RelayCommand.hub_device_id == hub.id, RelayCommand.status == "pending")
        .values(status="confirmed" if ok else "failed", confirmed_state=state,
                override=bool(override) if override is not None else None,
                err=None if ok else str(err or "failed")[:30], resolved_at=now)
        .execution_options(synchronize_session=False)
    )
    relay = await _relay(db, hub, payload.get("relay"))
    if relay is not None and state is not None:
        _set_state(relay, state, bool(override) if override is not None else None, "manual" if ok else None, now)


def _verb(equip_kind: str | None, plural: bool, on: bool) -> str:
    base = "ligad" if on else "desligad"
    return base + ("a" if equip_kind == "light" else "o") + ("s" if plural else "")


def _subject(equip_kind: str | None, item: Equipment | None) -> str:
    if equip_kind == "light":
        return "Luz"
    if equip_kind == "exhaust":
        return "Exaustor"
    if equip_kind == "fan":
        return "Ventiladores" if item and item.plural else "Ventilador"
    return "Saída"


def event_text(n: int, state: int, source: str, relay: HubRelay | None, item: Equipment | None, hum: float | None) -> str | None:
    """Texto do histórico do ambiente para um evento do Hub; None quando não vai para o histórico."""
    if n == 0:
        return HUB_EVENT_TEXT.get(source)
    kind = relay.equip_kind if relay else None
    subject = _subject(kind, item)
    name = f" ({item.name})" if item else ""
    how = SOURCE_TEXT.get(source, "")
    if source == "humidity" and hum is not None:
        how = f"pela umidade ({hum:.0f} %)".replace(".", ",")
    text = f"{subject}{name} {_verb(kind, bool(item and item.plural), bool(state))}"
    return f"{text} {how} · relé {n}".replace("  ", " ") if how else f"{text} · relé {n}"


async def handle_event(db: AsyncSession, hub: SensorDevice, payload: dict) -> bool:
    """relay/event: grava o evento (uma vez por seq), atualiza o estado da posição e
    registra no histórico do ambiente. Devolve False para evento repetido ou inválido."""
    n = payload.get("relay")
    state = _bit(payload.get("state"))
    source = payload.get("source")
    if not isinstance(n, int) or not 0 <= n <= 4 or state is None or not isinstance(source, str):
        return False
    seq = payload.get("seq") if isinstance(payload.get("seq"), int) else None
    if seq is not None:
        exists = (await db.execute(
            select(RelayEvent.id).where(RelayEvent.hub_device_id == hub.id, RelayEvent.seq == seq)
        )).scalar_one_or_none()
        if exists is not None:
            return False
    now = datetime.now(timezone.utc)
    ts = payload.get("ts")
    hub_ts = datetime.fromtimestamp(ts, timezone.utc) if isinstance(ts, int) and ts > 1_600_000_000 else None
    hum = payload.get("hum") if isinstance(payload.get("hum"), (int, float)) else None
    equip_ref = payload.get("equip_ref") if isinstance(payload.get("equip_ref"), str) else None

    relay = await _relay(db, hub, n) if n else None
    req_id = None
    if source == "manual" and relay is not None:
        req_id = (await db.execute(
            select(RelayCommand.req_id)
            .where(RelayCommand.hub_device_id == hub.id, RelayCommand.n == n,
                   RelayCommand.created_at >= now - timedelta(minutes=2))
            .order_by(RelayCommand.created_at.desc()).limit(1)
        )).scalar_one_or_none()

    event = RelayEvent(hub_device_id=hub.id, grow_id=hub.grow_id, n=n, state=state, source=source[:20],
                       seq=seq, hub_ts=hub_ts, hum=hum, equip_ref=(equip_ref or None) and equip_ref[:40],
                       req_id=req_id, received_at=now)
    try:
        async with db.begin_nested():
            db.add(event)
    except IntegrityError:
        return False

    at = hub_ts or now
    # Evento mais antigo que o estado conhecido (fila reenviada) não volta o estado. O ts do
    # Hub vem em segundos e o relógio dele não bate com o do servidor: 5 s de folga, e um
    # estado ainda não confirmado (rebind esperando o Hub) aceita qualquer evento novo.
    if relay is not None and (relay.state_at is None or not relay.state_confirmed
                              or _aware(relay.state_at) <= at + timedelta(seconds=5)):
        # O evento de rebind é a confirmação de que o Hub aplicou o vínculo novo
        _set_state(relay, state, False if source in ("rebind", "override_end", "config") else None, source, at)

    grow = await db.get(Grow, hub.grow_id) if hub.grow_id else None
    if grow is None:
        return True
    item = equipment_by_key(grow).get(relay.equip_key) if relay is not None and relay.equip_key else None
    # Evento de posição livre (rebind para "nada") não interessa ao histórico
    if n and (relay is None or (relay.equip_key is None and source != "rebind")):
        return True
    if source == "config" or (source == "rebind" and not state and item is None):
        return True
    text = event_text(n, state, source, relay, item, hum)
    if text:
        db.add(EnvironmentReading(grow_id=grow.id, user_id=grow.user_id, observed_at=at,
                                  notes=text, kind="relay", created_at=now, updated_at=now))
    return True


def command_view(command: RelayCommand, now: datetime | None = None) -> dict:
    status = command.status
    created = _aware(command.created_at) or datetime.now(timezone.utc)
    if status == "pending" and (now or datetime.now(timezone.utc)) - created > COMMAND_TIMEOUT:
        status = "timeout"
    return {
        "req_id": command.req_id,
        "status": status,
        "relay": command.n,
        "action": command.action,
        "requested_state": command.requested_state,
        "state": command.confirmed_state,
        "override": command.override,
        "err": command.err,
        "created_at": created,
        "resolved_at": _aware(command.resolved_at),
    }
