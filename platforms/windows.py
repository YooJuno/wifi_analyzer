"""Windows backend using the built-in netsh WLAN command."""

import re
import subprocess

from wifi_core import AP, ConnectionInfo, ScanSnapshot, deduplicate_aps

_SSID = re.compile(r"^\s*SSID\s+\d+\s*:\s*(.*)$", re.I)
_BSSID = re.compile(r"^\s*BSSID\s+\d+\s*:\s*([0-9a-f:.-]+)", re.I)
_CHANNEL = re.compile(r"^\s*(?:Channel|채널)\s*:\s*(\d+)", re.I)
_SIGNAL = re.compile(r"^\s*(?:Signal|신호)\s*:\s*(\d+)%", re.I)
_AUTH = re.compile(r"^\s*(?:Authentication|인증)\s*:\s*(.*)$", re.I)
_RADIO = re.compile(r"^\s*(?:Radio type|무선 종류|무선 유형|라디오 유형)\s*:\s*(.*)$", re.I)
_BAND = re.compile(r"^\s*(?:Band|대역|밴드)\s*:\s*(.*)$", re.I)


def _band(value, channel):
    match = re.search(r"([256])(?:[.,]\d+)?\s*GHz", value, re.I)
    return int(match.group(1)) if match else (2 if channel <= 14 else 5)


def _parse_networks(output):
    aps, ssid, security, band_text, current = [], None, "", "", None

    def append_current():
        nonlocal current
        if not current or not current["channel"]:
            current = None
            return
        channel = current["channel"]
        aps.append(AP(ssid, current["bssid"], channel, 20,
                      _band(str(current.get("band") or band_text), channel),
                      round(current["signal"] / 2 - 100), None, security,
                      str(current.get("phy", "")), width_known=False))
        current = None

    for raw in output.splitlines() + [""]:
        if match := _SSID.match(raw):
            append_current()
            ssid, security, band_text, current = match.group(1).strip() or None, "", "", None
        elif match := _AUTH.match(raw):
            security = match.group(1).strip()
        elif match := _BAND.match(raw):
            band_text = match.group(1).strip()
            if current:
                current["band"] = band_text
        elif match := _BSSID.match(raw):
            append_current()
            current = {"bssid": match.group(1), "channel": None, "signal": 0,
                       "band": band_text, "phy": ""}
        elif current and (match := _CHANNEL.match(raw)):
            current["channel"] = int(match.group(1))
        elif current and (match := _SIGNAL.match(raw)):
            current["signal"] = int(match.group(1))
        elif current and (match := _RADIO.match(raw)):
            current["phy"] = match.group(1).strip()
        elif not raw.strip():
            append_current()
    return aps


def _parse_interface(output):
    aliases = {
        "name": "interface", "이름": "interface", "state": "state", "상태": "state",
        "ssid": "ssid", "bssid": "bssid", "channel": "channel", "채널": "channel",
        "signal": "signal", "신호": "signal", "radio type": "phy", "무선 종류": "phy",
        "무선 유형": "phy", "라디오 유형": "phy",
        "authentication": "security", "인증": "security", "band": "band", "대역": "band",
        "밴드": "band", "transmit rate (mbps)": "tx_rate",
        "전송 속도(mbps)": "tx_rate", "송신 속도(mbps)": "tx_rate",
    }
    values = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        key = aliases.get(name.casefold())
        if key:
            values[key] = value
    connected = values.get("state", "").casefold() in {"connected", "연결됨"}
    channel = int(values["channel"]) if values.get("channel", "").isdigit() else None
    rate = re.search(r"[\d.]+", values.get("tx_rate", ""))
    signal = re.search(r"\d+", values.get("signal", ""))
    return ConnectionInfo(
        connected=connected, ssid=values.get("ssid") if connected else None,
        bssid=values.get("bssid") if connected else None, channel=channel,
        band=_band(values.get("band", ""), channel) if channel else None,
        rssi=round(int(signal.group()) / 2 - 100) if connected and signal else None,
        tx_rate=float(rate.group()) if rate else None, phy=values.get("phy", ""),
        security=values.get("security", ""), interface=values.get("interface", ""),
    )


def snapshot():
    result = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                            capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "netsh WLAN 스캔 실패")
    aps = _parse_networks(result.stdout)
    interface_result = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
        timeout=20, check=False)
    connection = (_parse_interface(interface_result.stdout)
                  if not interface_result.returncode else ConnectionInfo())
    current_bssid = (connection.bssid or "").lower().replace("-", ":")
    for ap in aps:
        ap.cur = bool(current_bssid) and (ap.bssid or "").lower().replace("-", ":") == current_bssid
    return ScanSnapshot(deduplicate_aps(aps), connection, SOURCE,
                        warnings=("netsh는 채널 폭과 noise를 제공하지 않습니다.",))


def scan():
    return snapshot().aps


SOURCE = "Windows netsh WLAN"
