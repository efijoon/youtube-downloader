from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', env_prefix='YTDL_', extra='ignore')

    host: str = '127.0.0.1'
    port: int = 8000
    api_key: str | None = None

    data_dir: Path = ROOT / 'data'
    download_dir: Path = ROOT / 'downloads'
    headless: bool = True

    # JS runtime used by yt-dlp to solve YouTube's n/sig challenges (bun, deno, node)
    js_runtime: str = 'bun'

    cookie_refresh_minutes: int = 20
    max_concurrent_extractions: int = 2
    max_concurrent_downloads: int = 1
    info_cache_seconds: int = 300

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / 'profile'

    @property
    def cookies_file(self) -> Path:
        return self.data_dir / 'cookies.txt'

    @property
    def lock_file(self) -> Path:
        return self.data_dir / 'profile.lock'


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.download_dir.mkdir(parents=True, exist_ok=True)
    return s
