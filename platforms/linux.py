"""Ubuntu/Linux backend using NetworkManager's nmcli command."""

import shutil
import subprocess
import re
import time

from wifi_core import AP, ConnectionInfo, ScanSnapshot, deduplicate_aps


def _split_terse(line):
    """Split nmcli's colon-delimited output while preserving escaped colons."""
    fields, value, escaped = [], [], False
    for char in line.rstrip("\n"):
        if escaped:
            value.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(value))
            value = []
        else:
            value.append(char)
    if escaped:
        value.append("\\")
    fields.append("".join(value))
    return fields


def _band_and_channel(freq, channel):
    if 2400 <= freq < 2500:
        return 2, channel
    if 4900 <= freq < 5900:
        return 5, channel
    if 5925 <= freq < 7125:
        return 6, channel
    return (2 if channel <= 14 else 5), channel


RESCAN_INTERVAL_SECONDS = 5.0
_last_rescan = 0.0


def _parse(output):
    aps = []
    connection = ConnectionInfo()
    for line in output.splitlines():
        fields = _split_terse(line)
        if len(fields) != 8 or not fields[3].isdigit():
            continue
        active, ssid, bssid, channel, freq, signal, security, _advertised_rate = fields
        channel = int(channel)
        frequency = re.search(r"\d+", freq)
        freq = int(frequency.group()) if frequency else 0
        band, channel = _band_and_channel(freq, channel)
        # nmcli reports a percentage; this conversion is necessarily approximate.
        rssi = round(int(signal or 0) / 2 - 100)
        current = active.casefold() in {"yes", "true", "예"}
        ap = AP(ssid or None, bssid or None, channel, 20, band, rssi, None,
                security, "", current, frequency=freq, width_known=False)
        aps.append(ap)
        if current:
            connection = ConnectionInfo(
                connected=True, ssid=ap.ssid, bssid=ap.bssid, channel=channel,
                width=None, band=band, rssi=rssi, security=security,
                # nmcli's Wi-Fi-list RATE is the BSS advertised capability,
                # not the currently negotiated client transmit rate.
                tx_rate=None,
            )
    return aps, connection


def snapshot():
    global _last_rescan
    if not shutil.which("nmcli"):
        raise RuntimeError("nmcli를 찾을 수 없습니다. Ubuntu에서는 NetworkManager를 설치하고 실행하세요.")
    now = time.monotonic()
    rescan = "yes" if now - _last_rescan >= RESCAN_INTERVAL_SECONDS else "no"
    result = subprocess.run(
        ["nmcli", "--terse", "--escape", "yes", "--fields",
         "ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY,RATE", "device", "wifi", "list", "--rescan", rescan],
        capture_output=True, text=True, timeout=45, check=False,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "nmcli 스캔 실패"
        raise RuntimeError(message)
    if rescan == "yes":
        _last_rescan = time.monotonic()
    aps, connection = _parse(result.stdout)
    return ScanSnapshot(deduplicate_aps(aps), connection, SOURCE,
                        warnings=("nmcli는 채널 폭과 noise를 제공하지 않습니다.",))


def scan():
    return snapshot().aps


SOURCE = "NetworkManager nmcli (5초 재스캔)"
