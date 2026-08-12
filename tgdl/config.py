"""Typed application settings, loaded from environment / .env."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    database_path: Path = Path("data/tgdl.db")
    download_dir: Path = Path("data/downloads")
    max_file_size_mb: int = 48
    max_height: int = 720
    max_concurrent_downloads: int = 3
    download_timeout_s: int = 300
    log_level: str = "INFO"

    # Netscape-format cookies, kept in per-platform jars so each site only ever
    # receives its own credentials (see tgdl/downloader/cookies.py for the routing
    # policy — notably, non-story Instagram requests stay anonymous by default).
    #
    # Each jar can be given either as *content* via env var (the cookies.txt text
    # or its base64 encoding, auto-detected — preferred on PaaS hosts like Coolify;
    # materialized to a private temp file at startup) or as a *file path*. Content
    # wins over the path when both are set.
    #   COOKIES / COOKIES_FILE               generic jar (any platform without its own)
    #   YOUTUBE_COOKIES / YOUTUBE_COOKIES_FILE    used only for YouTube
    #   INSTAGRAM_COOKIES / INSTAGRAM_COOKIES_FILE used only for Instagram
    #     (stories, plus one retry when a post turns out to be login-walled)
    cookies: str = ""
    cookies_file: Path | None = None
    youtube_cookies: str = ""
    youtube_cookies_file: Path | None = None
    instagram_cookies: str = ""
    instagram_cookies_file: Path | None = None

    # Max in-flight downloads a single user can hold at once (abuse guard).
    max_per_user_concurrent: int = 1

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


def load_settings() -> Settings:
    return Settings()
