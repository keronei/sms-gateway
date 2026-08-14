"""
fake_modem.py - a scriptable stand-in for modem.serial_at.ATChannel, for
tests that exercise modem/sms.py and modem/manager.py logic without any
real serial port or hardware.

It implements the same public surface sms.py and manager.py actually call
(send / send_expect_prompt / send_payload), and answers each command by
consulting a small rule table you hand it - so a test can describe modem
*behavior* ("this module rejects AT+CNMI mode=2 but accepts mode=1") rather
than hand-building AtResponse/ATError objects for every case.

Deliberately does NOT touch modem.serial_transport / real serial I/O, and
does not spin up the ATChannel reader thread - callers of sms.configure()
etc. only ever need the .send()-family methods, so this stays a plain
synchronous object.
"""
import re

from modem.serial_at import AtResponse, ATError, ATTimeout


class FakeATChannel:
    """Scriptable fake of ATChannel.

    `rules` maps a command matcher to a response:
      - matcher: an exact command string, or a compiled regex (re.Pattern)
        matched against the full command text with .match().
      - response, one of:
          "OK"                          -> success, no info lines
          ["+FOO: 1", "OK"]             -> success, info lines minus the
                                            trailing OK
          ATError(...) / ATTimeout(...) -> that exception instance is
                                            raised (re-raised on every
                                            matching call)
          callable(command) -> response  -> called fresh each time; return
                                            "OK"/list/exception as above,
                                            for stateful/dynamic behavior

    Unmatched commands raise AssertionError by default (fail loud in
    tests) unless `default_ok=True`, in which case they succeed with no
    info lines - handy for tests that only care about a couple of specific
    commands and don't want to script every housekeeping AT command too.

    Every call is recorded in `.sent_commands` in order, so tests can
    assert exactly what was sent and in what sequence.
    """

    def __init__(self, rules=None, default_ok=False):
        self.rules = list(rules or [])
        self.default_ok = default_ok
        self.sent_commands = []
        self.is_open = True
        # state for send_expect_prompt() / send_payload() pairing, mirroring
        # the real ATChannel's two-step CMGS-style flow
        self._awaiting_payload_for = None

    # ---------------------------------------------------------- scripting
    def add_rule(self, matcher, response):
        self.rules.append((matcher, response))

    def _resolve(self, command):
        for matcher, response in self.rules:
            if isinstance(matcher, re.Pattern):
                if matcher.match(command):
                    return response
            elif matcher == command:
                return response
        if self.default_ok:
            return "OK"
        raise AssertionError(
            f"FakeATChannel: no rule matched command {command!r} "
            f"(scripted matchers: {[m if isinstance(m, str) else m.pattern for m, _ in self.rules]})"
        )

    def _respond(self, command, response):
        if callable(response) and not isinstance(response, (ATError,)):
            response = response(command)
        if isinstance(response, ATError):
            raise response
        if response == "OK":
            return AtResponse(ok=True, lines=[], raw="OK")
        if isinstance(response, (list, tuple)):
            lines = list(response)
            terminal = lines[-1] if lines else "OK"
            info = lines[:-1]
            if terminal != "OK":
                raise ATError(f"{command!r} failed: {terminal}", lines=info)
            return AtResponse(ok=True, lines=info, raw="\n".join(lines))
        raise TypeError(f"Unsupported scripted response type: {response!r}")

    # ------------------------------------------------------- public API
    def send(self, command, timeout=10):
        self.sent_commands.append(command)
        response = self._resolve(command)
        return self._respond(command, response)

    def send_expect_prompt(self, command, timeout=10):
        self.sent_commands.append(command)
        response = self._resolve(command)
        if isinstance(response, ATError):
            raise response
        # real ATChannel would raise ATTimeout if no '>' prompt showed up;
        # here we just trust the script and remember what to answer on the
        # following send_payload() call.
        self._awaiting_payload_for = command

    def send_payload(self, payload, ctrl_z=True, timeout=20):
        command = self._awaiting_payload_for
        self._awaiting_payload_for = None
        key = f"{command}\x00PAYLOAD:{payload}"
        self.sent_commands.append(key)
        response = self._resolve(key)
        return self._respond(key, response)


def cms_error(code):
    """Builds an ATError shaped like a real '+CMS ERROR: <code>' failure,
    the way ATChannel._finalize() raises it."""
    return ATError(f"failed: +CMS ERROR: {code}", lines=[])


def at_timeout(command="AT"):
    return ATTimeout(f"Timed out waiting for response to {command!r}")


def cnmi_channel(supported_ds, cmgf_ok=True, ds_error_code=303, mode=1):
    """Convenience builder for the CNMI ds-fallback scenario: a module
    where AT+CMGF=0 works, and AT+CNMI=<mode>,1,0,<ds>,0 only succeeds for
    ds values in `supported_ds` (others fail with +CMS ERROR: ds_error_code).

    `mode` defaults to 1 - the module's own documented mode for this
    config (AT Command Interface Spec 6.3.4's worked example is
    "AT+CNMI=1,1,0,1,0"). Pass mode=2 to simulate the original bug this
    project hit (mode=2 is documented as "reserved, not supported
    currently" on the MU509, which is what actually produced the
    +CMS ERROR: 303 - not something specific to ds=1)."""
    rules = []
    if cmgf_ok:
        rules.append(("AT+CMGF=0", "OK"))
    else:
        rules.append(("AT+CMGF=0", cms_error(ds_error_code)))

    cnmi_re = re.compile(rf"^AT\+CNMI={mode},1,0,(\d+),0$")

    def cnmi_response(command):
        m = cnmi_re.match(command)
        ds = int(m.group(1))
        return "OK" if ds in supported_ds else cms_error(ds_error_code)

    rules.append((cnmi_re, cnmi_response))
    return FakeATChannel(rules=rules)
