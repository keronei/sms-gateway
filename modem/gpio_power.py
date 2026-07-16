"""
gpio_power.py - drives the MU509's PWRKEY line through a transistor switch on
a Raspberry Pi GPIO pin.

--------------------------------------------------------------------------
Hardware wiring (default: BCM GPIO17 / physical pin 11)
--------------------------------------------------------------------------
The module's power-on pin must be pulled to GROUND for >=500ms to switch it
on. Do NOT wire the Pi's GPIO pin directly to the module's PWRKEY pin/GND —
use a small NPN transistor (e.g. 2N3904) or logic-level N-MOSFET (e.g.
2N7000) as a switch, both to protect the Pi's GPIO and because the module's
PWRKEY line typically needs to be *sunk* to ground rather than driven from a
3.3V source:

    Pi GPIO17 (pin 11) --[1k ohm resistor]--> transistor base
    transistor emitter -----------------------> Pi GND (pin 9) [common ground
                                                  with the modem supply]
    transistor collector ----------------------> modem PWRKEY pin

Idle state: GPIO LOW -> transistor OFF -> PWRKEY left floating (pulled up
internally by the module, per its datasheet).
Power pulse: GPIO HIGH for ~600ms -> transistor ON -> PWRKEY pulled to GND
-> module powers on. The same pulse also powers the module OFF if it happens
to already be on, per the datasheet's PWRKEY behaviour, so we only ever
pulse it when the device is NOT responding to AT commands.

If your wiring differs, change modem_gpio_power_pin in Settings — the pin
number is fully configurable, this is just the recommended default.
--------------------------------------------------------------------------
"""
import time
import threading

DEFAULT_PIN = 17          # BCM numbering -> physical pin 11
PULSE_SECONDS = 0.6        # comfortably above the 500ms minimum

_lock = threading.Lock()
_device = None
_device_pin = None


def _get_device(pin):
    """Lazily create (or recreate, if the pin changed) the gpiozero OutputDevice.
    Imported lazily so this module can still be imported on non-Pi machines
    (e.g. for unit tests) without gpiozero/RPi.GPIO installed."""
    global _device, _device_pin
    if _device is not None and _device_pin == pin:
        return _device
    from gpiozero import OutputDevice

    if _device is not None:
        _device.close()
    _device = OutputDevice(pin, active_high=True, initial_value=False)
    _device_pin = pin
    return _device


def power_pulse(pin=DEFAULT_PIN, seconds=PULSE_SECONDS):
    """Pulse the PWRKEY line to switch the modem on. Thread-safe."""
    with _lock:
        dev = _get_device(pin)
        dev.on()
        try:
            time.sleep(seconds)
        finally:
            dev.off()


def release(pin=None):
    """Release the GPIO pin (used on clean daemon shutdown)."""
    global _device, _device_pin
    with _lock:
        if _device is not None and (pin is None or pin == _device_pin):
            _device.close()
            _device = None
            _device_pin = None
