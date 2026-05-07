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
    GEMINI_MODEL: str = "gemini-2.0-flash-exp"
    EMBEDDING_MODEL: str = "models/text-embedding-004"
    EMBEDDING_DIMENSIONS: int = 768

    # MQTT
    MQTT_HOST: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""

    # MinIO
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "letsgrow"
    MINIO_SECRET_KEY: str = "letsgrow_dev"
    MINIO_BUCKET: str = "letsgrow-media"
    MINIO_SECURE: bool = False

    # Subscription plan limits
    FREE_MAX_GROWS: int = 1
    FREE_MAX_POTS_PER_GROW: int = 3
    FREE_AI_QUERIES_PER_MONTH: int = 10
    GROWER_MAX_GROWS: int = 3
    GROWER_MAX_POTS_PER_GROW: int = 8
    PRO_MAX_GROWS: int = 9999
    PRO_MAX_POTS_PER_GROW: int = 9999


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
