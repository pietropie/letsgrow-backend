from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    APP_NAME: str = "LetsGrow API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://letsgrow:letsgrow_dev@localhost:5432/letsgrow"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_ANDROID_CLIENT_ID: str = ""
    GOOGLE_IOS_CLIENT_ID: str = ""

    # Google AI (Gemini)
    GOOGLE_API_KEY: str = ""
    # gemini-2.0-flash / gemini-2.0-flash-exp foram desativados pelo Google em
    # 01/06/2026 — usar um modelo estável atual com suporte a visão.
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # "models/text-embedding-004" foi descontinuado pela API do Gemini (404 NOT_FOUND
    # em embedContent na v1beta) — usar "models/gemini-embedding-001", que por padrão
    # gera vetores de 3072 dimensões. Para manter compatibilidade com a coluna
    # pgvector existente (Vector(768)), o EMBEDDING_DIMENSIONS abaixo é repassado como
    # `output_dimensionality` em cada chamada embed_query/embed_documents (ver
    # app/rag/indexer.py e app/rag/retriever.py).
    EMBEDDING_MODEL: str = "models/gemini-embedding-001"
    EMBEDDING_DIMENSIONS: int = 768

    # Provedores alternativos de IA — usados pelo painel admin (/admin/ai-panel)
    # para permitir trocar de provider em runtime, sem redeploy (ver
    # app/services/ai_provider.py e app/models/ai_config.py). Deixe em branco
    # se não for usar o provedor — o erro só aparece se alguém selecionar esse
    # provider no painel sem a chave configurada.
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    # DeepSeek e Z.ai (GLM) expõem API compatível com a da OpenAI — usamos o
    # mesmo client ChatOpenAI, só trocando base_url + api_key (ver ai_provider.py).
    DEEPSEEK_API_KEY: str = ""
    ZAI_API_KEY: str = ""

    # Token compartilhado que protege o painel admin (/admin/ai-panel) e os
    # endpoints /api/v1/admin/*. Gere algo forte com:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    # e configure a MESMA string aqui (.env local) e no Coolify (produção).
    # Com string vazia, o painel fica bloqueado por padrão (nunca casa).
    ADMIN_TOKEN: str = ""

    # MQTT
    MQTT_HOST: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""

    # MinIO
    # MINIO_ENDPOINT é o host usado pelo backend para FALAR com o MinIO
    # internamente (em produção, o nome do serviço no docker-compose: "minio:9000",
    # só resolvível dentro da rede do compose).
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "letsgrow"
    MINIO_SECRET_KEY: str = "letsgrow_dev"
    MINIO_BUCKET: str = "letsgrow-media"
    MINIO_SECURE: bool = False

    # MINIO_PUBLIC_ENDPOINT é o host/porta usado para GERAR as URLs pré-assinadas
    # (upload/download) que vão para o app mobile — precisa ser um endereço que
    # o celular consegue resolver e alcançar pela internet (domínio público ou
    # IP:porta do servidor), nunca o nome interno "minio". Sem isso, o app
    # recebe uma URL com host "minio" e quebra com "Unable to resolve host".
    # Se ficar em branco, cai para MINIO_ENDPOINT/MINIO_SECURE (ok só em dev,
    # quando o host configurado já é algo alcançável pelo celular).
    MINIO_PUBLIC_ENDPOINT: str = ""
    # String (não bool) de propósito: deixamos "" significar "herdar
    # MINIO_SECURE" — um campo bool tradicional rejeitaria "" como valor de
    # env var. Aceita "true"/"false" (case-insensitive); qualquer outra coisa
    # (incl. "") cai no fallback. Ver _minio_public_secure() em storage.py.
    MINIO_PUBLIC_SECURE: str = ""

    # Subscription plan limits
    FREE_MAX_GROWS: int = 1
    FREE_MAX_POTS_PER_GROW: int = 3
    FREE_MAX_PLANTS: int = 3
    FREE_AI_QUERIES_PER_MONTH: int = 10
    GROWER_MAX_GROWS: int = 3
    GROWER_MAX_POTS_PER_GROW: int = 8
    GROWER_MAX_PLANTS: int = 10
    PRO_MAX_GROWS: int = 9999
    PRO_MAX_POTS_PER_GROW: int = 9999
    PRO_MAX_PLANTS: int = 9999


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
