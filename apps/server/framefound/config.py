"""Environment-driven configuration.

Every knob comes from the environment (see .env.example). Settings objects are
the ONLY place environment variables are read — application code takes typed
values, never os.environ.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAMEFOUND_", extra="ignore")

    server_name: str = "FrameFound"
    secret_key: str = ""  # required before signed URLs ship (M3); warned at startup
    setup_token: str = ""  # one-time token that creates the first admin
    data_dir: Path = Path("/data")
    media_root: Path = Path("/media")
    compute: Literal["cpu", "cuda"] = "cpu"
    domain: str = ""  # set in public-HTTPS mode; also switches cookies to Secure

    whisper_model: str = "small"
    vision_model: str = "ViT-B-32/laion2b_s34b_b79k"
    proxy_resolution: int = 1080
    # Optional locally-built Blackmagic RAW decoder (docker-compose.braw.yml).
    braw_decoder: Path = Path("/opt/braw/braw-decode")

    # Sessions: sliding idle expiry with an absolute cap.
    session_idle_minutes: int = 720  # 12 h without activity logs you out
    session_absolute_hours: int = 168  # 7 d hard cap regardless of activity

    # Infrastructure endpoints share compose-level names (no FRAMEFOUND_ prefix).
    postgres_host: str = Field("postgres", validation_alias=AliasChoices("POSTGRES_HOST"))
    postgres_port: int = Field(5432, validation_alias=AliasChoices("POSTGRES_PORT"))
    postgres_user: str = Field("framefound", validation_alias=AliasChoices("POSTGRES_USER"))
    postgres_password: str = Field("", validation_alias=AliasChoices("POSTGRES_PASSWORD"))
    postgres_db: str = Field("framefound", validation_alias=AliasChoices("POSTGRES_DB"))
    redis_host: str = Field("redis", validation_alias=AliasChoices("REDIS_HOST"))
    redis_port: int = Field(6379, validation_alias=AliasChoices("REDIS_PORT"))

    # Full-URL override; takes precedence when set (tests, unusual deployments).
    database_url: str = ""

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def cookie_secure(self) -> bool:
        # Public HTTPS mode implies TLS at the edge; local/tailnet mode is HTTP.
        return bool(self.domain)


@lru_cache
def get_settings() -> Settings:
    return Settings()
