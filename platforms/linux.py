"""Ubuntu/Linux backend using NetworkManager's nmcli command."""

import shutil
import subprocess
import re
import time
from concurrent.futures import ThreadPoolExecutor

from wifi_core import AP, ConnectionInfo, ScanSnapshot, deduplicate_aps, frequency_to_channel
from platforms.commands import run_command


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
        if len(fields) not in {8, 9} or not fields[3].isdigit():
            continue
        active, ssid, bssid, channel, freq, signal, security, _advertised_rate = fields[:8]
        if not signal.isdigit():
            continue
        interface = fields[8] if len(fields) == 9 else ""
        if interface and not connection.interface:
            connection.interface = interface
        channel = int(channel)
        frequency = re.search(r"\d+", freq)
        freq = int(frequency.group()) if frequency else 0
        band, channel = _band_and_channel(freq, channel)
        # nmcli reports a percentage; this conversion is necessarily approximate.
        rssi = round(min(100, int(signal)) / 2 - 100)
        current = active.casefold() in {"yes", "true", "예"}
        ap = AP(ssid or None, bssid.lower().replace("-", ":") or None, channel, 20, band, rssi, None,
                security, "", current, frequency=freq or None, width_known=False, rssi_estimated=True)
        aps.append(ap)
        if current and not connection.connected:
            connection = ConnectionInfo(
                connected=True, ssid=ap.ssid, bssid=ap.bssid, channel=channel,
                width=None, band=band, rssi=rssi, security=security,
                # nmcli's Wi-Fi-list RATE is the BSS advertised capability,
                # not the currently negotiated client transmit rate.
                tx_rate=None,
                interface=interface, rssi_estimated=True,
            )
    return aps, connection


def _parse_iw_scan(output):
    aps = []
    for block in re.split(r"(?m)^BSS ", output)[1:]:
        address = re.match(r"([0-9a-f:]{17})", block, re.I)
        frequency = re.search(r"(?m)^\s*freq:\s*(\d+)", block)
        signal = re.search(r"(?m)^\s*signal:\s*(-?[\d.]+)\s*dBm", block)
        if not address or not frequency or not signal:
            continue
        band, channel = frequency_to_channel(int(frequency.group(1)))
        if band is None:
            continue
        ssid = re.search(r"(?m)^\s*SSID: ?(.*)$", block)
        secondary = re.search(r"secondary channel offset:\s*(above|below|no secondary)", block)
        width = None
        if secondary:
            width = 20 if secondary.group(1) == "no secondary" else 40
        # Match operation values, never capability/support descriptions.
        operation = re.search(r"channel width:\s*\d+\s*\((20|40|80|160)\s*MHz\)", block, re.I)
        if operation:
            width = int(operation.group(1))
        if re.search(r"(?mi)^\s*\*?\s*channel width:\s*\d+\s*\([^)]*80\s*\+\s*80", block):
            width = None
        center1 = re.search(r"center freq segment 1:\s*(\d+)", block)
        center2 = re.search(r"center freq segment 2:\s*(\d+)", block)
        if width == 80 and center1 and center2 and int(center2.group(1)):
            width = 160 if abs(int(center2.group(1)) - int(center1.group(1))) == 8 else None
        phy = next((name for token, name in (("EHT operation", "802.11be"), ("HE operation", "802.11ax"),
                                             ("VHT operation", "802.11ac"), ("HT operation", "802.11n"))
                    if token in block), "")
        rssi = round(float(signal.group(1)))
        if not -127 <= rssi < 0:
            continue
        aps.append(AP(_iw_ssid(ssid.group(1)) or None if ssid else None, address.group(1).lower(),
                      channel, width or 20, band, rssi, None,
                      phy=phy, width_known=width is not None,
                      frequency=int(frequency.group(1)),
                      secondary_offset=secondary.group(1) if secondary and secondary.group(1) in {"above", "below"} else None))
    return aps


def _iw_ssid(value):
    return re.sub(rb"\\x([0-9a-fA-F]{2})", lambda match: bytes([int(match.group(1), 16)]),
                  value.encode("utf-8")).decode("utf-8", errors="replace")


