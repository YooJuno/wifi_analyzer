"""Platform-neutral Wi-Fi data types and radio calculations."""

import math
import time
from dataclasses import dataclass, field

BLOCKS = {
    40: [(36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112),
         (116, 120), (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)],
    80: [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)],
    160: [(36, 64), (100, 128)],
}

# 6 GHz primary channels are spaced four channel numbers (20 MHz) apart.
# A bonded channel starts at channel 1 plus a multiple of 8/16/32 for
# 40/80/160 MHz respectively.  The final primary is one 20 MHz slot below the
# upper edge of that block.
SIX_GHZ_BLOCK_SIZE = {40: 8, 80: 16, 160: 32}


def channel_to_frequency(channel, band=None):
    if band == 6:
        return 5935 if channel == 2 else 5950 + 5 * channel
    if band == 2 and channel == 14:
        return 2484
    return 2407 + 5 * channel if band == 2 or (band is None and channel <= 14) else 5000 + 5 * channel


def occupied_range(channel, width, band=None):
    """Return the occupied frequency range in MHz."""
    if band == 2 or (band is None and channel <= 14):
        center = channel_to_frequency(channel, 2)
        return center - width / 2, center + width / 2
    if band == 5:
        for low, high in BLOCKS.get(width, []):
            if low <= channel <= high:
                return channel_to_frequency(low, 5) - 10, channel_to_frequency(high, 5) + 10
    if band == 6 and width in SIX_GHZ_BLOCK_SIZE and 1 <= channel <= 233 \
            and (channel - 1) % 4 == 0:
        channel_span = SIX_GHZ_BLOCK_SIZE[width]
        low = 1 + ((channel - 1) // channel_span) * channel_span
        high = low + channel_span - 4
        # The top of the 6 GHz allocation can contain a smaller remainder;
        # an impossible advertised width must not be projected out of band.
        if high <= 233:
            return (channel_to_frequency(low, 6) - 10,
                    channel_to_frequency(high, 6) + 10)
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
                 "sec", "phy", "cur", "frequency", "key", "channel_utilization",
                 "station_count", "secondary_offset", "width_known", "observed_at")

    def __init__(self, ssid, bssid, ch, width, band, rssi, noise,
                 sec="", phy="", cur=False, frequency=None, channel_utilization=None,
                 station_count=None, secondary_offset=None, width_known=True,
                 observed_at=None):
        self.ssid = ssid
        self.bssid = bssid
        self.ch, self.width, self.band = ch, width, band
        self.rssi, self.noise = rssi, noise
        self.sec, self.phy, self.cur = sec, phy, cur
        self.frequency = frequency
        self.channel_utilization = channel_utilization
        self.station_count = station_count
        self.secondary_offset = secondary_offset
        self.width_known = width_known
        self.observed_at = observed_at or time.time()
        self.key = bssid or f"{ssid}|{band}|{ch}|{width}"

    @property
    def rng(self):
        if self.band == 2 and self.width == 40 and self.secondary_offset:
            center = self.center_frequency + (10 if self.secondary_offset == "above" else -10)
            return center - 20, center + 20
        if self.band in {5, 6} and self.width > 20:
            return occupied_range(self.ch, self.width, self.band)
        if self.frequency is not None:
            return self.frequency - self.width / 2, self.frequency + self.width / 2
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


def deduplicate_aps(aps):
    """Collapse duplicate BSSID rows while preserving anonymous observations.

    BSSID is the stable identity exposed by all three native scanners.  Some
    drivers return the same BSS more than once; suffixing those rows would mix
    time-series history whenever their ordering changes.
    """
    by_bssid, anonymous = {}, []
    for ap in aps:
        if not ap.bssid:
            anonymous.append(ap)
            continue
        key = ap.bssid.lower().replace("-", ":")
        previous = by_bssid.get(key)
        if previous is None or (bool(ap.ssid), ap.rssi) > (bool(previous.ssid), previous.rssi):
            ap.bssid = key
            ap.key = key
            by_bssid[key] = ap
    return list(by_bssid.values()) + anonymous


@dataclass
class ConnectionInfo:
    connected: bool = False
    ssid: str | None = None
    bssid: str | None = None
    channel: int | None = None
    width: int | None = None
    band: int | None = None
    rssi: int | None = None
    noise: int | None = None
    tx_rate: float | None = None
    phy: str = ""
    security: str = ""
    interface: str = ""

    @property
    def snr(self):
        if self.rssi is None or self.noise is None:
            return None
        return self.rssi - self.noise


@dataclass
class ScanSnapshot:
    aps: list[AP]
    connection: ConnectionInfo = field(default_factory=ConnectionInfo)
    source: str = ""
    timestamp: float = field(default_factory=time.time)
    warnings: tuple[str, ...] = ()
