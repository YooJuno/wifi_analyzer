"""macOS backends: CoreWLAN first, system_profiler as a fallback."""

import json
import re
import subprocess

from wifi_core import AP

_CHANNEL = re.compile(r"(\d+)\s*\((\d)GHz(?:,\s*(\d+)MHz)?\)")
_SIGNAL_NOISE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
_WIDTHS = {0: 20, 1: 20, 2: 40, 3: 40, 4: 80, 5: 160}


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


def _system_profiler():
    result = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                            capture_output=True, text=True, timeout=45, check=True)
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
    walk(json.loads(result.stdout))
    return aps


def _corewlan():
    import CoreWLAN
    interface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
    networks, _ = interface.scanForNetworksWithName_error_(None, None)
    current_bssid = interface.bssid()
    aps = []
    for network in networks or []:
        wlan_channel = network.wlanChannel()
        if wlan_channel is None:
            continue
        channel = int(wlan_channel.channelNumber())
        bssid = network.bssid()
        aps.append(AP(network.ssid() or None, bssid, channel,
                      _WIDTHS.get(int(wlan_channel.channelWidth()), 20),
                      2 if channel <= 14 else 5, int(network.rssiValue()),
                      int(network.noiseMeasurement()), "", "", bssid == current_bssid))
    return aps


def scan():
    try:
        return _corewlan()
    except ImportError:
        return _system_profiler()


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
