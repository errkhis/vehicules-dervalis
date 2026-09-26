"""Separate PostgreSQL tables: the original bot's member tables are untouched."""
import json
import uuid
from dataclasses import asdict
from datetime import date

from .models import Notice


class Store:
    def __init__(self, database_url, chat_id):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row,
                                    connect_timeout=10, options="-c statement_timeout=15000")
        self.chat_id = chat_id
        self.owner = str(uuid.uuid4())
        self.locked = False

    def initialize(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_bot_runs (
                chat_id TEXT PRIMARY KEY, owner TEXT, locked_until TIMESTAMPTZ,
                last_scan DATE
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_bot_notices (
                chat_id TEXT NOT NULL, notice_key TEXT NOT NULL, notice JSONB NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','skipped','sending','sent','uncertain')),
                message TEXT, message_id BIGINT, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(chat_id, notice_key)
            )
        """)

    def acquire(self):
        self.conn.execute("INSERT INTO vehicle_bot_runs(chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (self.chat_id,))
        row = self.conn.execute("""
            UPDATE vehicle_bot_runs SET owner=%s, locked_until=NOW()+INTERVAL '9 minutes'
            WHERE chat_id=%s AND (locked_until IS NULL OR locked_until < NOW()) RETURNING chat_id
        """, (self.owner, self.chat_id)).fetchone()
        self.locked = row is not None
        if self.locked:
            # A previous invocation stopped after starting a Telegram request.
            self.conn.execute("""UPDATE vehicle_bot_notices SET status='uncertain',
                last_error='Previous run stopped during delivery', updated_at=NOW()
                WHERE chat_id=%s AND status='sending'""", (self.chat_id,))
        return self.locked

    def last_scan(self):
        row = self.conn.execute("SELECT last_scan FROM vehicle_bot_runs WHERE chat_id=%s", (self.chat_id,)).fetchone()
        return row["last_scan"] if row else None

    def remember(self, notices, matcher, scan_date: date):
        with self.conn.transaction():
            rows = self.conn.execute("""SELECT notice_key FROM vehicle_bot_notices
                WHERE chat_id=%s AND notice_key = ANY(%s)""",
                (self.chat_id, [n.key for n in notices])).fetchall()
            known = {row["notice_key"] for row in rows}
            new = []
            for notice in notices:
                if notice.key not in known:
                    new.append((self.chat_id, notice.key, json.dumps(asdict(notice), ensure_ascii=False),
                                "pending" if matcher.matches(notice.title) else "skipped"))
                    known.add(notice.key)
            if new:
                with self.conn.cursor() as cursor:
                    cursor.executemany("""INSERT INTO vehicle_bot_notices(chat_id,notice_key,notice,status)
                        VALUES (%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING""", new)
            self.conn.execute("UPDATE vehicle_bot_runs SET last_scan=%s WHERE chat_id=%s AND owner=%s",
                              (scan_date, self.chat_id, self.owner))
        return {"new_notices": len(new), "new_matches": sum(row[3] == "pending" for row in new)}

    def pending(self, limit):
        rows = self.conn.execute("""SELECT notice,message FROM vehicle_bot_notices
            WHERE chat_id=%s AND status='pending' AND retry_at <= NOW()
            ORDER BY attempts, created_at, notice_key LIMIT %s""", (self.chat_id, limit)).fetchall()
        return [(Notice(**row["notice"]), row["message"]) for row in rows]

    def cache_message(self, key, message):
        self.conn.execute("UPDATE vehicle_bot_notices SET message=%s WHERE chat_id=%s AND notice_key=%s",
                          (message, self.chat_id, key))

    def sending(self, key):
        self.conn.execute("""UPDATE vehicle_bot_notices SET status='sending',attempts=attempts+1,
            updated_at=NOW() WHERE chat_id=%s AND notice_key=%s""", (self.chat_id, key))

    def sent(self, key, message_id):
        self.conn.execute("""UPDATE vehicle_bot_notices SET status='sent',message_id=%s,
            last_error=NULL,updated_at=NOW() WHERE chat_id=%s AND notice_key=%s""",
            (message_id, self.chat_id, key))

    def failure(self, key, reason, uncertain=False, retry_seconds=600):
        self.conn.execute("""UPDATE vehicle_bot_notices SET status=%s,last_error=%s,attempts=attempts+1,
            retry_at=NOW()+(%s * INTERVAL '1 second'),updated_at=NOW()
            WHERE chat_id=%s AND notice_key=%s""",
            ("uncertain" if uncertain else "pending", reason[:200], retry_seconds, self.chat_id, key))

    def counts(self):
        rows = self.conn.execute("""SELECT status,COUNT(*) AS total FROM vehicle_bot_notices
            WHERE chat_id=%s GROUP BY status""", (self.chat_id,)).fetchall()
        return {r["status"]: r["total"] for r in rows}

    def resolve(self, key, retry):
        return self.conn.execute("""UPDATE vehicle_bot_notices SET status=%s,retry_at=NOW(),
            updated_at=NOW() WHERE chat_id=%s AND notice_key=%s AND status='uncertain' RETURNING notice_key""",
            ("pending" if retry else "sent", self.chat_id, key)).fetchone() is not None

    def close(self):
        try:
            if self.locked:
                self.conn.execute("UPDATE vehicle_bot_runs SET owner=NULL,locked_until=NULL WHERE chat_id=%s AND owner=%s",
                                  (self.chat_id, self.owner))
        finally:
            self.conn.close()
