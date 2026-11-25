# Mosquitto 2 com a configuração do LetsGrow dentro da imagem (sem bind mount:
# no Coolify o bind mount derrubou o deploy em 2026-10-30).
# Usuários e ACL: plugin dynamic-security (ver mosquitto.conf e entrypoint.sh).
# Build a partir de letsgrow-backend/: docker build -f mosquitto/Dockerfile .
FROM eclipse-mosquitto:2

# jq: o entrypoint refaz o usuário do backend dentro do dynamic-security.json
RUN apk add --no-cache jq

COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY mosquitto/entrypoint.sh /usr/local/bin/letsgrow-mosquitto-entrypoint

# sed tira um eventual CRLF do checkout no Windows.
RUN sed -i 's/\r$//' /usr/local/bin/letsgrow-mosquitto-entrypoint /mosquitto/config/mosquitto.conf \
 && chmod 0755 /usr/local/bin/letsgrow-mosquitto-entrypoint \
 && command -v mosquitto_ctrl > /dev/null \
 && test -f /usr/lib/mosquitto_dynamic_security.so

EXPOSE 1883
ENTRYPOINT ["/usr/local/bin/letsgrow-mosquitto-entrypoint"]
CMD ["/usr/sbin/mosquitto", "-c", "/mosquitto/config/mosquitto.conf"]
