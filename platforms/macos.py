"""macOS backends: CoreWLAN first, system_profiler as a fallback."""

import json
import re
import subprocess

from wifi_core import AP, ConnectionInfo, ScanSnapshot, deduplicate_aps

_CHANNEL = re.compile(r"(\d+)\s*\((\d)GHz(?:,\s*(\d+)MHz)?\)")
_SIGNAL_NOISE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
_WIDTHS = {0: None, 1: 20, 2: 40, 3: 80, 4: 160}
_BANDS = {1: 2, 2: 5, 3: 6}
_PHY = {1: "802.11a", 2: "802.11b", 3: "802.11g", 4: "802.11n",
        5: "802.11ac", 6: "802.11ax", 7: "802.11be"}
_SECURITY = {0: "Open", 1: "WEP", 2: "WPA Personal", 3: "WPA/WPA2 Personal",
             4: "WPA2 Personal", 5: "Personal", 6: "Dynamic WEP",
             7: "WPA Enterprise", 8: "WPA/WPA2 Enterprise", 9: "WPA2 Enterprise",
             10: "Enterprise", 11: "WPA3 Personal", 12: "WPA3 Enterprise",
             13: "WPA2/WPA3 Transition", 14: "OWE", 15: "OWE Transition"}


def _entry(name, entry, current=False):
    match = _CHANNEL.search(str(entry.get("spairport_network_channel", "")))
    if not match:
        return None
    sn = _SIGNAL_NOISE.search(str(entry.get("spairport_signal_noise", "")))
    rssi, noise = (int(sn.group(1)), int(sn.group(2))) if sn else (-99, 0)
    return AP(None if str(name).startswith("<redacted") else name, None, int(match.group(1)),
              int(match.group(3) or 20), int(match.group(2)), rssi, noise,
              str(entry.get("spairport_security_mode", "")).replace("spairport_security_mode_", ""),
              str(entry.get("spairport_network_phymode", "")), current)


def _parse_system_profiler(payload):
    aps = []
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "spairport_current_network_information" and isinstance(item, dict):
                    ap = _entry(item.get("_name", "?"), item, True)
                    if ap:
                        aps.append(ap)
                elif key == "spairport_airport_other_local_wireless_networks" and isinstance(item, list):
                    for network in item:
                        ap = _entry(network.get("_name", "?"), network)
                        if ap:
                            aps.append(ap)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(json.loads(payload))
    return aps


def _system_profiler():
    result = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                            capture_output=True, text=True, timeout=45, check=True)
    return _parse_system_profiler(result.stdout)


def _system_profiler_snapshot():
    aps = _system_profiler()
    current = next((ap for ap in aps if ap.cur), None)
    connection = ConnectionInfo(
        connected=current is not None,
        ssid=current.ssid if current else None,
        channel=current.ch if current else None,
        width=current.width if current else None,
        band=current.band if current else None,
        rssi=current.rssi if current else None,
        noise=current.noise if current else None,
        phy=current.phy if current else "",
        security=current.sec if current else "",
    )
    return ScanSnapshot(aps, connection, "system_profiler",
                        warnings=("BSSID는 system_profiler에서 제공되지 않습니다.",))


def _information_elements(raw):
    """Return optional BSS Load and HT secondary-channel fields."""
    if raw is None:
        return {}
    data, position, result = bytes(raw), 0, {}
    while position + 2 <= len(data):
        element_id, length = data[position], data[position + 1]
        position += 2
        if position + length > len(data):
            break
        payload = data[position:position + length]
        position += length
        if element_id == 11 and length >= 5:
            result["station_count"] = int.from_bytes(payload[:2], "little")
            result["channel_utilization"] = payload[2]
        elif element_id == 61 and length >= 2:
            result["secondary_offset"] = {1: "above", 3: "below"}.get(payload[1] & 3)
    return result


def _network_security(network):
    for value in (12, 11, 13, 14, 15, 9, 8, 7, 4, 3, 2, 1, 0):
        try:
            if network.supportsSecurity_(value):
                return _SECURITY[value]
        except Exception:
            continue
    try:
        return _SECURITY.get(int(network.securityMode()), "")
    except Exception:
        return ""


def _network_phy(network):
    for value in range(7, 0, -1):
        try:
            if network.supportsPHYMode_(value):
                return _PHY[value]
        except Exception:
            continue
    return ""


def _corewlan_snapshot():
    import CoreWLAN
    interface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
    if interface is None:
        raise RuntimeError("사용 가능한 Wi-Fi 인터페이스가 없습니다.")
    networks, error = interface.scanForNetworksWithName_error_(None, None)
    if error is not None:
        raise RuntimeError(str(error))
    current_bssid = (interface.bssid() or "").lower()
    aps = []
    for network in networks or []:
        wlan_channel = network.wlanChannel()
        if wlan_channel is None:
            continue
        channel = int(wlan_channel.channelNumber())
        band = _BANDS.get(int(wlan_channel.channelBand()))
        if band is None:
            continue
        raw_width = int(wlan_channel.channelWidth())
        width = _WIDTHS.get(raw_width)
        bssid = network.bssid()
        aps.append(AP(network.ssid() or None, bssid, channel,
                      width or 20, band, int(network.rssiValue()),
                      int(network.noiseMeasurement()), _network_security(network),
                      _network_phy(network), bool(bssid) and bssid.lower() == current_bssid,
                      width_known=width is not None,
                      **_information_elements(network.informationElementData())))
    wlan_channel = interface.wlanChannel()
    connected = wlan_channel is not None
    raw_width = int(wlan_channel.channelWidth()) if wlan_channel else 0
    connection = ConnectionInfo(
        connected=connected, ssid=interface.ssid() or None,
        bssid=interface.bssid() or None,
        channel=int(wlan_channel.channelNumber()) if wlan_channel else None,
        width=_WIDTHS.get(raw_width),
        band=_BANDS.get(int(wlan_channel.channelBand())) if wlan_channel else None,
        rssi=int(interface.rssiValue()) if connected else None,
        noise=int(interface.noiseMeasurement()) if connected else None,
        tx_rate=float(interface.transmitRate()) if connected else None,
        phy=_PHY.get(int(interface.activePHYMode()), "") if connected else "",
        security=_SECURITY.get(int(interface.security()), "") if connected else "",
        interface=str(interface.interfaceName() or ""),
    )
    return ScanSnapshot(deduplicate_aps(aps), connection, "CoreWLAN")


def _corewlan():
    return _corewlan_snapshot().aps


def scan():
    return snapshot().aps


def snapshot():
    try:
        # GUI scans run on a worker thread. PyObjC requires an explicit
        # autorelease pool outside the main Python thread.
        import objc
        with objc.autorelease_pool():
            return _corewlan_snapshot()
    except (ImportError, ModuleNotFoundError):
        return _system_profiler_snapshot()
    except Exception as core_error:
        try:
            result = _system_profiler_snapshot()
            result.warnings += (f"CoreWLAN 실패: {core_error}",)
            return result
        except Exception:
            raise core_error


def source():
    try:
        import CoreWLAN  # noqa: F401
        return "CoreWLAN"
    except ImportError:
        return "system_profiler"


def request_location_permission():
    try:
        from CoreLocation import CLLocationManager
    except ImportError:
        return None, None
    manager = CLLocationManager.alloc().init()
    status = manager.authorizationStatus()
    if status == 0:
        manager.requestWhenInUseAuthorization()
    return manager, status
