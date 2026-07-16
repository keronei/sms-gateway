"""
backoff.py - small reusable exponential-backoff-with-jitter helper.

Used by both the device-presence/power-on watchdog and the PPP internet
supervisor so retry behaviour is consistent and easy to reason about in one
place.
"""
import random


class ExponentialBackoff:
    def __init__(self, base=3.0, factor=2.0, max_delay=120.0, jitter=0.3):
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.attempt = 0

    def reset(self):
        self.attempt = 0

    def peek_delay(self):
        """Delay that next_delay() would return, without advancing the counter."""
        delay = min(self.base * (self.factor ** self.attempt), self.max_delay)
        jitter_amount = delay * self.jitter
        return max(0.0, delay + random.uniform(-jitter_amount, jitter_amount))

    def next_delay(self):
        delay = self.peek_delay()
        self.attempt += 1
        return delay
