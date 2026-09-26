import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .telegram import DeliveryError, format_message

CASABLANCA = ZoneInfo("Africa/Casablanca")


def scan_start(now, previous):
    today = now.date()
    # Cover the date boundary and catch up after downtime.
    start = today - timedelta(days=1) if now.hour == 0 else today
    if previous:
        start = min(start, previous)
    if (today - start).days > 7:
        raise ValueError("More than 7 days of downtime: run a manual backfill before resuming")
    return start


def run(store, portal, matcher, sender, max_alerts=20, now=None, budget_seconds=220,
        target_date=None):
    started = time.monotonic()
    now = now or datetime.now(CASABLANCA)
    if not store.acquire():
        return {"ok": True, "skipped": "another_check_is_running"}
    result = {"ok": True, "listed": 0, "details_fetched": 0, "sent": 0, "errors": []}
    if target_date:
        result["test_date"] = target_date.isoformat()
    try:
        if target_date:
            notices = portal.listing(target_date, target_date)
            result.update(store.remember(notices, matcher, target_date,
                                         advance_checkpoint=False))
        else:
            notices = portal.listing(scan_start(now, store.last_scan()), now.date())
            result.update(store.remember(notices, matcher, now.date()))
        result["listed"] = len(notices)
    except Exception as exc:
        result["ok"] = False
        result["errors"].append("Listing failed: " + type(exc).__name__)
    pending = (store.pending_on_date(max_alerts, target_date.isoformat())
               if target_date else store.pending(max_alerts))
    for notice, message in pending:
        if time.monotonic() - started > budget_seconds:
            result["deferred"] = True
            break
        if message is None:
            try:
                result["details_fetched"] += 1
                message = format_message(notice, portal.details(notice))
                store.cache_message(notice.key, message)
            except Exception as exc:
                store.failure(notice.key, "Detail fetch failed: " + type(exc).__name__)
                result["ok"] = False
                result["errors"].append(notice.key + ": detail unavailable")
                continue
        store.sending(notice.key)
        try:
            message_id = sender.send(message)
        except DeliveryError as exc:
            store.failure(notice.key, str(exc), exc.uncertain, exc.retry_seconds)
            result["ok"] = False
            result["errors"].append(notice.key + ": " + str(exc))
            break  # Avoid repeating a rate limit or permission failure for every notice.
        store.sent(notice.key, message_id)
        result["sent"] += 1
        time.sleep(3.1)  # One group/channel, no burst of messages.
    result["queue"] = store.counts()
    if result["queue"].get("uncertain", 0):
        result["ok"] = False
        result["errors"].append("Some deliveries need review; see README")
    result["portal_requests"] = portal.requests
    result["seconds"] = round(time.monotonic() - started, 2)
    return result
