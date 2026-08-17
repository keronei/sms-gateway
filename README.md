# Dispatch — SMS Mail Merge Console

A small self-hosted dashboard for personalizing and bulk-sending SMS, with CSV
mail merge, a review step before sending, and live delivery status. Sending
can go through either an
[android-sms-gateway](https://github.com/capcom6/android-sms-gateway) device
or a directly-attached Huawei modem — see "SMS dispatch backend" below.

## How it works

1. **Settings** — choose the dispatch backend (Android Gateway or Huawei
   modem), then configure it: for the gateway, your Android phone's SMS
   Gateway Local Server address/port/credentials; for the modem, its ports
   and dial settings (see the Modem section further down).
2. **Compose** — write a template with `{fields}`, upload a CSV, map each field to
   a CSV column, and preview every merged message before anything is sent.
3. **Review & Dispatch** — create a campaign from the preview, then start
   dispatching. Messages send with a configurable delay/batching so the gateway
   doesn't get flagged for bulk sending. You can pause, resume, or stop mid-run.
4. **History** — every campaign and its per-recipient outcome is saved locally
   (SQLite) so you can revisit it or export a results CSV.

## Requirements

- Python 3.9+
- The SMS Gateway Android app, with **Local Server** mode turned on (Settings →
  the app shows its local IP, port — default 8080 — and Basic Auth credentials).
- The dashboard machine (Mac now, Raspberry Pi later) must be able to reach the
  phone: either on the same LAN, or both joined to the same Tailscale network.

## Running locally (Mac)

```bash
cd sms-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Open http://localhost:5050. Go to **Settings**, fill in the phone's address/port/
username/password (from the SMS Gateway app's Home screen), click **Test
connection**, then **Save settings**.

Data is stored in `data/dashboard.db` (SQLite) — safe to delete to reset everything.

## Preparing your CSV

- One row per recipient.
- Include a phone number column (any of `phone`, `mobile`, `tel`, `msisdn`, etc.
  are auto-detected; you can also pick manually).
- Any other column can be referenced in your template as `{column_name}`.
- Numbers without a `+` country code are expanded using the **Default country
  code** you set in Settings (e.g. `254` turns `0712345678` into
  `+254712345678`). Set this before uploading real recipient lists.
- Grab **Download a sample CSV** from the Compose tab for the expected shape.

## Deploying on a Raspberry Pi

1. Copy this folder to the Pi (`scp -r sms-dashboard pi@raspberrypi.local:~/`).
2. On the Pi:
   ```bash
   cd sms-dashboard
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt gunicorn
   ```
3. Run it with a production server instead of Flask's dev server:
   ```bash
   venv/bin/gunicorn -w 2 -b 0.0.0.0:5050 app:app
   ```
4. (Optional) Run it as a systemd service so it survives reboots. Create
   `/etc/systemd/system/dispatch.service`:
   ```ini
   [Unit]
   Description=Dispatch SMS mail-merge console
   After=network.target

   [Service]
   User=pi
   WorkingDirectory=/home/pi/sms-dashboard
   ExecStart=/home/pi/sms-dashboard/venv/bin/gunicorn -w 2 -b 0.0.0.0:5050 app:app
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now dispatch
   ```
5. Visit `http://<pi-ip>:5050` from any device on the same network (or
   Tailscale) to use the dashboard.

If the Pi and the Android phone are both on your Tailscale network, use the
phone's Tailscale IP/hostname as the gateway address in Settings — this also
lets you dispatch from anywhere, not just your home LAN.

## Notes on the gateway API

- Uses the SMS Gateway **Local Server** API (`POST /message`, `GET
  /message/{id}`), Basic Auth over HTTP by default. If you've enabled HTTPS on
  the device, check "Use HTTPS" in Settings.
- Delivery reports (`withDeliveryReport`) are requested by default so **Refresh
  delivery status** can update `sent` → `delivered`/`failed` after the initial
  send.
- The gateway itself may apply its own rate limits/rules (see the app's own
  Settings → Messages screen) — the dashboard's delay/batch settings work
  alongside those, not instead of them.

## SMS dispatch backend

Settings has an **SMS dispatch backend** picker (Android Gateway / Huawei
modem) that decides which of the two actually sends campaign messages, the
test-send box, and `/api/test-send` — not just internally; it's the toggle
between `gateway.py` (Android) and `modem_gateway.py` (this modem), which
present the same `send_message()`/`test_connection()` shape so the rest of
the app (`dispatcher.py`, `app.py`) doesn't care which one is active.

