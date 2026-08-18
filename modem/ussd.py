"""
ussd.py - USSD session handling on top of an already-open ATChannel.

AT+CUSD's own OK response only confirms the request was *accepted* - the
network's actual reply arrives later as an unsolicited "+CUSD: <m>,<str>,<dcs>"
line (this is true whether it's the answer to a code we dialed, or a
mid-session menu reply). UssdWaiter bridges that: manager.py's URC callback
feeds every line through on_urc_line(), and whichever thread sent the
request calls wait_for_reply() to block (with a timeout) until it shows up -
the same way you'd watch a USSD popup appear on a phone.

Session semantics (3GPP TS 27.007 AT+CUSD): sending an initial code and
replying mid-session use the *identical* AT+CUSD=1,"<text>",15 command - the
network just knows which session it belongs to. AT+CUSD=2 ends a session.

<m> (session state) values: 0 = no further action needed (session over),
1 = further action required (network expects a reply), 2 = terminated by
network, 4 = operation not supported, 5 = network timeout.

Real device output is messier than the spec suggests, in three ways this
module has to tolerate:
  - <str> and <dcs> are themselves optional per 3GPP TS 27.007 - a bare
    "+CUSD: 0" (or "+CUSD: 2") with no quoted text at all is a valid, fairly
    common reply (e.g. a plain session-end acknowledgement with no message)
  - the quoted <str> can itself contain literal embedded newlines (e.g. a
    multi-item bundle menu), which arrives as several physical lines even
    though it's logically one response
  - the quoted <str> can contain an unescaped '"' byte (e.g. when it's
    actually raw packed 7-bit GSM septet garbage - a coincidental byte
    value, not a real quote), which breaks strict quote-matching

The latter two are handled by buffering: once a line starts a "+CUSD:"
response, keep appending subsequent lines until there's a brief pause in
arrival (real multi-line responses arrive in a fast burst), then parse the
whole buffered block with a loose regex that takes "first quote to last
quote" as the text rather than stopping at the first embedded quote. The
first (no quoted text at all) is handled by making that whole part of the
regex optional, so a bare reply resolves immediately rather than being
mistaken for the start of a multi-line response that never completes.
"""
import re
import time
import threading

from modem import text_codec

# Loose on purpose: DOTALL so '.' matches embedded newlines, and a greedy
# match up to the LAST quote (not the first) so a stray '"' byte inside
# garbled/packed content doesn't truncate the text early. The whole
# ,"<str>"[,<dcs>] portion is optional - 3GPP TS 27.007 defines <str> and
# <dcs> as optional parameters, and real networks commonly send a bare
# "+CUSD: 0" (or "+CUSD: 2") with no text at all for a plain session-end
# acknowledgement. Without this, a bare reply like that wouldn't match at
# all, and would fall through to on_urc_line's multi-line-buffering path
# waiting (uselessly, since nothing more is coming) for BUFFER_SETTLE_SECONDS,
# then to _finish()'s last-resort fallback, which used to display the raw
# "+CUSD: 0" AT line itself as if it were the message text.
CUSD_RE = re.compile(r'^\+CUSD:\s*(\d)\s*(?:,\s*"(.*)"\s*(?:,\s*(\d+))?)?\s*$', re.DOTALL)
CUSD_START_RE = re.compile(r'^\+CUSD:')

BUFFER_SETTLE_SECONDS = 0.5   # safety-net gap if nothing ever cleanly matches
MAX_BUFFER_SECONDS = 5.0      # absolute cap - a real multi-line reply arrives
                               # in a fast burst (well under a second in
                               # practice); if lines keep trickling in past
                               # this, something unrelated is being swept
                               # into the buffer - force a flush rather than
                               # risk buffering forever and never resolving


