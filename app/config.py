from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration comes from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(default="", alias="DATABASE_URL")

    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_webhook_secret: SecretStr | None = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")

    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")

    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: SecretStr | None = Field(
        default=None, alias="SUPABASE_SERVICE_ROLE_KEY"
    )
    supabase_receipts_bucket: str = Field(default="receipts", alias="SUPABASE_RECEIPTS_BUCKET")
    signed_url_ttl_seconds: int = Field(default=900, alias="SIGNED_URL_TTL_SECONDS")

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required before starting receipt processing.")
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
