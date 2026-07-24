"""
ports.py - lightweight, one-off "is anything AT-capable listening on this
serial device" probe. Opens its own short-lived connection - do not call
this on a port that's already owned by an open ATChannel or an active pppd
session (i.e. never probe the data port while PPP is up).
"""
import time
from modem.serial_transport import HuaweiSerial


def probe_port(port, baudrate=115200, timeout=2.0):
    try:
        with HuaweiSerial(port, baudrate, timeout=timeout) as ser:
            ser.flush_input()
            ser.write(b"AT\r")
            time.sleep(0.3)
            data = ser.read(256)
            return b"OK" in data
    except OSError:
        return False