A few things behave differently by design between the two:
- **Sending through the modem is asynchronous under the hood** — the app and
  the modem daemon are separate processes talking only through the
  `modem_commands` table (see the Architecture section below), so a modem
  send blocks the dispatch worker thread polling for up to 45s waiting for
  the daemon to actually key `AT+CMGS`. This never blocks the dashboard
  itself (each campaign dispatches on its own background thread), but a
  stuck/offline daemon will make that recipient time out rather than fail
  instantly the way an unreachable Android Gateway does.
- **Delivery-report tracking uses a different correlation key per backend.**
  The Android Gateway gives one opaque message id per send; the modem gives
  one TP-MR value per PDU segment (see `modem_sms_refs`). "Refresh delivery
  status" reads whichever one applies to how a given recipient was actually
  sent — a recipient sent via the gateway is safely skipped when refreshing
  under modem settings and vice versa, so switching backends mid-campaign
  doesn't corrupt older recipients' tracking, it just means only recipients
  sent after the switch get refreshed until you switch back.
- **The modem can only dial one recipient per send** (`AT+CMGS` has no batch
  equivalent) — irrelevant in practice since the dispatcher already sends one
  recipient at a time either way.
- The Modem tab's Inbox **Reply** button always sends via the modem
  regardless of this setting — you're replying on the SIM that received the
  message, so that's the only backend that makes sense there.

## Modem (Huawei MU509) — cellular backup internet + SMS routing

A **separate daemon** (`modem/manager.py`, run via `run_modem_manager.py`) owns
the serial ports and the GPIO power pin, brings the modem up reliably on boot,
handles SMS in PDU mode (send with delivery-report tracking and automatic
multi-segment splitting; receive with multi-part reassembly), and supervises a
PPP internet connection on request. Campaign dispatch can send through either
this modem or the Android gateway - see "SMS dispatch backend" below.

### Architecture

A **separate daemon** (`modem/manager.py`, run via `run_modem_manager.py`) owns
the serial ports and the GPIO power pin. It is a completely different OS
process from the Flask dashboard and is never imported by it. The two only
ever communicate through the shared SQLite database:

- `modem_status` — current state (device present, SIM, signal, PPP state/IP, ...), written by the daemon, read by Flask.
- `modem_events` — an append-only log (URCs, power-cycles, PPP retries, daemon activity), written by the daemon, read by Flask.
- `modem_commands` — action requests (power-cycle, reconnect, send SMS, send USSD, end USSD session), written by Flask when you click a button, picked up and executed by the daemon within ~1.5s.
- `modem_inbox` / `modem_sms_refs` — received messages and per-segment delivery-report tracking for sent ones.

Flask's `/api/modem/*` routes only ever read those tables or insert a command
row — there is no serial or GPIO code anywhere in `app.py`. `modem_gateway.py`
(project root, alongside `gateway.py`) is the one place outside the daemon
that touches `modem_commands` directly — it's what `dispatcher.py`/`app.py`
call when the SMS dispatch backend is set to "modem" (see above), enqueuing a
`send_sms` command and polling `modem_commands` for the daemon's result the
same way the Reply button's `/api/modem/send` does, just synchronously from
the caller's point of view.

Inside `modem/`:
- `serial_at.py` / `serial_transport.py` — generic Hayes AT command engine and the underlying serial I/O it runs on (fully reusable, knows nothing about SMS/PPP specifically).
- `gpio_power.py` — pulses the PWRKEY line through a transistor switch.
- `ports.py` — one-off "is anything AT-capable on this device file" probe, used by the presence watchdog.
- `ppp.py` — dials the *data* port into a PPP session via `pppd`+`chat`, supervises it, retries with backoff.
- `pdu.py` — encodes/decodes the GSM PDU wire format (GSM7/UCS2, concatenation UDH) — no AT or I/O knowledge, pure codec.
- `sms.py` — the AT-command side of SMS: `AT+CNMI` configuration (with the mode/ds fallback described below), `AT+CMGS`/`AT+CMGL`/`AT+CMGR`/`AT+CMGD`, built on top of `pdu.py`.
- `text_codec.py` — shared hex/7-bit text decoding helpers used by both SMS and USSD reply parsing.
- `ussd.py` — `AT^USSDMODE`/`AT+CUSD` session handling.
- `backoff.py` — the exponential-backoff-with-jitter helper shared by the power-on watchdog and the PPP supervisor.
- `manager.py` — orchestrates all of the above as one state machine.

### Hardware wiring — GPIO power control

The MU509 needs its PWRKEY pin pulled to **ground for ≥500ms** to power on.
Default pin: **BCM GPIO17 (physical pin 11)**. Wire it through a small NPN
transistor (e.g. 2N3904) or logic-level N-MOSFET (e.g. 2N7000) — **do not**
wire the Pi's GPIO directly to the module's PWRKEY pin:

