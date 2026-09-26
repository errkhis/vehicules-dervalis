import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Works when next_bot is the Vercel project root and when imported from this repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_bot.config import Config, load_env
from vehicle_bot.matching import Matcher
from vehicle_bot.portal import Portal
from vehicle_bot.service import run
from vehicle_bot.storage import Store
from vehicle_bot.telegram import Telegram


def authorized(header, secret):
    return bool(secret) and hmac.compare_digest(header or "", "Bearer " + secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        load_env()
        if not authorized(self.headers.get("Authorization"), os.environ.get("CRON_SECRET", "").strip()):
            self.respond(401, {"ok": False, "error": "unauthorized"})
            return
        if os.environ.get("VERCEL_ENV") not in (None, "production"):
            self.respond(403, {"ok": False, "error": "production_only"})
            return
        store = portal = None
        try:
            config = Config.from_env()
            matcher = Matcher.from_file(config.keywords)
            store = Store(config.database_url, config.chat_id)
            store.initialize()
            portal = Portal()
            result = run(store, portal, matcher, Telegram(config.token, config.chat_id), config.max_alerts)
            self.respond(200 if result["ok"] else 503, result)
        except Exception as exc:
            # Do not print credentials or raw request/database exception messages.
            logging.error("Vehicle check failed: %s", type(exc).__name__)
            self.respond(500, {"ok": False, "error": type(exc).__name__})
        finally:
            if portal:
                portal.close()
            if store:
                store.close()

    def respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, *_):
        pass
