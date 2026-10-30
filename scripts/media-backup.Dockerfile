# Container do backup das fotos (serviço media-backup do docker-compose.coolify.yml).
#
# Usa a mesma imagem do MinIO, que já tem bash e mc, só para rodar
# scripts/backup-media.sh pela Scheduled Task do Coolify. O script vai dentro
# da imagem: não depende de arquivo nem de pasta no servidor.
# Ver analise-2026-10-16/DEVOPS.md, "Backup das fotos".
FROM ghcr.io/coollabsio/minio:RELEASE.2025-10-15T17-29-55Z

COPY scripts/backup-media.sh /opt/letsgrow/backup-media.sh

# Não roda o servidor MinIO: o container só espera a tarefa agendada.
ENTRYPOINT ["sleep", "infinity"]
CMD []
