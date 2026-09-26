"""Local tools. Preview never sends messages or opens the database."""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import date

from vehicle_bot.config import Config, ROOT
from vehicle_bot.matching import Matcher
from vehicle_bot.portal import Portal
from vehicle_bot.service import CASABLANCA, run
from vehicle_bot.storage import Store
from vehicle_bot.telegram import Telegram, format_message
from datetime import datetime


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Read public notices without sending or saving")
    preview.add_argument("--date", type=date.fromisoformat, default=None)
    preview.add_argument("--details", type=int, default=0, help="Maximum matching detail pages to preview")
    commands.add_parser("check", help="Run a real check and send alerts")
    commands.add_parser("status", help="Show queue counts")
    backfill = commands.add_parser("backfill", help="Queue one older day after downtime; does not send")
    backfill.add_argument("--date", type=date.fromisoformat, required=True)
    resolve = commands.add_parser("resolve", help="Resolve an uncertain delivery after checking Telegram")
    resolve.add_argument("key", help="Notice key, e.g. o8p:1042601")
    resolve.add_argument("--action", required=True, choices=["retry", "sent"])
    args = parser.parse_args()
    if args.command == "preview":
        portal = Portal()
        try:
            target = args.date or datetime.now(CASABLANCA).date()
            notices = portal.listing(target, target)
            matcher = Matcher.from_file(ROOT / "keywords.json")
            matches = [notice for notice in notices if matcher.matches(notice.title)]
            print(json.dumps({"date": target.isoformat(), "aos_notices": len(notices),
                              "vehicle_matches": [asdict(n) for n in matches],
                              "listing_requests": portal.requests}, ensure_ascii=True, indent=2))
            for notice in matches[:max(0, args.details)]:
                print(format_message(notice, portal.details(notice)))
        finally:
            portal.close()
        return
    config = Config.from_env()
    store = Store(config.database_url, config.chat_id)
    portal = None
    try:
        store.initialize()
        if args.command == "status":
            result = store.counts()
        elif args.command == "backfill":
            if args.date > datetime.now(CASABLANCA).date():
                raise ValueError("Cannot backfill a future date")
            if not store.acquire():
                raise RuntimeError("A check is running; try again later")
            previous = store.last_scan()
            if previous and (args.date - previous).days > 1:
                raise ValueError("Backfill consecutive days, starting at the last scan date")
            portal = Portal()
            notices = portal.listing(args.date, args.date)
            result = store.remember(notices, Matcher.from_file(config.keywords),
                                    max(previous, args.date) if previous else args.date)
        elif args.command == "resolve":
            if not store.acquire():
                raise RuntimeError("A check is running; try again later")
            result = {"resolved": store.resolve(args.key, args.action == "retry")}
        else:
            portal = Portal()
            result = run(store, portal, Matcher.from_file(config.keywords),
                         Telegram(config.token, config.chat_id), config.max_alerts)
        print(json.dumps(result, indent=2))
    finally:
        if portal:
            portal.close()
        store.close()


if __name__ == "__main__":
    main()
