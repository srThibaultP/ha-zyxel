"""zysh-cgi client for Zyxel NWA standalone access points (experimental)."""
from __future__ import annotations

import ast
from html.parser import HTMLParser
import re
from threading import Lock
from urllib.parse import urlsplit

import hashlib
import ssl
import warnings

import requests
from requests.adapters import HTTPAdapter

from .api import ZyxelAuthenticationError, ZyxelConnectionError

# Only commands confirmed against the device UI belong in this list.
COMMANDS = (
    "show version",
    "show wlan all",
    "show wireless-hal current channel",
    "show wireless-hal statistic",
    "show fqdn",
    "show nebula cloud status",
    "show wireless-hal station number",
    "show wireless-hal station info",
    "show wlan-ssid-profile all",
    "show cpu status",
    "show mem status",
    "show system uptime",
    "show port status",
    "show led_suppress status",
    "show led_locator status",
)
MAX_RESPONSE = 2_000_000
# Models that share the NWA standalone web UI and zysh-cgi API.
SUPPORTED_MODELS = {"NWA50AX", "NWA50BE", "NWA130BE"}
SLOT_BANDS = {"1": "radio_2g", "2": "radio_5g", "3": "radio_6g"}
BAND_SLOTS = {band: f"slot{number}" for number, band in SLOT_BANDS.items()}
MAX_SSIDS_PER_RADIO = 8
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,31}")


class LoginForm(HTMLParser):
    """Read the CSRF token without retaining password fields."""
    def __init__(self):
        super().__init__()
        self.csrf = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "CSRFToken":
            self.csrf = attrs.get("value", "")


def parse_response(text):
    """Parse data literals, never execute JavaScript received from a device."""
    if len(text) > MAX_RESPONSE:
        raise ZyxelConnectionError("AP response exceeds size limit")
    result = {}
    # Responses contain one assignment per line; literals may contain semicolons.
    for line in text.splitlines():
        match = re.fullmatch(r"\s*var\s+(zyshdata\d+|errno\d+|errmsg\d+)\s*=\s*(.*?)\s*;\s*", line)
        if not match:
            continue
        try:
            result[match[1]] = ast.literal_eval(match[2])
        except (SyntaxError, ValueError, RecursionError) as err:
            raise ZyxelConnectionError("Unsupported AP response format") from err
    if not any(key.startswith("zyshdata") for key in result):
        raise ZyxelConnectionError("No zysh status data in AP response")
    return result


def _leading_int(value):
    """Return the number starting a value such as '41%', or None."""
    match = re.match(r"\s*(-?\d+)", value) if isinstance(value, str) else None
    return int(match[1]) if match else None


