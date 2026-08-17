"""
manager_test_utils.py - shared helpers for tests that exercise
ModemManager methods directly (never via .run()), without touching a
real serial port, GPIO, PPP supervisor, or sqlite database.
"""
from modem.manager import ModemManager


class CapturingLogger:
    """Stand-in for ModemManager.log() that records calls instead of
    writing to db.add_modem_event()."""

    def __init__(self):
        self.entries = []  # list of (level, category, message)

    def __call__(self, level, category, message):
        self.entries.append((level, category, message))

    def messages(self):
        return [m for _, _, m in self.entries]

    def levels(self):
        return [lvl for lvl, _, _ in self.entries]


def make_manager():
    mgr = ModemManager()
    mgr.log = CapturingLogger()
    return mgr
