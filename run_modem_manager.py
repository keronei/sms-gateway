#!/usr/bin/env python3
"""
run_modem_manager.py - entry point used by the systemd service.
Kept separate from `python3 -m modem.manager` just so the systemd unit has a
stable, obvious path to point at.
"""
from modem.manager import main

if __name__ == "__main__":
    main()