def _iw_output(interface, *arguments):
    try:
        result = run_command(["iw", "dev", interface, *arguments], timeout=3, stable_locale=True)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _enrich_iw(aps, connection):
    interface = connection.interface
    if not interface or not shutil.which("iw"):
        return False
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wifi-iw") as executor:
        futures = [executor.submit(_iw_output, interface, *args)
                   for args in (("link",), ("info",), ("survey", "dump"), ("scan", "dump"))]
        link, info, survey, scan_dump = [future.result() for future in futures]
    measured = {ap.bssid: ap for ap in _parse_iw_scan(scan_dump)}
    for ap in aps:
        candidate = measured.get((ap.bssid or "").lower())
        if candidate and candidate.band == ap.band and candidate.ch == ap.ch:
            ap.rssi, ap.rssi_estimated = candidate.rssi, False
            ap.width, ap.width_known = candidate.width, candidate.width_known
            ap.secondary_offset, ap.phy = candidate.secondary_offset, candidate.phy
        if ap.bssid == connection.bssid:
            connection.rssi, connection.rssi_estimated = ap.rssi, ap.rssi_estimated
            connection.width = ap.width if ap.width_known else None
    if "Not connected." in link:
        connection.connected = False
        connection.ssid = connection.bssid = None
        connection.rssi = connection.noise = connection.channel = connection.band = connection.width = None
        connection.tx_rate = None
        connection.rssi_estimated = False
        connection.security = connection.phy = ""
        for ap in aps:
            ap.cur = False
        return True
    address = re.search(r"Connected to ([0-9a-f:]{17})", link, re.I)
    frequency = re.search(r"(?m)^\s*freq:\s*(\d+)", link)
    signal = re.search(r"(?m)^\s*signal:\s*(-?[\d.]+)\s*dBm", link)
    rate = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", link, re.I)
    if address:
        if address.group(1).lower() != connection.bssid:
            connection.width = connection.noise = None
        connection.bssid = address.group(1).lower()
        connection.connected = True
        ssid = re.search(r"(?m)^\s*SSID: ?(.*)$", link)
        if ssid:
            connection.ssid = _iw_ssid(ssid.group(1)) or None
    if frequency:
        connection.band, connection.channel = frequency_to_channel(int(frequency.group(1)))
    if signal and -127 <= float(signal.group(1)) < 0:
        connection.rssi, connection.rssi_estimated = round(float(signal.group(1))), False
    if rate:
        connection.tx_rate = float(rate.group(1))
    width = re.search(r"width:\s*(20|40|80|160)\s*MHz", info, re.I)
    info_frequency = re.search(r"channel \d+\s*\((\d+) MHz\)", info)
    if width and frequency and info_frequency and info_frequency.group(1) == frequency.group(1):
        connection.width = int(width.group(1))
    if frequency:
        for block in re.split(r"(?m)^Survey data", survey):
            survey_freq = re.search(r"frequency:\s*(\d+)", block)
            noise = re.search(r"noise:\s*(-?\d+)\s*dBm", block)
            if survey_freq and noise and survey_freq.group(1) == frequency.group(1) and "[in use]" in block:
                value = int(noise.group(1))
                connection.noise = value if -127 <= value < 0 else None
    for ap in aps:
        ap.cur = bool(connection.connected and ap.bssid == connection.bssid)
        if ap.cur:
            ap.rssi, ap.rssi_estimated = connection.rssi, connection.rssi_estimated
            ap.noise = connection.noise
            if connection.width:
                ap.width, ap.width_known = connection.width, True
                center = re.search(r"center1:\s*(\d+) MHz", info)
                if ap.band == 2 and ap.width == 40 and center and frequency:
                    offset = int(center.group(1)) - int(frequency.group(1))
                    ap.secondary_offset = {10: "above", -10: "below"}.get(offset)
    return bool(link or info or survey or scan_dump)


def snapshot():
    global _last_rescan
    if not shutil.which("nmcli"):
        raise RuntimeError("nmcli를 찾을 수 없습니다. Ubuntu에서는 NetworkManager를 설치하고 실행하세요.")
    now = time.monotonic()
    rescan = "yes" if now - _last_rescan >= RESCAN_INTERVAL_SECONDS else "no"
    result = run_command(
        ["nmcli", "--terse", "--escape", "yes", "--fields",
         "ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY,RATE,DEVICE", "device", "wifi", "list", "--rescan", rescan],
        timeout=45, stable_locale=True,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "nmcli 스캔 실패"
        raise RuntimeError(message)
    if rescan == "yes":
        _last_rescan = time.monotonic()
    aps, connection = _parse(result.stdout)
    enriched = _enrich_iw(aps, connection)
    warnings = []
    if any(ap.rssi_estimated for ap in aps):
        warnings.append("일부 RSSI는 신호 백분율에서 환산한 추정값입니다.")
    if any(not ap.width_known for ap in aps):
        warnings.append("일부 AP의 채널 폭 미제공 · 전체 겹침 판정 제한")
    if connection.connected and connection.noise is None:
        warnings.append("드라이버가 잡음을 제공하지 않아 SNR은 미측정입니다.")
    if not enriched:
        warnings.append("iw 추가 수집 미사용 · iw 설치 및 드라이버 지원 여부를 확인하세요.")
    return ScanSnapshot(deduplicate_aps(aps), connection, SOURCE + (" + iw" if enriched else ""),
                        warnings=tuple(warnings))


def scan():
    return snapshot().aps


SOURCE = "NetworkManager nmcli (5초 재스캔)"
