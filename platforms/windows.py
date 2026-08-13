"""Windows backend using the built-in netsh WLAN command."""

import re
import subprocess

from wifi_core import AP

_SSID = re.compile(r"^\s*SSID\s+\d+\s*:\s*(.*)$", re.I)
_BSSID = re.compile(r"^\s*BSSID\s+\d+\s*:\s*([0-9a-f:.-]+)", re.I)
_CHANNEL = re.compile(r"^\s*(?:Channel|채널)\s*:\s*(\d+)", re.I)
_SIGNAL = re.compile(r"^\s*(?:Signal|신호)\s*:\s*(\d+)%", re.I)
_AUTH = re.compile(r"^\s*(?:Authentication|인증)\s*:\s*(.*)$", re.I)


def scan():
    result = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                            capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "netsh WLAN 스캔 실패")
    aps, ssid, security, current = [], None, "", None

    def append_current():
        nonlocal current
        if not current or not current["channel"]:
            return
        channel = current["channel"]
        aps.append(AP(ssid, current["bssid"], channel, 20, 2 if channel <= 14 else 5,
                      round(current["signal"] / 2 - 100), None, security))
        current = None

    for raw in result.stdout.splitlines() + [""]:
        if match := _SSID.match(raw):
            append_current()
            ssid, security, current = match.group(1).strip() or None, "", None
        elif match := _AUTH.match(raw):
            security = match.group(1).strip()
        elif match := _BSSID.match(raw):
            append_current()
            current = {"bssid": match.group(1), "channel": None, "signal": 0}
        elif current and (match := _CHANNEL.match(raw)):
            current["channel"] = int(match.group(1))
        elif current and (match := _SIGNAL.match(raw)):
            current["signal"] = int(match.group(1))
        elif not raw.strip():
            append_current()
    return aps


SOURCE = "Windows netsh WLAN"
