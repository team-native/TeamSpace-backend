from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "TeamSpace API"
    database_url: str = (
        "postgresql+psycopg://teamspace:teamspace@localhost:5432/teamspace"
    )
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    frontend_base_url: str = "http://localhost:3000"
    anonymous_author_secret: str | None = None
    app_timezone: str = "Asia/Seoul"

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: int = 15

    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:admin@teamspace.local"

    # Bank provider credentials stay in server-side environment configuration.
    # The concrete provider adapter is intentionally pending vendor selection.
    bank_provider: str | None = None
    bank_account_alias: str | None = None
    bank_account_number: str | None = None
    bank_api_base_url: str | None = None
    bank_api_client_id: str | None = None
    bank_api_client_secret: str | None = None
    bank_api_token: str | None = None

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