class UssdWaiter:
    def __init__(self):
        self._event = threading.Event()
        self._result = None
        self._lock = threading.Lock()
        self._buffer = None
        self._buffer_start = None
        self._settle_timer = None

    def on_urc_line(self, line):
        """Returns True if this line was consumed as part of a +CUSD: reply.
        Resolves as soon as the buffered content forms a complete match -
        the common case (a well-formed single-line reply, including ones
        with a stray embedded quote) resolves immediately with no added
        delay. Only a genuinely incomplete multi-line reply waits for more
        lines, bounded by MAX_BUFFER_SECONDS so it can never hang forever."""
        stripped = line.rstrip("\r\n")
        to_finish = []

        with self._lock:
            if self._buffer is not None and (time.time() - self._buffer_start) >= MAX_BUFFER_SECONDS:
                # been buffering too long without a clean match - give up on
                # it and finalize whatever we have, then re-evaluate this
                # line fresh below
                to_finish.append("\n".join(self._buffer))
                self._clear_buffer_locked()

            if self._buffer is not None:
                self._buffer.append(stripped)
                combined = "\n".join(self._buffer)
                if CUSD_RE.match(combined.strip()):
                    to_finish.append(combined)
                    self._clear_buffer_locked()
                else:
                    self._reset_settle_timer()
                consumed = True
            elif CUSD_START_RE.match(stripped.strip()):
                if CUSD_RE.match(stripped.strip()):
                    to_finish.append(stripped)
                else:
                    self._buffer = [stripped]
                    self._buffer_start = time.time()
                    self._reset_settle_timer()
                consumed = True
            else:
                consumed = False

        for combined in to_finish:
            self._finish(combined)
        return consumed

    def _reset_settle_timer(self):
        if self._settle_timer:
            self._settle_timer.cancel()
        self._settle_timer = threading.Timer(BUFFER_SETTLE_SECONDS, self._flush_buffer)
        self._settle_timer.daemon = True
        self._settle_timer.start()

    def _clear_buffer_locked(self):
        """Caller must hold self._lock."""
        self._buffer = None
        self._buffer_start = None
        if self._settle_timer:
            self._settle_timer.cancel()
            self._settle_timer = None

    def _flush_buffer(self):
        with self._lock:
            if self._buffer is None:
                return
            combined = "\n".join(self._buffer)
            self._clear_buffer_locked()
        self._finish(combined)

    def _finish(self, combined):
        m = CUSD_RE.match(combined.strip())
        if m:
            state_str, raw_text, dcs_str = m.groups()
        else:
            # even the loose regex couldn't match (unusual) - still resolve
            # rather than leave the caller hanging until timeout, using
            # whatever session-state digit we can find and the raw block as
            # the text so nothing is silently lost
            m2 = re.match(r'^\+CUSD:\s*(\d)', combined.strip())
            state_str = m2.group(1) if m2 else "0"
            raw_text = combined
            dcs_str = None

        dcs = int(dcs_str) if dcs_str is not None else 15
        text = decode_ussd_text(dcs, raw_text or "")
        self._result = {
            "session_state": int(state_str),
            "text": text,
            "raw_text": raw_text,
            "dcs": dcs,
        }
        self._event.set()

    def reset(self):
        """Clears any previous reply/event state. The caller (manager.py's
        _handle_send_ussd) MUST call this immediately before sending a new
        USSD request, not after - see wait_for_reply()'s docstring for why
        the clear can't safely live there."""
        with self._lock:
            self._clear_buffer_locked()
        self._result = None
        self._event.clear()

    def wait_for_reply(self, timeout=30):
        """Deliberately does NOT clear self._event here. AT+CUSD's own OK
        only confirms the request was accepted - the actual network reply
        can arrive (on the serial reader thread, via on_urc_line()) at any
        point after that, including in the brief window between ussd.send()
        returning and this function being called (e.g. while a log line is
        being written). If this function cleared the event itself, a reply
        that raced in during that window would be silently discarded -
        self._result would already hold the correct reply, but the caller
        would still block for the full timeout waiting for an event that
        will never come again, since the real reply was already consumed
        and won't be resent. Resetting must happen once, up front, right
        before the request is sent - see reset()."""
        if self._event.wait(timeout):
            return self._result
        return None


def send(channel, text, timeout=10):
    """Dial an initial USSD code, or reply mid-session - identical AT syntax
    either way. Our codes are always plain ASCII (*144#, menu digits, ...),
    so dcs is always 15 (GSM7/plain text) on the way out."""
    channel.send(f'AT+CUSD=1,"{_escape(text)}",15', timeout=timeout)


def end_session(channel, timeout=10):
    channel.send("AT+CUSD=2", timeout=timeout)


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ------------------------------------------------------------------ decode
def decode_ussd_text(dcs, text):
    """dcs=15 (and 0) are meant to be plain text per the module's own docs,
    but real devices/carriers are inconsistent about honoring that - so even
    for dcs=15 we run the same "does this actually look like hex/packed
    septets that need decoding" check used for SMS bodies, rather than
    trusting the flag blindly. See text_codec.py for that logic."""
    return text_codec.decode_possible_hex(text)