def _uptime_seconds(value):
    """Convert an uptime such as '6 days 08:37:53' to seconds, or None."""
    match = re.fullmatch(r"\s*(?:(\d+)\s+days?\s+)?(\d+):(\d+):(\d+)\s*", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _client(station):
    """Keep the useful fields of one connected WiFi client."""
    return {
        "mac": station.get("_MAC"),
        "ip": station.get("_IPv4"),
        "ssid": station.get("_Display_SSID") or station.get("_SSID"),
        "band": station.get("_Band"),
        "signal_dbm": _leading_int(station.get("_RSSI_dBm")),
        "tx_rate": station.get("_TxRate"),
        "rx_rate": station.get("_RxRate"),
        "capability": station.get("_Capability"),
        "connected_since": station.get("_Time"),
    }


def _apply_radio_config(result, slots, profiles):
    """Add the radio state, SSID names and SSID positions to the result."""
    names = {profile.get("__name"): profile.get("_SSID") for profile in profiles
             if isinstance(profile, dict)}
    assignments = {}
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_name = str(slot.get("__name", ""))
        band = SLOT_BANDS.get(slot_name.removeprefix("slot"))
        if band is None:
            continue
        radio = result["radios"].setdefault(band, {})
        radio["Active"] = slot.get("_Activate") == "yes"
        ssids = []
        for position in range(1, MAX_SSIDS_PER_RADIO + 1):
            profile = slot.get(f"_SSID_profile_{position}")
            if not profile:
                continue
            ssid = names.get(profile) or profile
            assignments[f"{slot_name}:{position}"] = {"profile": profile, "ssid": ssid}
            if radio["Active"] and ssid not in ssids:
                ssids.append(ssid)
        if profiles:
            # Home Assistant limits a state to 255 characters.
            radio["SSIDs"] = ", ".join(ssids)[:255] or None
    if profiles:
        result["ssid_slots"] = assignments


def normalize_status(parsed):
    """Expose only explicitly supported device and radio values."""
    result = {"device": {}, "radios": {}, "clients": []}
    slots, profiles = [], []
    fields = {"_model": "model", "_firmware_version": "firmware",
              "_host_name": "name", "_IP_Address": "ip_address",
              "_Nebula_Cloud_status": "nebula_status",
              "_Zyxel_Cloud": "nebula_status"}  # NWA50BE firmware V7.30 name
    numbers = {"_wlanReceivedByte": "BytesReceived", "_wlanTransmittedByte": "BytesSent",
               "_ReceivedPktCount": "PacketsReceived", "_TransmittedPktCount": "PacketsSent",
               "_TxPower": "TxPower", "_Channel_Utilization": "ChannelUtilization"}
    for key, rows in parsed.items():
        if not key.startswith("zyshdata") or not isinstance(rows, list):
            continue
        index = key[len("zyshdata"):]
        if parsed.get("errno" + index) != 0:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for source, target in fields.items():
                value = row.get(source)
                if isinstance(value, str):
                    result["device"][target] = value
            for slot, band in SLOT_BANDS.items():
                value = row.get("_Slot" + slot)
                if isinstance(value, str):
                    # Same field in two commands: a channel reads '9 (20 MHz)',
                    # a client count from "station number" is a bare number.
                    target = "Clients" if value.strip().isdigit() else "Channel"
                    radio = result["radios"].setdefault(band, {})
                    radio[target] = int(value) if target == "Clients" else value
            for radio in row.get("_Slot", []):
                if not isinstance(radio, dict):
                    continue
                band = SLOT_BANDS.get(radio.get("__name"))
                if band is None:
                    continue
                values = result["radios"].setdefault(band, {})
                for source, target in numbers.items():
                    try:
                        value = int(radio[source])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if value >= 0:
                        values[target] = value
            for source, target in (("_CPU_utilization", "cpu_usage"),
                                   ("_memory_usage", "memory_usage")):
                value = _leading_int(row.get(source))
                if value is not None:
                    result["device"][target] = value
            if isinstance(row.get("_system_uptime"), str):
                value = _uptime_seconds(row["_system_uptime"])
                if value is not None:
                    result["device"]["uptime"] = value
            for source, target in (("_Suppress_Mode_Status", "led_suppress"),
                                   ("_Locator_LED_Status", "led_locator")):
                if isinstance(row.get(source), str):
                    result["device"][target] = row[source]
            if row.get("_Port") == "1":
                port = result.setdefault("port", {})
                if isinstance(row.get("_Status"), str):
                    port["LinkStatus"] = row["_Status"]
                for source, target in (("_RxBytes", "BytesReceived"), ("_TxBytes", "BytesSent")):
                    value = _leading_int(row.get(source))
                    if value is not None and value >= 0:
                        port[target] = value
            result["clients"].extend(
                _client(station) for station in row.get("_index", [])
                if isinstance(station, dict) and "_MAC" in station)
            slots.extend(row.get("_slot", []))
            profiles.extend(row.get("_ssid_profile", []))
    _apply_radio_config(result, slots, profiles)
    counts = [radio["Clients"] for radio in result["radios"].values() if "Clients" in radio]
    if counts:
        result["device"]["clients"] = sum(counts)
    model = result["device"].get("model", "").strip().upper()
    if model not in SUPPORTED_MODELS:
        raise ZyxelConnectionError(
            f"Unsupported access point model {model!r}; supported: {sorted(SUPPORTED_MODELS)}")
    return result


def fetch_cert_fingerprint(host):
    """Return the SHA-256 fingerprint of the certificate an HTTPS host presents.

    NWA access points use a self-signed certificate issued to a device name, not
    the IP, so normal validation always fails. Callers record this fingerprint
    when the device is added and pin it afterwards.
    """
    parsed = urlsplit(host)
    try:
        pem = ssl.get_server_certificate((parsed.hostname, parsed.port or 443), timeout=10)
    except OSError:
        raise ZyxelConnectionError("Could not read the AP's HTTPS certificate") from None
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


class _PinnedAdapter(HTTPAdapter):
    """Accept exactly one certificate, identified by its SHA-256 fingerprint."""
    def __init__(self, fingerprint):
        self._fingerprint = fingerprint
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        # urllib3 warns about verify=False even though the pin is enforced.
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        # The CA/hostname check can never pass for this self-signed cert; the
        # fingerprint check above replaces it and still rejects any other cert.
        kwargs["verify"] = False
        return super().send(request, **kwargs)


class NWA50AXClient:
    """One isolated session per AP; bounded retries and serialized requests."""
    def __init__(self, host, username, password, cert_fingerprint=None):
        parsed = urlsplit(host)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ZyxelConnectionError("Host must be an HTTP(S) origin without a path")
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.trust_env = False
        self.cert_fingerprint = cert_fingerprint
        if cert_fingerprint:
            self.session.mount("https://", _PinnedAdapter(cert_fingerprint))
        self.sessionkey = None
        self._lock = Lock()

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.host + path, timeout=(5, 10),
                                            allow_redirects=False, **kwargs)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE:
                raise ZyxelConnectionError("AP response exceeds size limit")
            return response
        except requests.RequestException:
            # Avoid exposing credentials, tokens or response bodies in logs.
            raise ZyxelConnectionError("AP HTTP request failed") from None

    def _follow_https_redirect(self, location):
        """Accept only an HTTP -> HTTPS upgrade on the same host (NWA50BE does this)."""
        current, target = urlsplit(self.host), urlsplit(location)
        if (current.scheme != "http" or target.scheme != "https"
                or target.hostname != current.hostname or target.path not in ("", "/")):
            raise ZyxelConnectionError("AP redirected the login page unexpectedly")
        self.host = f"https://{target.netloc}"
        if not self.cert_fingerprint:
            raise ZyxelConnectionError(
                f"AP requires HTTPS; use {self.host} so its certificate can be pinned")

    def _login(self):
        self.sessionkey = None
        self.session.cookies.clear()
        page = self._request("GET", "/")
        if page.status_code in (301, 302, 303, 307, 308):
            self._follow_https_redirect(page.headers.get("Location", ""))
            page = self._request("GET", "/")
        form = LoginForm()
        form.feed(page.text)
        if form.csrf is None:
            raise ZyxelConnectionError("AP login page has no CSRFToken field")
        response = self._request("POST", "/", data={
            "username": self.username, "pwd": self.password,
            "password": self.password, "CSRFToken": form.csrf,
            "fake_safari_username": "", "fake_safari_password": "",
        })
        if response.status_code not in (302, 303) or not any(
                cookie.name == "authtok" and cookie.value for cookie in self.session.cookies):
            raise ZyxelAuthenticationError("AP rejected login")
        self.sessionkey = True

    def fetch_status(self):
        with self._lock:
            for attempt in range(2):
                if not self.sessionkey:
                    self._login()
                response = self._request("POST", "/cgi-bin/zysh-cgi", data=[
                    ("filter", "js2"), *[("cmd", command) for command in COMMANDS], ("write", "0")])
                if response.status_code in (301, 302, 303, 307, 308) or "name=\"CSRFToken\"" in response.text:
                    self.sessionkey = None
                    if attempt == 0:
                        continue
                    raise ZyxelAuthenticationError("AP session expired after login")
                return normalize_status(parse_response(response.text))
            raise ZyxelConnectionError("AP returned no data")

    def run_config(self, commands):
        """Run configuration commands the way the AP web interface does.

        The interface sends `wlan slotN`, the change and `exit` with no
        `configure terminal`, which makes the AP answer with an empty body.
        write=0 applies the change to the running configuration.
        """
        with self._lock:
            for attempt in range(2):
                if not self.sessionkey:
                    self._login()
                response = self._request("POST", "/cgi-bin/zysh-cgi", data=[
                    ("filter", "js2"), *[("cmd", command) for command in commands],
                    ("write", "0")])
                if response.status_code in (301, 302, 303, 307, 308) or "name=\"CSRFToken\"" in response.text:
                    self.sessionkey = None
                    if attempt == 0:
                        continue
                    raise ZyxelAuthenticationError("AP session expired after login")
                parsed = parse_response(response.text)
                for key, value in parsed.items():
                    if key.startswith("errno") and value != 0:
                        raise ZyxelConnectionError("AP refused the command")
                return
            raise ZyxelConnectionError("AP returned no data")

    def set_radio(self, slot, active):
        """Turn one radio on or off."""
        self._check_slot(slot)
        self.run_config([f"wlan {slot}", "activate" if active else "no activate", "exit"])

    def set_ssid(self, slot, position, profile):
        """Put an SSID profile on a radio, or remove it when profile is None."""
        self._check_slot(slot)
        if not 1 <= position <= MAX_SSIDS_PER_RADIO:
            raise ZyxelConnectionError("Invalid SSID position")
        if profile is None:
            command = f"no ssid profile {position}"
        elif PROFILE_NAME.fullmatch(profile):
            command = f"ssid profile {position} {profile}"
        else:
            raise ZyxelConnectionError("Invalid SSID profile name")
        self.run_config([f"wlan {slot}", command, "exit"])

    def set_led_locator(self, active):
        """Start or stop the blinking locator LED."""
        self.run_config([f"led_locator {'on' if active else 'off'}"])

    @staticmethod
    def _check_slot(slot):
        if slot not in BAND_SLOTS.values():
            raise ZyxelConnectionError("Invalid radio slot")

    def close(self):
        with self._lock:
            self.session.close()
