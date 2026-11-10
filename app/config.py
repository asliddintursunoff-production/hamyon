from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://finance:finance@db:5432/finance"  # the docker-compose database

    telegram_bot_token: SecretStr | None = None

    telethon_api_id: int | None = None
    telethon_api_hash: SecretStr | None = None
    telethon_phone: str | None = None
    telethon_session_path: str = "data/telethon"
    # Hosting without a persistent disk (Railway): the whole login session as a Telethon StringSession.
    # Create it with `python -m app.telegram.export_session`. When set, the session file is not used.
    telethon_session: SecretStr | None = None

    gemini_api_key: SecretStr | None = None
    # Each model has its own free-tier quota. Owner's text/voice try these in order:
    gemini_model: str = "gemini-flash-lite-latest"  # fast (<1s), handles Uzbek text and voice
    gemini_fallback_models: list[str] = ["gemini-2.5-flash", "gemini-flash-latest"]
    gemini_background_models: list[str] = ["gemini-flash-lite-latest", "gemini-2.5-flash"]
    # Background classification may use at most this many requests per day, so the owner's
    # text/voice messages always have free quota left.
    ai_background_daily_budget: int = 300

    # The owner (receives notifications). Anyone else must still know BOT_ACCESS_CODE.
    owner_telegram_id: int = 1220127328

    bot_access_code: SecretStr | None = None
    bot_session_days: int = 30
    bot_max_code_attempts: int = 5
    bot_lockout_minutes: int = 30

    session_secret: SecretStr | None = None  # derived from the secrets below when not set
    # HTTPS URL for the Telegram Mini App. Empty = use the optional cloudflared tunnel if it runs.
    public_base_url: str | None = None
    tunnel_metrics_url: str | None = "http://tunnel:2000/quicktunnel"
    # Set automatically by Railway; used as the Mini App URL when PUBLIC_BASE_URL is empty.
    railway_public_domain: str | None = None
    railway_service_id: str | None = None  # present only when running on Railway

    humo_bot_username: str = "HUMOcardbot"
    # The HUMO main-menu (reply keyboard) button that returns all card balances. Pressing a reply-keyboard
    # button just sends its text, so this works even when the keyboard message is far back in history.
    humo_balance_button_text: str | None = "💰 Balans"
    humo_balance_button_pattern: str = r"(?i)\b(balans|баланс|balance)\b"
    balance_refresh_timeout_seconds: int = 45

    initial_cards: str = "8549:AO ANOR BANK,3798:TBCBANK,5592:OCTOBANK"

    history_sync_initial_limit: int | None = None  # None = the whole HUMO chat history
    # Optional: never collect HUMO messages older than this date. None = collect everything.
    history_start_date: date | None = None
    history_sync_interval_seconds: int = 600

    ai_max_concurrency: int = 1
    ai_min_interval_seconds: float = 4.0
    ai_max_retries: int = 3
    ai_request_timeout_seconds: float = 25.0
    ai_max_message_attempts: int = 6
    ai_confidence_threshold: float = 0.75
    merchant_negative_cache_days: int = 7

    worker_count: int = 2
    queue_maxsize: int = 1000

    timezone: str = "Asia/Tashkent"
    currency: str = "UZS"

    @field_validator("*", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("database_url", mode="after")
    @classmethod
    def _async_driver(cls, v: str | None) -> str:
        if not v:
            return ""
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        # asyncpg calls libpq's "sslmode" just "ssl".
        if "+asyncpg" in v and "sslmode=" in v:
            v = v.replace("sslmode=", "ssl=")
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def history_start(self) -> datetime | None:
        """Start of history_start_date in local time (timezone-aware), or None for no limit."""
        if self.history_start_date is None:
            return None
        return datetime.combine(self.history_start_date, time.min, tzinfo=self.tz)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"} or bool(self.railway_service_id)

    @property
    def telethon_configured(self) -> bool:
        return bool(self.telethon_api_id and self.telethon_api_hash)

    def derived_session_secret(self) -> str:
        """Stable secret for signing cookies without an extra .env entry."""
        if self.session_secret:
            return self.session_secret.get_secret_value()
        import hashlib

        material = "|".join(
            v.get_secret_value() if v else ""
            for v in (self.telegram_bot_token, self.bot_access_code, self.telethon_api_hash)
        )
        return hashlib.sha256(f"finance-session|{material}".encode()).hexdigest()

    def parsed_initial_cards(self) -> list[tuple[str, str]]:
        result = []
        for chunk in (self.initial_cards or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            last4, _, bank = chunk.partition(":")
            last4 = last4.strip()
            if len(last4) == 4 and last4.isdigit():
                result.append((last4, bank.strip() or "HUMO"))
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