```
Pi GPIO17 (pin 11) --[1k ohm resistor]--> transistor base
Pi GND (pin 9)     ------------------------> transistor emitter
modem PWRKEY pin   ------------------------> transistor collector
```

Idle state is GPIO LOW (transistor off, PWRKEY left floating/pulled up by the
module per its datasheet); a power-on pulse drives GPIO HIGH for ~600ms. The
Pi and modem must share a common ground.

If you wire it to a different pin, set it in Settings → Modem → "GPIO power
pin" — nothing else needs to change.

The daemon only ever pulses this pin when the control port stops answering
AT commands, so it self-heals if the module crashes or loses power
momentarily, without you needing to SSH in.

### One-time setup on the Pi

```bash
cd sms-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-modem.txt   # pyserial, gpiozero, RPi.GPIO — Pi only

sudo apt update
sudo apt install ppp                     # provides pppd + chat

# group membership so the daemon can run as a normal user, not root:
sudo usermod -aG dialout,dip,gpio pi     # serial ports, pppd, GPIO
# log out/in (or reboot) for group changes to take effect
```

Install the systemd services (adjust paths/user in the unit files first if
your username or install path differs from `pi` / `/home/pi/sms-dashboard`):

```bash
sudo cp deploy/modem-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now modem-manager
```

Check it's alive:
```bash
sudo systemctl status modem-manager
journalctl -u modem-manager -f
```

### Because the Pi is currently offline

Since there's no Ethernet/Tailscale path available yet, you'll need **local
access** (HDMI+keyboard, or pull the SD card and chroot/edit) to do this
initial install and confirm the modem daemon actually brings up an IP and
that Tailscale comes up over it, before relying on remote access. Once
`modem-manager` is enabled via systemd, it starts automatically on every
boot from then on — no manual step required after a power cycle.

A sane bring-up order to minimize risk:
1. With local access, do the setup above but leave `modem-manager` **stopped**.
2. Run it once in the foreground so you can watch it directly:
   `venv/bin/python3 run_modem_manager.py`
3. Confirm in the output (or `tail -f` the dashboard, once you can reach it
   locally) that the device is detected and AT init succeeds. At this point
   PPP will deliberately **not** connect yet — see Auto-connect above; that's
   expected, not a fault. Authorize it now, either by ticking Settings →
   Modem → Auto-connect and saving, or by texting `connect` to the SIM from
   another phone, then confirm PPP comes up with an IP.
4. Confirm Tailscale actually comes up and you can reach the Pi over it from
   another device.
5. Only then `sudo systemctl enable --now modem-manager` for it to survive
   reboots, and disconnect local access.

### Verifying which ttyUSB is which

The daemon assumes `/dev/ttyUSB0` = AT control port, `/dev/ttyUSB1` = PPP
data port (both configurable in Settings). If your unit enumerates
differently, confirm locally first:
```bash
for p in /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyUSB2; do
  echo "== $p =="; sudo timeout 2 bash -c "echo -e 'AT\r' > $p; cat < $p" ; echo
done
```
Whichever ports answer `OK` are AT-capable; **do not** point the PPP data
port at the same one you're using for AT control — they must be different
device files, since dialing puts that port into PPP framing mode.

### Settings → Modem fields

- **AT control port / PPP data port** — the two `/dev/ttyUSBx` device files (must differ).
- **APN** — from your SIM/carrier (M2M SIMs usually need this explicitly; consumer SIMs may default to `internet`).
- **PPP username/password** — leave blank unless your carrier requires PAP/CHAP auth for the APN.
- **SIM PIN** — only if the SIM is PIN-locked.
- **GPIO power pin** — must match your wiring.
- **Auto-connect** — whether the daemon dials PPP on its own. **Defaults to
  off**: the daemon never dials out just because it started, or because the
  modem power-cycled and the control channel came back up — only because you
  explicitly asked it to, via one of:
  - ticking this checkbox on and saving Settings,
  - clicking **Reconnect internet now** on the Modem tab, or
  - texting **`connect`** (exact text, case-insensitive) to the SIM from any
    phone.

  Any of those three flips this setting to **on** and stays on (auto-redialing
  through drops) until you turn it off again — it's the same persisted value
  either way, not a one-off action. Turn it off if you want the modem
  powered/AT-ready but not consuming data.

  Upgrading from an older install: this setting used to default to **on**.
  That default only applies to a brand-new `data/dashboard.db` — if you
  already have one, its stored value is still `1` regardless of this change,
  so untick it once in Settings → Modem if you want the new "off until asked"
  behavior.

### Modem tab

Shows live status (device presence, AT readiness, SIM status, signal
quality, registration/operator, PPP state + IP, retry countdown), a live log
of daemon activity and raw unsolicited events, and two manual controls:
"Power-cycle modem" (forces a PWRKEY pulse) and "Reconnect internet now"
(authorizes + redials immediately, resetting backoff — see Auto-connect
above for what "authorizes" means). Both just insert a row into
`modem_commands` — the daemon picks it up within ~1.5s.

