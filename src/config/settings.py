import os
from functools import lru_cache
from typing import Optional, Literal

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL
import yaml


class DatabaseSettings(BaseSettings):
    host: str = Field(default="localhost")
    port: int = Field(default=5432)
    username: str = Field(default="telegram_mcp")
    password: str = Field(default="")
    name: str = Field(default="telegram_mcp")

    @property
    def url(self) -> str:
        # Built via URL.create instead of an f-string so credentials with
        # special characters (@, :, /) get percent-escaped on render.
        return URL.create(
            "postgresql+asyncpg",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        ).render_as_string(hide_password=False)


class TelegramSessionSettings(BaseSettings):
    session_string: str = ""


class TelegramSettings(BaseSettings):
    api_id: int = Field(default=0)
    api_hash: str = Field(default="")
    sessions: list[TelegramSessionSettings] = Field(default_factory=list)
    mtproxy_host: Optional[str] = None
    mtproxy_port: int = 443
    mtproxy_secret: str = ""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")


class WorkerSettings(BaseSettings):
    poll_interval_seconds: int = 5


class HistorySweepSettings(BaseSettings):
    """Interval of the bottom-up history sweep over watching chats."""
    interval_hours: float = 6.0


class WebhookSettings(BaseSettings):
    enabled: bool = False
    url: str = ""
    interval_seconds: int = 60
    mode: Literal["batch", "single"] = "batch"


class LoggingSettings(BaseSettings):
    level: str = "INFO"
    format: str = "json"


class MediaLinksSettings(BaseSettings):
    """Time-limited public links to cached message media (get_media_link)."""
    # Public base URL serving /files/<code>; empty = the tool refuses (fail closed)
    public_url: str = ""
    ttl_seconds: int = 300
    max_download_seconds: int = 60
    max_download_mb: int = 200


class Settings(BaseSettings):
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    history_sweep: HistorySweepSettings = Field(default_factory=HistorySweepSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    media_links: MediaLinksSettings = Field(default_factory=MediaLinksSettings)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Master token of the UI/REST plane (PLAN_UI_MASTER_TOKEN). NOT passed
    # explicitly in get_settings(): pydantic-settings reads OS-env and .env
    # itself — an explicit os.environ.get() would silently ignore .env.
    # Empty value = every /api/* is 401 (fail closed).
    ui_master_token: str = ""


def load_config_from_yaml(config_path: str = "config.yml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def _media_links_settings(yaml_config: dict) -> MediaLinksSettings:
    """media_links resolution: OS env > .env > config.yml.

    An EMPTY value on any layer counts as absent and falls through to the
    next one — a copied .env.example (MEDIA_LINKS_PUBLIC_URL=) or a compose
    passthrough with an empty default must not silence config.yml. Only
    when every layer is empty does public_url stay "" (fail closed).
    """
    yaml_ml = yaml_config.get("media_links") or {}

    def layered(yaml_key: str, env_key: str, default):
        value = (
            os.environ.get(env_key)
            or dotenv_values(".env").get(env_key)
            or yaml_ml.get(yaml_key)
        )
        return value if value is not None else default

    return MediaLinksSettings(
        public_url=str(layered("public_url", "MEDIA_LINKS_PUBLIC_URL", "")),
        ttl_seconds=int(layered("ttl_seconds", "MEDIA_LINKS_TTL_SECONDS", 300)),
        max_download_seconds=int(layered("max_download_seconds", "MEDIA_LINKS_MAX_DOWNLOAD_SECONDS", 60)),
        max_download_mb=int(layered("max_download_mb", "MEDIA_LINKS_MAX_DOWNLOAD_MB", 200)),
    )


@lru_cache
def get_settings() -> Settings:
    import os
    
    yaml_config = load_config_from_yaml()
    
    db_yaml = yaml_config.get("postgres", {})
    db_config = {
        "host": db_yaml.get("host", "postgres"),
        "port": int(db_yaml.get("port", 5432)),
        "username": db_yaml.get("username", "telegram_mcp"),
        "password": db_yaml.get("password", ""),
        "name": db_yaml.get("database", "telegram_mcp"),
    }

    # DB_* env vars win over YAML (README's external-DB docker-compose
    # documents them): empty/absent vars keep the YAML value.
    db_env_overrides = (
        ("host", os.environ.get("DB_HOST")),
        ("port", os.environ.get("DB_PORT")),
        ("username", os.environ.get("DB_USER")),
        ("password", os.environ.get("DB_PASSWORD")),
        ("name", os.environ.get("DB_NAME")),
    )
    for key, value in db_env_overrides:
        if value:
            db_config[key] = value
    db_config["port"] = int(db_config["port"])
    
    return Settings(
        database=DatabaseSettings(**db_config),
        telegram=TelegramSettings(
            api_id=int(os.environ.get("TELEGRAM_API_ID", yaml_config.get("telegram", {}).get("api_id", 0))),
            api_hash=os.environ.get("TELEGRAM_API_HASH", yaml_config.get("telegram", {}).get("api_hash", "")),
            sessions=[
                TelegramSessionSettings(**s) if isinstance(s, dict) else TelegramSessionSettings(session_string=s)
                for s in yaml_config.get("telegram", {}).get("sessions", [])
            ],
            mtproxy_host=yaml_config.get("telegram", {}).get("mtproxy_host"),
            mtproxy_port=int(yaml_config.get("telegram", {}).get("mtproxy_port", 443)),
            mtproxy_secret=yaml_config.get("telegram", {}).get("mtproxy_secret", ""),
        ),
        worker=WorkerSettings(
            poll_interval_seconds=yaml_config.get("worker", {}).get("poll_interval_seconds", 5)
        ),
        history_sweep=HistorySweepSettings(
            interval_hours=float(yaml_config.get("history_sweep", {}).get("interval_hours", 6.0))
        ),
        webhook=WebhookSettings(
            enabled=yaml_config.get("webhook", {}).get("enabled", False),
            url=yaml_config.get("webhook", {}).get("url", ""),
            interval_seconds=yaml_config.get("webhook", {}).get("interval_seconds", 60),
            mode=yaml_config.get("webhook", {}).get("mode", "batch"),
        ),
        logging=LoggingSettings(
            level=yaml_config.get("logging", {}).get("level", "INFO"),
            format=yaml_config.get("logging", {}).get("format", "json"),
        ),
        media_links=_media_links_settings(yaml_config),
    )
