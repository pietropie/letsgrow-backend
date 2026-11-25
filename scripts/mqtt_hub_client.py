"""
Cria (ou recria) o usuário MQTT de um Hub direto no broker, sem o onboarding por BLE.
Serve para o Hub de bancada (firmware com MQTT_PASSWORD no secrets.h).

Uso (com MQTT_HOST/MQTT_PORT/MQTT_USERNAME/MQTT_PASSWORD do backend no ambiente):
    python scripts/mqtt_hub_client.py AABBCCDDEEFF <grow_id>

A senha nova sai só no terminal; não grave em arquivo versionado. Recriar troca a
senha e derruba a sessão antiga do Hub. Não mexe no banco: para o Hub aparecer no
app, use o onboarding (POST /api/v1/iot/hubs/activate) ou POST /api/v1/iot/devices.
"""
import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services import mqtt_broker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mac", help="MAC do Hub (12 hex, com ou sem ':')")
    parser.add_argument("grow_id", type=uuid.UUID, help="UUID do grow do Hub")
    args = parser.parse_args()

    mac = mqtt_broker.normalize_mac(args.mac)
    if mac is None:
        print("MAC inválido", file=sys.stderr)
        return 2
    password = mqtt_broker.generate_mqtt_password()
    asyncio.run(mqtt_broker.upsert_hub_client(mac, args.grow_id, password))
    print(f"usuário: {mac}\nsenha:   {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