### SMS (send / receive) — PDU mode

Runs in AT **PDU mode** (`AT+CMGF=0`), not text mode — this is what makes
delivery reports and correct UCS2/multi-segment handling possible, neither of
which text mode supports. `AT+CNMI` is set to `1,1,0,<ds>,0`: mode=1 and
ds=1 are the module's own documented values for "store incoming and notify
via `+CMTI`, report delivery status directly via `+CDS`" (see
`modem/sms.py`'s module docstring for why mode=2 doesn't work on this
module despite `AT+CNMI=?` advertising it, and why `ds=2` is skipped
entirely — the module's `"SR"` status-report storage isn't supported, so
that mode would silently lose every delivery report).

**Receiving:** a `+CMTI` URC (or a 20-second fallback sweep) triggers
`AT+CMGL=4` to pull everything off the SIM. A normal single-part message is
copied into `modem_inbox` and deleted off the SIM immediately (SIM storage is
tiny — ~40 messages — and the DB is the durable copy). A message that's one
part of a **concatenated (multi-part) SMS** is held back instead: its SIM
slot is deliberately *not* deleted until every part of the set has arrived,
at which point they're merged in order into a single `modem_inbox` row and
all their SIM slots are cleared together. If a part never shows up, the set
is delivered anyway after 5 minutes with `[missing part N/M]` markers spliced
in, rather than holding the SIM slot forever.

**Sending:** `AT+CMGS` in PDU mode, with automatic splitting into multiple
segments for long messages and delivery-report tracking end to end
(`modem_sms_refs` records each segment's message reference and status,
updated as `+CDS` reports arrive).

The Modem tab's **Inbox** card lists messages with **Reply** (opens a quick
compose box, sends via the same `send_sms` primitive the future
campaign-dispatch-via-modem integration will reuse) and **Delete** (removes
the DB copy — the SIM copy is already gone by the time you see it here).

**Texting `connect`** to the SIM from any phone brings up the internet
connection the same way clicking **Reconnect internet now** does — see
Auto-connect above.

### USSD

Uses `AT^USSDMODE=0` (non-transparent) + `AT+CUSD=1,"<code>",15`. The
network's reply always arrives asynchronously as a `+CUSD:` unsolicited
line — never as the AT command's own response — so `modem/ussd.py` includes
a small waiter that the URC callback feeds and a command handler blocks on
(with a 30s timeout) until the reply shows up, the same way you'd watch a
USSD popup appear on a phone.

Session state (`<m>` in the spec): `0` = done, `1` = network wants a reply
(the Modem tab's USSD box turns into a reply field and shows an "End
session" button), `2`/`4`/`5` = network-side error/unsupported/timeout.
Response text decoding is best-effort: `dcs=15` is plain text; UCS2 replies
(hex-encoded) are detected and decoded, with a sanity check that rejects
decoding attempts that produce mostly non-printable garbage, falling back to
showing the raw text rather than guessing wrong.

Known edge case: manually power-cycling the modem while a USSD (or SMS)
request is still waiting on a reply closes the serial reader thread that
wait depends on, so that in-flight request will simply time out rather than
ever completing — rare in practice, and self-healing (no crash, no
deadlock), so not worth more machinery for now.

### Troubleshooting

- **Nothing shows in the Modem tab** — the daemon isn't running or hasn't
  written a status row yet; check `systemctl status modem-manager` /
  `journalctl -u modem-manager`.
- **`device_present` stays 0 and GPIO keeps pulsing** — check wiring/common
  ground, and confirm the control port setting matches the port that
  actually answers `AT` (see "Verifying which ttyUSB is which" above).
- **PPP state just stays `down` and nothing happens, no error shown** — this
  is expected, not a fault: Auto-connect defaults to off, so the daemon
  won't dial on its own. Tick it on in Settings → Modem, click "Reconnect
  internet now", or text `connect` to the SIM. See Auto-connect above.
- **PPP keeps failing / `ppp_last_error` shows a pppd exit code** — check
  the APN is correct, `pppd`/`chat` are installed (`which pppd chat`), and
  that the daemon's user is in the `dialout`/`dip` groups.
- **"pppd not found"** — `sudo apt install ppp`.
- **SIM shows `pin_required`** — set the PIN in Settings → Modem.



- Credentials are stored in plaintext in the local SQLite file — this tool is
  designed for a trusted local/Tailscale network, not the public internet.
- Don't expose port 5050 directly to the internet without adding
  authentication in front of it (e.g. a reverse proxy with basic auth, or keep
  it Tailscale-only).
