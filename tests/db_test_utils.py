"""
db_test_utils.py - redirects db.py's DB_PATH to a fresh temporary sqlite
file for the duration of a test, then restores it. Used by tests that
need to exercise real SQL (db.py's own logic, or code that calls db.py
functions directly like modem_gateway.py/dispatcher.py) rather than
mocking every db call - safer than pointing at the real
data/dashboard.db, and catches actual SQL bugs a pure mock never would.
"""
import os
import tempfile
import unittest

import db


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._real_db_path = db.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)  # let sqlite create it fresh
        self._tmp_path = path
        db.DB_PATH = path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._real_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp_path + suffix)
            except OSError:
                pass
