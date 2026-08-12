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

    # Optional Netscape-format cookies file. On datacenter IPs (most VPS/Coolify
    # hosts) YouTube demands "Sign in to confirm you're not a bot"; a cookies file
    # exported from a logged-in browser is the reliable fix. Left empty, the bot
    # relies on player-client fallback (android/ios/tv), which covers many cases.
    youtube_cookies_file: Path | None = None

    # Optional generic Netscape-format cookies file shared by both download engines
    # (yt-dlp and gallery-dl). Needed for login-gated content such as Instagram
    # stories. Falls back to `youtube_cookies_file` when unset, so one exported
    # browser cookies file (which may hold cookies for many sites) covers everything.
    cookies_file: Path | None = None

    @property
    def effective_cookies_file(self) -> Path | None:
        return self.cookies_file or self.youtube_cookies_file

    # Max in-flight downloads a single user can hold at once (abuse guard).
    max_per_user_concurrent: int = 1

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


def load_settings() -> Settings:
    return Settings()
