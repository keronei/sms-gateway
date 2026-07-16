"""
gateway.py - thin client for the android-sms-gateway "Local Server" API.

Docs: https://docs.sms-gate.app/getting-started/local-server/
  POST  /message         send an SMS (Basic Auth)
  GET   /message/{id}    poll delivery state
  GET   /health          liveness check (used for "Test connection")
"""
import requests

DEFAULT_TIMEOUT = 15


class GatewayError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _base_url(settings):
    scheme = "https" if settings.get("use_https") else "http"
    address = settings.get("address", "").strip()
    port = settings.get("port")
    if not address:
        raise GatewayError("Gateway address is not configured.")
    if port:
        return f"{scheme}://{address}:{port}"
    return f"{scheme}://{address}"


def _auth(settings):
    return (settings.get("username") or "", settings.get("password") or "")


def test_connection(settings):
    """Try to reach the device. Returns dict {ok, message}."""
    base = _base_url(settings)
    try:
        resp = requests.get(f"{base}/health", auth=_auth(settings), timeout=8)
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "message": "Connection timed out. Check the address/port and that the device is reachable (LAN or Tailscale)."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": "Could not connect. Is the Local Server running on the phone and on the same network?"}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "message": f"Request failed: {e}"}

    if resp.status_code == 401:
        return {"ok": False, "message": "Connected, but the username/password was rejected (401)."}
    if resp.status_code >= 500:
        return {"ok": False, "message": f"Device responded with a server error ({resp.status_code})."}
    # 200/404 both indicate the device answered on that host:port; 404 just means
    # this particular build has no /health route, which is fine.
    return {"ok": True, "message": "Device reachable and credentials accepted." if resp.status_code != 401 else "Device reachable."}


def send_message(settings, phone_numbers, text):
    """Send one message to one or more phone numbers. Returns the gateway's JSON response."""
    base = _base_url(settings)
    payload = {
        "textMessage": {"text": text},
        "phoneNumbers": phone_numbers,
    }
    if settings.get("sim_number") not in (None, "", 0):
        try:
            payload["simNumber"] = int(settings["sim_number"])
        except (TypeError, ValueError):
            pass
    if "with_delivery_report" in settings:
        payload["withDeliveryReport"] = bool(settings["with_delivery_report"])

    try:
        resp = requests.post(
            f"{base}/message",
            json=payload,
            auth=_auth(settings),
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise GatewayError(f"Network error contacting gateway: {e}")

    if resp.status_code == 401:
        raise GatewayError("Authentication rejected (401). Check username/password.", 401)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        raise GatewayError(f"Gateway returned {resp.status_code}: {detail}", resp.status_code)

    try:
        return resp.json()
    except ValueError:
        raise GatewayError("Gateway returned a non-JSON response.")


def get_message_state(settings, message_id):
    base = _base_url(settings)
    try:
        resp = requests.get(f"{base}/message/{message_id}", auth=_auth(settings), timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise GatewayError(f"Network error contacting gateway: {e}")
    if resp.status_code >= 400:
        raise GatewayError(f"Gateway returned {resp.status_code} while checking status.", resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise GatewayError("Gateway returned a non-JSON response.")
