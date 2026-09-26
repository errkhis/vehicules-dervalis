import hmac
import logging
import os
import re
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError

# Works when next_bot is the Vercel project root and when imported from this repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_bot.config import Config, ConfigError, load_env
from vehicle_bot.matching import Matcher
from vehicle_bot.portal import Portal
from vehicle_bot.service import run
from vehicle_bot.storage import Store
from vehicle_bot.telegram import Telegram


def authorized(provided_secret, expected_secret):
    return bool(expected_secret) and hmac.compare_digest(provided_secret or "", expected_secret)


def safe_database_message(exc):
    """Return a useful database message while hiding a connection password."""
    diagnostic = getattr(exc, "diag", None)
    message = getattr(diagnostic, "message_primary", None) or str(exc) or "Database request failed"
    return re.sub(r"postgres(?:ql)?://[^\s@]+@", "postgresql://***@", message)[:240]


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/check")
def check(cron_secret: str | None = Query(default=None, alias="secret")):
    """Run one protected procurement scan for the external scheduler."""
    load_env()
    if not authorized(cron_secret, os.environ.get("CRON_SECRET", "").strip()):
        raise HTTPException(status_code=401, detail="unauthorized")
    if os.environ.get("VERCEL_ENV") not in (None, "production"):
        raise HTTPException(status_code=403, detail="production_only")

    store = portal = None
    try:
        config = Config.from_env()
        matcher = Matcher.from_file(config.keywords)
        store = Store(config.database_url, config.chat_id)
        store.initialize()
        portal = Portal()
        result = run(store, portal, matcher, Telegram(config.token, config.chat_id), config.max_alerts)
        return JSONResponse(
            status_code=200 if result["ok"] else 503,
            content=result,
            headers={"Cache-Control": "no-store"},
        )
    except ConfigError as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc)},
            headers={"Cache-Control": "no-store"},
        )
    except PsycopgError as exc:
        message = safe_database_message(exc)
        logging.error("Vehicle database failed: %s", message)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "Database error: " + message},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        # Do not print credentials or raw request/database exception messages.
        logging.error("Vehicle check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": type(exc).__name__},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        if portal:
            portal.close()
        if store:
            store.close()
