import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """A safe configuration message that never includes a secret value."""


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    database_url: str
    token: str
    chat_id: str
    secret: str
    keywords: Path = ROOT / "keywords.json"
    max_alerts: int = 20

    @classmethod
    def from_env(cls):
        load_env()
        database_url = os.environ.get("DATABASE_URL", "").strip() or os.environ.get("POSTGRES_URL", "").strip()
        keys = ("DATABASE_URL or POSTGRES_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CRON_SECRET")
        values = [database_url, *(os.environ.get(key, "").strip() for key in keys[1:])]
        if not all(values):
            missing = [key for key, value in zip(keys, values) if not value]
            raise ConfigError("Missing Vercel setting: " + ", ".join(missing))
        if len(values[3]) < 16:
            raise ConfigError("CRON_SECRET must contain at least 16 characters")
        return cls(*values)
