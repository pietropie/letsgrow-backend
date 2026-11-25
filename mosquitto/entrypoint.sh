#!/bin/sh
# Prepara o plugin dynamic-security e entrega para o entrypoint oficial da imagem.
#
#   MQTT_BACKEND_PASSWORD  senha do usuário "letsgrow-backend" (obrigatória)
#
# /mosquitto/data/dynamic-security.json fica no volume: os usuários dos Hubs
# (criados pelo backend em tempo real) sobrevivem a restart e redeploy. A cada
# subida, só o usuário do backend, a role dele e o acesso padrão são refeitos a
# partir do ambiente (trocar MQTT_BACKEND_PASSWORD e reiniciar basta). Os Hubs
# não são tocados aqui.
set -eu

DYNSEC=/mosquitto/data/dynamic-security.json
BACKEND_USER=letsgrow-backend

if [ -z "${MQTT_BACKEND_PASSWORD:-}" ]; then
  echo "[letsgrow] MQTT_BACKEND_PASSWORD vazio: o broker não sobe sem a senha do backend." >&2
  exit 1
fi

umask 077
mkdir -p /mosquitto/data
tmpdir=$(mktemp -d)

# Hash da senha (salt + PBKDF2) no formato do plugin, gerado pelo próprio mosquitto_ctrl.
mosquitto_ctrl dynsec init "$tmpdir/init.json" "$BACKEND_USER" "$MQTT_BACKEND_PASSWORD" > /dev/null
backend_client=$(jq -c --arg u "$BACKEND_USER" \
  '.clients[] | select(.username == $u) | .roles = [{"rolename": $u}] | .textname = "Backend LetsGrow"' \
  "$tmpdir/init.json")
rm -rf "$tmpdir"
if [ -z "$backend_client" ]; then
  echo "[letsgrow] mosquitto_ctrl não gerou o usuário do backend." >&2
  exit 1
fi

# Backend: publica e assina letsgrow/# (telemetria, discovery, comandos), lê $SYS/#
# e fala com a API de controle do plugin (cria/recria os usuários dos Hubs).
backend_role='{
  "rolename": "letsgrow-backend",
  "textname": "Backend LetsGrow",
  "acls": [
    {"acltype": "publishClientSend",    "topic": "$CONTROL/dynamic-security/#", "priority": 0, "allow": true},
    {"acltype": "publishClientReceive", "topic": "$CONTROL/dynamic-security/#", "priority": 0, "allow": true},
    {"acltype": "subscribePattern",     "topic": "$CONTROL/dynamic-security/#", "priority": 0, "allow": true},
    {"acltype": "publishClientSend",    "topic": "letsgrow/#", "priority": 0, "allow": true},
    {"acltype": "publishClientReceive", "topic": "letsgrow/#", "priority": 0, "allow": true},
    {"acltype": "subscribePattern",     "topic": "letsgrow/#", "priority": 0, "allow": true},
    {"acltype": "publishClientReceive", "topic": "$SYS/#", "priority": 0, "allow": true},
    {"acltype": "subscribePattern",     "topic": "$SYS/#", "priority": 0, "allow": true},
    {"acltype": "unsubscribePattern",   "topic": "#", "priority": 0, "allow": true}
  ]
}'

[ -s "$DYNSEC" ] || echo '{"clients": [], "groups": [], "roles": []}' > "$DYNSEC"

# Tudo que não está numa role é negado (publicar e assinar); receber o que já
# assinou e cancelar assinatura ficam liberados.
jq --arg u "$BACKEND_USER" --argjson client "$backend_client" --argjson role "$backend_role" '
  .defaultACLAccess = {
    "publishClientSend": false,
    "publishClientReceive": true,
    "subscribe": false,
    "unsubscribe": true
  }
  | .groups = (.groups // [])
  | .clients = ([(.clients // [])[] | select(.username != $u)] + [$client])
  | .roles = ([(.roles // [])[] | select(.rolename != $u)] + [$role])
' "$DYNSEC" > "$DYNSEC.new"
mv "$DYNSEC.new" "$DYNSEC"

chown -R mosquitto:mosquitto /mosquitto/data
chmod 0600 "$DYNSEC"
hubs=$(jq '[.clients[] | select(.username != "letsgrow-backend")] | length' "$DYNSEC")
echo "[letsgrow] dynamic-security pronto: backend + ${hubs} Hub(s)."

exec /docker-entrypoint.sh "$@"
