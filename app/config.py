from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Intent Signal Update"
    environment: str = "development"
    database_url: str = "sqlite:///./intent_signals.db"
    api_key: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"

    scheduler_enabled: bool = False
    scheduler_interval_minutes: int = 30
    max_rss_items_per_poll: int = 50
    request_timeout_seconds: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
