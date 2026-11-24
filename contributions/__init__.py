"""
contributions - optional feature: aggregate forwarded M-PESA/KCB paybill
confirmation SMS into a normalized database, report progress per region and
per account, reply to contributors, and sync to a Google Sheet.

Deliberately isolated from the rest of the app:
  - its own tables (schema.py) and its own key/value settings
  - it only READS the existing `modem_inbox` table and only WRITES to the
    existing `modem_commands` table (to send replies) - exactly the two
    seams the Flask process already uses to talk to the modem daemon
  - one master switch (`enabled`); when off, nothing here reads, replies
    or syncs, and the rest of the dashboard behaves as if it didn't exist

Module map:
  schema.py   tables
  store.py    data access + settings + region matching + progress maths
  parser.py   forwarded-SMS -> payments (configurable regex)
  service.py  the ingest pipeline: one inbound message -> decisions + replies
  replies.py  reply templates + the outbound reply queue (non-blocking)
  reports.py  period/region/account queries for the dashboard
  sheets.py   Google Sheets client (service account, no Google libraries)
  sync.py     the periodic sync "window" (get online, push, go offline)
  worker.py   the background threads
  api.py      Flask blueprint (/api/contrib/...)
"""
