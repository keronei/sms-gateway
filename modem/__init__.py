"""
modem/ - the standalone modem-manager daemon: everything that talks to the
Huawei MU509 over serial (AT commands) or GPIO (power control).

This package is intentionally never imported by the Flask app (app.py,
dispatcher.py, etc). It runs as its own process (see manager.py / the
run_modem_manager.py entry point) and coordinates with the rest of the
system purely through the shared SQLite database in db.py:

  modem_status    - current state (device present, SIM, signal, PPP, ...)
  modem_events    - append-only log (URCs, power cycles, PPP retries, ...)
  modem_commands  - action requests from Flask (power-cycle, reconnect, ...)

This keeps low-level hardware/protocol code fully separated from the web
server, while both sides stay in sync through ordinary DB reads/writes.
"""
