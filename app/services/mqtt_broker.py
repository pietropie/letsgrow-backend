"""
Conversa do backend com o broker MQTT (Mosquitto 2 + plugin dynamic-security).

- Usuário MQTT de cada Hub, criado/atualizado em tempo real pela API de controle
  do plugin (`$CONTROL/dynamic-security/v1`), com uma role por Hub limitada aos
  tópicos do grow dele.
- Comandos para o Hub em `letsgrow/{grow_id}/LG-{MAC}/cmd` (QoS 1).

Cada chamada abre uma conexão curta com o usuário do backend (MQTT_USERNAME):
o volume é pequeno (ativação de Hub, abrir pareamento, confirmar satélite) e
assim nada depende do listener estar conectado. Falha do broker vira
BrokerUnavailable, que as rotas devolvem como 503.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

DYNSEC_TOPIC = "$CONTROL/dynamic-security/v1"
DYNSEC_RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"
TIMEOUT_S = 8.0

_MAC_RE = re.compile(r"^[0-9A-F]{12}$")


class BrokerUnavailable(RuntimeError):
    """Broker fora do ar ou recusou o comando."""


def normalize_mac(value: str | None) -> str | None:
    """'aa:bb:cc:dd:ee:ff' / 'AABBCCDDEEFF' → 'AABBCCDDEEFF'; inválido → None."""
    if not value:
        return None
    mac = re.sub(r"[:\-\s]", "", value).upper()
    return mac if _MAC_RE.match(mac) else None


def generate_mqtt_password() -> str:
    """Senha aleatória de 32 caracteres (URL-safe, sem ':' nem espaço)."""
    return secrets.token_urlsafe(24)


def hub_role_name(mac: str) -> str:
    return f"hub-{mac}"


def hub_topics(grow_id: uuid.UUID | str, mac: str) -> list[str]:
    """Tópicos liberados para o Hub (contrato hub-onboarding-ble, seção Broker)."""
    return [
        f"letsgrow/{grow_id}/LG-{mac}/#",
        f"letsgrow/{grow_id}/hub/{mac}/#",
        # telemetria dos satélites sai pelo Hub com o MAC do satélite no tópico
        f"letsgrow/{grow_id}/+/telemetry",
    ]


def hub_command_topic(grow_id: uuid.UUID | str, mac: str) -> str:
    return f"letsgrow/{grow_id}/LG-{mac}/cmd"


def build_hub_client_commands(mac: str, grow_id: uuid.UUID | str, password: str) -> list[dict]:
    """Comandos dynsec que (re)criam o cliente e a role do Hub.

    Apaga antes (erro "não existe" é ignorado) para a reativação trocar a senha e
    o grow da ACL de uma vez; apagar o cliente também derruba a sessão antiga.
    """
    role = hub_role_name(mac)
    commands: list[dict] = [
        {"command": "deleteClient", "username": mac},
        {"command": "deleteRole", "rolename": role},
        {"command": "createRole", "rolename": role, "textname": f"Hub {mac}"},
    ]
    for topic in hub_topics(grow_id, mac):
        for acltype in ("publishClientSend", "publishClientReceive", "subscribePattern"):
            commands.append({
                "command": "addRoleACL",
                "rolename": role,
                "acltype": acltype,
                "topic": topic,
                "priority": 0,
                "allow": True,
            })
    commands += [
        {"command": "createClient", "username": mac, "password": password, "textname": f"Hub {mac}"},
        {"command": "addClientRole", "username": mac, "rolename": role, "priority": 0},
    ]
    return commands


# Comandos cujo erro não importa (apagar o que pode não existir)
_IGNORE_ERRORS = {"deleteClient", "deleteRole"}


def _client():
    import aiomqtt  # importado aqui: os testes rodam sem a lib

    return aiomqtt.Client(
        hostname=settings.MQTT_HOST,
        port=settings.MQTT_PORT,
        username=settings.MQTT_USERNAME or None,
        password=settings.MQTT_PASSWORD or None,
        timeout=TIMEOUT_S,
    )


async def _dynsec(commands: list[dict]) -> list[dict]:
    """Manda os comandos ao plugin e devolve as respostas (uma por comando)."""
    async def run() -> list[dict]:
        async with _client() as client:
            await client.subscribe(DYNSEC_RESPONSE_TOPIC, qos=1)
            await client.publish(DYNSEC_TOPIC, json.dumps({"commands": commands}), qos=1)
            async for message in client.messages:
                data = json.loads(message.payload)
                return list(data.get("responses") or [])
        return []

    try:
        return await asyncio.wait_for(run(), TIMEOUT_S)
    except Exception as exc:  # conexão recusada, timeout, JSON ruim…
        logger.error("dynamic-security indisponível: %s", exc.__class__.__name__)
        raise BrokerUnavailable("dynsec") from exc


async def upsert_hub_client(mac: str, grow_id: uuid.UUID | str, password: str) -> None:
    """Cria (ou recria) o usuário MQTT do Hub com a ACL do grow."""
    commands = build_hub_client_commands(mac, grow_id, password)
    responses = await _dynsec(commands)
    if len(responses) < len(commands):
        raise BrokerUnavailable("dynsec: resposta incompleta")
    for response in responses:
        if response.get("error") and response.get("command") not in _IGNORE_ERRORS:
            # nunca logar o payload: o createClient tem a senha
            logger.error("dynsec %s falhou para o Hub %s: %s", response.get("command"), mac, response["error"])
            raise BrokerUnavailable(f"dynsec {response.get('command')}")
    logger.info("Usuário MQTT do Hub %s criado/atualizado (grow %s)", mac, grow_id)


async def publish_json(topic: str, payload: dict, qos: int = 1) -> None:
    async def run() -> None:
        async with _client() as client:
            await client.publish(topic, json.dumps(payload), qos=qos)

    try:
        await asyncio.wait_for(run(), TIMEOUT_S)
    except Exception as exc:
        logger.error("Falha ao publicar em %s: %s", topic, exc.__class__.__name__)
        raise BrokerUnavailable("publish") from exc


async def publish_hub_command(grow_id: uuid.UUID | str, hub_mac: str, payload: dict) -> None:
    await publish_json(hub_command_topic(grow_id, hub_mac), payload, qos=1)
