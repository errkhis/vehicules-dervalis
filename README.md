# Vehicle AOS alerts

This is a separate bot. It checks Moroccan **AOS** notices and posts one Telegram
message for each new title that matches a vehicle keyword. Buying, rental,
maintenance, repairs, and spare parts are included when a vehicle keyword appears
in the title. It does not use the old bot's Premium membership system.

## What each check does

1. Search today's AOS notices using normal HTTP requests, without Chrome.
2. Read the result list, including additional pages when needed.
3. Compare notice IDs with PostgreSQL in one lookup. Only new titles are matched.
4. Open detail pages only for new vehicle matches, or failed detail requests.
5. Send title, estimated amount, deadline, document indicator, and official link.
6. Save the Telegram message ID after a confirmed successful delivery.

The documents indicator comes from **“Prospectus, notices ou autres documents”**.
It does not mean that every downloadable file has been inspected. Missing or
unreadable field = “Non indiqué”; an explicit dash/none = “Non”.

The first check sends matching notices already published today. Dates use
`Africa/Casablanca`. During the first hour after midnight, yesterday is included.
The last completed scan date is also included after downtime, up to seven days.
IDs stop these overlapping searches from sending old notices again.

## Deploy on free Vercel

Create a **separate Vercel project** with **Root Directory = `vehicules-dervalis`** and
Framework Preset **Other**. Use Python 3.12. Keep the included `vercel.json`.
There is no browser/Node build step. Do not copy the old project's build settings.

Set these four environment variables for **Production**:

| Variable | What to put here |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The new bot's token from BotFather |
| `TELEGRAM_CHAT_ID` | Your group/channel ID (often `-100...`), or a public channel's `@username` |
| `DATABASE_URL` | PostgreSQL connection URL. Leave this empty when the Supabase Vercel integration already provides `POSTGRES_URL`. |
| `CRON_SECRET` | A random secret, at least 16 characters; 32 random bytes recommended |

Add the bot to your group/channel. Give it permission to send messages. For a
channel, make it an administrator with permission to post.

The database user needs permission to create tables. The bot creates
`vehicle_bot_runs` and `vehicle_bot_notices`; it does not modify the original
`summary_users` or `summary_admin_actions` tables. A transaction-pooler connection
can be used: locking uses a database row, not a session advisory lock.

After deploying, configure your **outside scheduler**:

| Setting | Value |
| --- | --- |
| Method | `GET` |
| URL | `https://YOUR-PROJECT.vercel.app/api/check?secret=YOUR_CRON_SECRET` |
| Frequency | Every 10 minutes (`*/10 * * * *`) |
| Timeout | Up to 300 seconds, if the scheduler supports it |

Use the Production URL. Keep this URL private because it contains the secret.
Preview deployments refuse to send messages. If Vercel
Deployment Protection is enabled for that URL, configure the scheduler's allowed
access using Vercel's supported protection settings.

Vercel Hobby's built-in cron cannot run every 10 minutes. An outside scheduler
triggers this ordinary Vercel function instead; normal function usage limits still
apply. See [Vercel cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).
`vercel.pro.json` is an optional alternative only for a future Pro deployment.

Nothing is scheduled just by creating these files. Production environment
variables, deployment, group permissions, and the outside scheduler are required.

## Try it locally without sending messages

From this folder:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py preview --details 2
```

On Linux/macOS, use `.venv/bin/python` instead. `preview` needs no Telegram token
or database. It only reads the public website and prints matches. Choose a date:

```text
python manage.py preview --date 2026-09-25 --details 2
```

To run the real job locally, copy `.env.example` to `.env`, fill it, then run
`python manage.py check`. **This command sends real Telegram messages.**

## Keywords and accents

Edit `keywords.json`, then redeploy. It contains French vehicle words, common
typos, plurals, Arabic terms, and an optional exclusion list. Titles are compared
without accents or letter case. Punctuation is normalized, so `pick-up` matches
`pick up`. Matching uses word boundaries: `bus` will not match `arbustes` or `buses`.
Telegram keeps the original title.

This is a keyword filter, not a perfect classifier. General words such as
`utilitaire` or `tracteur` can sometimes match a different kind of equipment.
No list can recognize every spelling mistake. Already recorded notices are not
rechecked when keywords change; the new list applies to newly discovered IDs.

## Failed checks and duplicate protection

- One database lease allows one check per destination at a time. The lease expires
  after 9 minutes if a process stops; Vercel's function limit is 300 seconds.
- A listing error does not advance the saved date or count as an empty day.
- Detail failures stay pending. Confirmed Telegram rejections are retried later.
- Successfully prepared messages are cached, so retrying delivery does not reopen
  their detail pages.
- Each run sends at most 20 alerts, spaced about 3 seconds apart. Remaining alerts
  stay queued for the next check. The worker stops taking more work near its time
  budget.
- A Telegram timeout can mean “sent, but the reply was lost.” Such a delivery is
  marked **uncertain**, rather than automatically sending a possible duplicate.
  A crash between sending and saving the Telegram response is handled the same way.

Exactly-once delivery cannot be guaranteed across a database and Telegram. Check
uncertain notices in the group, then resolve them locally using your configured
database connection:

```text
python manage.py status
python manage.py resolve ORG:NOTICE_ID --action sent
python manage.py resolve ORG:NOTICE_ID --action retry
```

Use `sent` if the message is already there; use `retry` if it is missing. The next
scheduled check sends a retried message. To find uncertain IDs, query:

```sql
SELECT notice_key, notice->>'title' AS title, last_error
FROM vehicle_bot_notices
WHERE chat_id = 'YOUR_CHAT_ID' AND status = 'uncertain';
```

After downtime longer than seven days, queue missing dates in order, starting at
the last recorded scan date, using `python manage.py backfill --date YYYY-MM-DD`.
This command queues matches but does not send. Repeat for consecutive days until
the saved date is within seven days of today; normal checks can then catch up.

The endpoint returns counts and errors as JSON. `200` means a successful check
(or another check already running), `401` means the secret was wrong, and `503`
means a scan/delivery needs retry or review. Database/configuration failures return
`500`. Monitor failed scheduler calls and the returned queue counts.

## Tests

```text
python -m unittest discover -s tests -v
```

Tests cover accents, Arabic, typos, word boundaries, parsing, pagination,
mixed encodings, midnight catch-up, duplicate prevention, queued retries,
uncertain sends, HTML escaping, and request authentication. Workflow tests use
an in-memory store and mocked Telegram. They do not send messages or verify a
real PostgreSQL deployment.

Live read-only check on 25 September 2026: 23 AOS notices, 2 vehicle matches,
3 requests for the listing and 2 detail requests. Both message previews included
their estimated amounts, deadlines, and documents indicator. The old bot was not
changed. Database credentials and Telegram delivery still need a deployment test.

A later read-only pagination check used smaller pages and collected **63 unique
notices across four pages**, using six HTTP requests. All 22 offline tests passed.
The local Windows environment blocked the PostgreSQL binary DLL, so a real
database connection was not tested here; production runs on Vercel's Linux runtime.
