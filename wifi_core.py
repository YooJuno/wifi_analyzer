"""Platform-neutral Wi-Fi data types and radio calculations."""

import math

BLOCKS = {
    40: [(36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112),
         (116, 120), (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)],
    80: [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)],
    160: [(36, 64), (100, 128)],
}


def channel_to_frequency(channel, band=None):
    if band == 6:
        return 5950 + 5 * channel
    return 2407 + 5 * channel if band == 2 or (band is None and channel <= 14) else 5000 + 5 * channel


def occupied_range(channel, width, band=None):
    """Return the occupied frequency range in MHz."""
    if band == 2 or (band is None and channel <= 14):
        center = channel_to_frequency(channel, 2)
        return center - 10, center + 10
    if band == 5:
        for low, high in BLOCKS.get(width, []):
            if low <= channel <= high:
                return channel_to_frequency(low, 5) - 10, channel_to_frequency(high, 5) + 10
    center = channel_to_frequency(channel, band)
    return center - (width / 2), center + (width / 2)


def overlap_mhz(first, second):
    return max(0, min(first[1], second[1]) - max(first[0], second[0]))


def relevant_interferers(target, aps, relative_threshold=15):
    """Return overlapping APs strong enough to affect the target connection.

    APs more than ``relative_threshold`` dB below the target are included in
    raw scan data but omitted from diagnosis because their practical impact is
    normally negligible.
    """
    interferers = []
    for ap in aps:
        if ap is target or ap.band != target.band or ap.rssi <= target.rssi - relative_threshold:
            continue
        overlap = overlap_mhz(target.rng, ap.rng)
        if overlap:
            interferers.append((overlap, ap))
    return sorted(interferers, key=lambda item: -item[1].rssi)


def interference_margin_db(target, interferers):
    """Estimate target signal over overlapping Wi-Fi signal in dB."""
    interference_mw = sum(10 ** (ap.rssi / 10.0) * min(1.0, overlap / target.width)
                          for overlap, ap in interferers)
    return None if not interference_mw else target.rssi - 10 * math.log10(interference_mw)


def interference_level(margin):
    """Classify a C/I estimate without claiming it is a throughput measurement."""
    if margin is None or margin >= 20:
        return "낮음"
    if margin >= 10:
        return "주의"
    if margin >= 0:
        return "높음"
    return "심각"


class AP:
    __slots__ = ("ssid", "bssid", "ch", "width", "band", "rssi", "noise",
                 "sec", "phy", "cur", "frequency", "key")

    def __init__(self, ssid, bssid, ch, width, band, rssi, noise,
                 sec="", phy="", cur=False, frequency=None):
        self.ssid = ssid
        self.bssid = bssid
        self.ch, self.width, self.band = ch, width, band
        self.rssi, self.noise = rssi, noise
        self.sec, self.phy, self.cur = sec, phy, cur
        self.frequency = frequency
        self.key = bssid or f"{ssid}|{band}|{ch}|{width}"

    @property
    def rng(self):
        if self.frequency is not None and self.band == 2:
            return self.frequency - 10, self.frequency + 10
        return occupied_range(self.ch, self.width, self.band)

    @property
    def center_frequency(self):
        return self.frequency or channel_to_frequency(self.ch, self.band)

    @property
    def snr(self):
        return self.rssi - self.noise if self.noise is not None and self.noise < 0 else 0


def assign_keys(aps):
    """Give all APs a stable, unique UI row identifier."""
    groups = {}
    for ap in aps:
        identity = ("bssid", ap.bssid) if ap.bssid else (
            "network", ap.ssid, ap.band, ap.ch, ap.width)
        groups.setdefault(identity, []).append(ap)
    for identity, group in groups.items():
        group.sort(key=lambda item: -item.rssi)
        base = group[0].bssid if identity[0] == "bssid" else "|".join(map(str, identity[1:]))
        for index, ap in enumerate(group):
            ap.key = base if len(group) == 1 else f"{base}#{index}"
    return aps
