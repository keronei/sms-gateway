"""Shared fixtures for the contributions tests: a temp DB with a small ward
hierarchy and one account, plus helpers to build confirmation SMS and push
messages through the real ingest pipeline."""
from datetime import datetime
from zoneinfo import ZoneInfo

from contributions import service, store
from tests.db_test_utils import TempDbTestCase

TZ = ZoneInfo("Africa/Nairobi")
RCV = datetime(2026, 5, 24, 11, 30, tzinfo=TZ).timestamp()   # when the SMS "arrived"
NOW = RCV + 60
ACCOUNT = "133***6248"


def sms(ref="UEOQP5BEC6", amount="200.00", date="24/05/2026", time="11:27 AM", account=ACCOUNT, paybill="522522"):
    return (f"Ksh {amount} sent to KCB Pay Bill {paybill} for account {account} MINISTRY OF REPENTANCE AND HOLINESS "
            f"has been received on {date} at {time}. M-PESA ref {ref}. To reverse this transaction, SMS this message to 16120.")


class ContribTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self._n = 0
        with store.tx() as c:
            self.lan = store.save_region(c, {"name": "Langata", "code": "LAN", "kind": "constituency"})
            self.kar = store.save_region(c, {"name": "Karen", "code": "KAR", "parent_id": self.lan, "aliases": ["Karen Ward"]})
            self.sc = store.save_region(c, {"name": "South C", "code": "SC", "parent_id": self.lan})
            self.kib = store.save_region(c, {"name": "Kibra", "code": "KIB"})
            pb = store.default_paybill(c)
            self.paybill_id = pb["id"]
            acc = store.save_account(c, {"account_ref": ACCOUNT, "label": "Building fund", "paybill_id": pb["id"]})
            self.acc = acc

    def settings(self, **kw):
        with store.tx() as c:
            store.set_settings(c, {k: str(v) for k, v in kw.items()})

    def ingest(self, sender, body, received_at=RCV, now=NOW, source_ref=None):
        self._n += 1
        with store.tx() as c:
            return service.process_message(c, "sms", source_ref if source_ref is not None else self._n,
                                           sender, body, received_at, now=now)

    def q(self, sql, *params):
        with store.tx() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def replies(self):
        return [(r["kind"], r["body"], r["phone"]) for r in self.q("SELECT * FROM outbound_replies ORDER BY id")]

    def reply_kinds(self):
        return [k for k, _, _ in self.replies()]

    def contribution(self, code):
        rows = self.q("SELECT * FROM contributions WHERE txn_code = ?", code)
        return rows[0] if rows else None
