"""Loads pipeline configuration from environment variables / .env."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_key_id: str
    private_key_path: str
    api_base: str
    max_requests_per_second: float
    db_path: str


def load_config() -> Config:
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    api_base = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2").strip().rstrip("/")
    max_rps = float(os.environ.get("KALSHI_MAX_REQUESTS_PER_SECOND", "8"))
    db_path = os.environ.get("KALSHI_DB_PATH", "data/kalshi.db").strip()

    missing = []
    if not api_key_id:
        missing.append("KALSHI_API_KEY_ID")
    if not private_key_path:
        missing.append("KALSHI_PRIVATE_KEY_PATH")
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill these in (see README.md)."
        )
    if not os.path.isfile(private_key_path):
        raise ConfigError(
            f"KALSHI_PRIVATE_KEY_PATH points to a file that doesn't exist: {private_key_path}"
        )

    return Config(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        api_base=api_base,
        max_requests_per_second=max_rps,
        db_path=db_path,
    )
