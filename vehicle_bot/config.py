import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """A safe configuration message that never includes a secret value."""


def normalize_database_url(database_url):
    """Remove Supabase's integration-only URI tag before passing it to psycopg."""
    parts = urlsplit(database_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != "supa"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


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
        values[0] = normalize_database_url(values[0])
        return cls(*values)
