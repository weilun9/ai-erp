from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repository root (one level above backend/)
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    app_env: str = "dev"
    app_name: str = "AI ERP"

    postgres_user: str = "erp"
    postgres_password: str = "erp_dev_password"
    postgres_db: str = "erp"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("dev", "test")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @model_validator(mode="after")
    def _no_dev_secret_outside_dev(self) -> "Settings":
        if not self.is_dev and (self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32):
            raise ValueError("JWT_SECRET must be set to a random value of 32+ chars outside dev")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
