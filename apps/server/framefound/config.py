"""Environment-driven configuration.

Every knob comes from the environment (see .env.example). Settings objects are
the ONLY place environment variables are read — application code takes typed
values, never os.environ.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAMEFOUND_", extra="ignore")

    server_name: str = "FrameFound"
    secret_key: str = ""  # required in production; validated at startup
    data_dir: Path = Path("/data")
    media_root: Path = Path("/media")
    compute: Literal["cpu", "cuda"] = "cpu"

    whisper_model: str = "small"
    vision_model: str = "ViT-B-32/laion2b_s34b_b79k"
    proxy_resolution: int = 1080

    # Assembled from POSTGRES_* / REDIS_* by compose; empty until M1 wiring.
    database_url: str = ""
    redis_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
