#!/bin/bash
# Backup diário das fotos: MinIO ("src") → Cloudflare R2 ("r2"), com lixeira.
#
# Roda no container minio pela Scheduled Task "mc mirror fotos" do Coolify
# (montado em /opt/letsgrow/backup-media.sh pelo docker-compose.coolify.yml).
# Ver analise-2026-10-16/DEVOPS.md, "Backup das fotos".
#
# - Copia para r2/<destino>/<bucket>/ o que é novo ou mudou no MinIO
#   (mc mirror sem --remove: este passo nunca apaga nada).
# - O que está no R2 e não existe mais no MinIO vai para
#   r2/<destino>/deleted/<bucket>/. Uma regra de lifecycle no R2 apaga
#   deleted/ depois de 33 dias (a política de privacidade promete até 35).
# - Nada fora de deleted/ é apagado. Se a listagem do MinIO falhar, ou se
#   sumir de uma vez mais que o limite abaixo, nada é movido e a tarefa falha.
#
# BACKUP_BUCKETS troca a lista de buckets (usado no teste com um bucket
# temporário). MC_HOST_r2 vem da variável MEDIA_BACKUP_R2_URL do Coolify.
set -euo pipefail

DEST="r2/letsgrow-media-backups"
BUCKETS="${BACKUP_BUCKETS:-avatars events strain-images}"
# Trava contra uma listagem errada do MinIO: no máximo 20% dos objetos de um
# bucket (ou 10, o que for maior) vão para a lixeira numa execução.
MAX_MOVE_PERCENT=20
MAX_MOVE_MIN="${MAX_MOVE_MIN:-10}"

export MC_HOST_src="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@localhost:9000"

for b in $BUCKETS; do
  mc mirror --overwrite --quiet "src/$b" "$DEST/$b"

  # Falha aqui interrompe tudo antes de mover qualquer objeto.
  src_list=$(mc find "src/$b")
  # Prefixo ainda vazio no R2 (bucket sem fotos) não é erro: não há o que mover.
  dst_list=$(mc find "$DEST/$b" 2>/dev/null || true)

  declare -A live=()
  while IFS= read -r p; do
    [ -n "$p" ] && live["${p#src/$b/}"]=1
  done <<< "$src_list"

  gone=()
  total=0
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    total=$((total + 1))
    k="${p#"$DEST/$b/"}"
    [ -n "${live[$k]+x}" ] || gone+=("$k")
  done <<< "$dst_list"

  limit=$((total * MAX_MOVE_PERCENT / 100))
  [ "$limit" -ge "$MAX_MOVE_MIN" ] || limit=$MAX_MOVE_MIN
  if [ "${#gone[@]}" -gt "$limit" ]; then
    echo "ERRO: $b: ${#gone[@]} de $total objetos sumiram do MinIO (limite $limit)." >&2
    echo "Nada foi movido. Confira o MinIO; se a exclusão for real, rode com MAX_MOVE_MIN maior." >&2
    exit 1
  fi

  for k in "${gone[@]}"; do
    mc mv --quiet "$DEST/$b/$k" "$DEST/deleted/$b/$k"
    echo "lixeira: $b/$k"
  done
  echo "$b: $total no backup, ${#gone[@]} movidos para a lixeira"
  unset live
done
