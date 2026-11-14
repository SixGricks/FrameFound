"""Environment-driven configuration.

Every knob comes from the environment (see .env.example). Settings objects are
the ONLY place environment variables are read — application code takes typed
values, never os.environ.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAMEFOUND_", extra="ignore")

    server_name: str = "FrameFound"
    secret_key: str = ""  # required before signed URLs ship (M3); warned at startup
    setup_token: str = ""  # one-time token that creates the first admin
    data_dir: Path = Path("/data")
    # Where video proxies live, when not with everything else under data_dir.
    # Proxies are most of the derived data by size, so they are the part
    # worth putting on a NAS share or an external drive. Same layout as under
    # data_dir, so moving them is a copy and a change of this one value
    # (`framefound proxies move`, storage/stores.py). None: under data_dir.
    proxy_dir: Path | None = None
    # One folder for everything bulky (a `Framefound` folder on a NAS share):
    # proxies, slideshow renders, listing exports, basemaps, the model
    # library and backup copies, each in a subfolder named after its kind
    # (storage/stores.py). None: each kind stays where it always was.
    bulk_dir: Path | None = None
    # Where each backup archive is copied off this machine. None: the bulk
    # folder's `backups` when there is one, else no copy (native/backup.py).
    backup_copy_dir: Path | None = None
    media_root: Path = Path("/media")
    # Model weights and caches. A volume at /models in the containers; a
    # worker running natively on another machine points it at a local folder.
    models_dir: Path = Path("/models")
    compute: Literal["cpu", "cuda"] = "cpu"
    # ONNX Runtime execution provider for the vision models. "coreml" runs
    # them on Apple silicon's GPU / Neural Engine (the native Mac install only);
    # it falls back to the CPU per model when CoreML cannot take it.
    onnx_provider: Literal["cpu", "coreml"] = "cpu"

    # --- Local AI models (docs/local-models.md) -----------------------------
    # Where the software that runs models is installed, one folder per
    # version. Set, it also means "this install can run models": the Mac
    # install's settings file names ~/FrameFound/runtimes, the containers
    # leave it unset and every local-AI control says so (local_models).
    runtimes_dir: Path | None = None
    # Both on this machine only (127.0.0.1). The first is FrameFound's own
    # model server; the second is the program it starts to run one model,
    # which nothing else talks to.
    model_server_port: int = 54805
    model_runtime_port: int = 54806
    # Developer only (CONTRIBUTING.md): the AI models page against made-up
    # models, for review in a browser on a machine that cannot run any.
    # Ignored when runtimes_dir is set or a public domain is configured, so
    # a real install can never be made to show them.
    local_ai_demo: bool = False

    # --- Workers on another machine (docs/remote-workers.md) ---------------
    # Library roots are stored as the server's own paths (/media/intel/...).
    # A worker that sees the same shares somewhere else maps them here, one
    # "server prefix=local prefix" pair per entry, separated by ";":
    #   /media/intel=/Volumes/intel;/media/breeze=/Volumes/Breeze
    # Empty on the server itself. Set, it also marks this process as a remote
    # worker: it never flags a file missing (only the server's scanner may),
    # and it refuses work unless the shared data store is really mounted.
    root_map: str = ""
    # Recorded on every job this process runs, so the processing page can say
    # which machine did the work. Empty means "the server".
    worker_name: str = ""
    domain: str = ""  # set in public-HTTPS mode; also switches cookies to Secure
    # The host port Caddy publishes (compose's FRAMEFOUND_HTTP_PORT), for the
    # addresses the Security page shows. The app itself listens on 8000.
    http_port: int = 8080

    whisper_model: str = "small"
    vision_model: str = "ViT-B-32/laion2b_s34b_b79k"
    proxy_resolution: int = 1080
    # Optional locally-built Blackmagic RAW decoder (docker-compose.braw.yml).
    braw_decoder: Path = Path("/opt/braw/braw-decode")
    # Per-invocation CPU budget for media tools. Small hosts must leave cores
    # for the API and database; 0 means "use the FFmpeg default (all cores)".
    ffmpeg_threads: int = 2
    whisper_threads: int = 3
    # Derivative generation pauses below this much free space on data_dir,
    # so a full disk can never take the database down with it.
    min_free_gb: float = 5.0
    # Comma-separated CIDRs whose X-Forwarded-For headers may be believed.
    # Defaults to the Docker bridge range, where our own Caddy container runs.
    # Anything not listed is treated as a direct client and cannot spoof its
    # address past the public-access gate or the rate limiter.
    trusted_proxies: str = "172.16.0.0/12"
    # Suggest tags on other assets as soon as one is learned. On by default:
    # a tag that does not spread is just a label.
    tag_autosuggest: bool = True

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
    # Empty in the containers, where the queue is only on the internal
    # network. A native install (docs/mac.md) shares the machine with every
    # other program on it, so its queue has a password.
    redis_password: str = Field("", validation_alias=AliasChoices("REDIS_PASSWORD"))

    # Full-URL override; takes precedence when set (tests, unusual deployments).
    database_url: str = ""

    @field_validator("proxy_dir", "bulk_dir", "backup_copy_dir", "runtimes_dir", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # An env file line `FRAMEFOUND_PROXY_DIR=` means "not set"; left to
        # pydantic it would become Path(""), the process's working directory.
        return None if isinstance(value, str) and not value.strip() else value

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
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"

    @property
    def cookie_secure(self) -> bool:
        # Public HTTPS mode implies TLS at the edge; local/tailnet mode is HTTP.
        return bool(self.domain)


@lru_cache
def get_settings() -> Settings:
    return Settings()
