"""Native Wi-Fi collector. Fixed-width SDK layouts also permit fixture tests.

References: Microsoft Learn WLAN_BSS_ENTRY, WLAN_CONNECTION_ATTRIBUTES and
WlanRegisterNotification. DLL loading happens only inside snapshot().
"""

import ctypes as c
import struct
import threading

from wifi_core import AP, ConnectionInfo, ScanSnapshot, deduplicate_aps, frequency_to_channel
from platforms.wifi_ie import parse_information_elements

U32, U16, U8, I32 = c.c_uint32, c.c_uint16, c.c_uint8, c.c_int32
GUID = U8 * 16
PHY = {4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n",
       8: "802.11ac", 10: "802.11ax", 11: "802.11be"}


class SSID(c.Structure):
    _fields_ = [("length", U32), ("data", U8 * 32)]


class RateSet(c.Structure):
    _fields_ = [("length", U32), ("rates", U16 * 126)]


class BssEntry(c.Structure):
    _fields_ = [("ssid", SSID), ("phy_id", U32), ("bssid", U8 * 6),
                ("bss_type", U32), ("phy", U32), ("rssi", I32), ("quality", U32),
                ("in_domain", U8), ("beacon_period", U16), ("timestamp", c.c_uint64),
                ("host_timestamp", c.c_uint64), ("capabilities", U16), ("frequency_khz", U32),
                ("rates", RateSet), ("ie_offset", U32), ("ie_size", U32)]


class InterfaceInfo(c.Structure):
    _fields_ = [("guid", GUID), ("description", U16 * 256), ("state", U32)]


class Association(c.Structure):
    _fields_ = [("ssid", SSID), ("bss_type", U32), ("bssid", U8 * 6),
                ("phy", U32), ("phy_index", U32), ("quality", U32),
                ("rx_rate", U32), ("tx_rate", U32)]


class Security(c.Structure):
    _fields_ = [("enabled", I32), ("one_x", I32), ("auth", U32), ("cipher", U32)]


class ConnectionAttributes(c.Structure):
    _fields_ = [("state", U32), ("mode", U32), ("profile", U16 * 256),
                ("association", Association), ("security", Security)]


class Notification(c.Structure):
    _fields_ = [("source", U32), ("code", U32), ("guid", GUID),
                ("data_size", U32), ("data", c.c_void_p)]


def _ssid(value):
    return bytes(value.data[:min(value.length, 32)]).decode("utf-8", errors="replace") or None


def _mac(value):
    return ":".join(f"{byte:02x}" for byte in value)


def parse_bss_list(data):
    if len(data) < 8:
        raise ValueError("Truncated WLAN BSS header")
    total, count = struct.unpack_from("<II", data)
    entry_size = c.sizeof(BssEntry)
    if not 8 <= total <= len(data) or count > (total - 8) // entry_size:
        raise ValueError("Invalid WLAN BSS bounds")
    aps = []
    for index in range(count):
        offset = 8 + index * entry_size
        entry = BssEntry.from_buffer_copy(data, offset)
        frequency = entry.frequency_khz // 1000
        band, channel = frequency_to_channel(frequency)
        if band is None or not -127 <= entry.rssi < 0:
            continue
        fields = {}
        start, end = offset + entry.ie_offset, offset + entry.ie_offset + entry.ie_size
        if entry.ie_size and 8 + count * entry_size <= start <= end <= total:
            fields = parse_information_elements(data[start:end], band)
        width = fields.pop("width", None)
        if width is None and entry.phy in {4, 5, 6}:
            width = 20
        aps.append(AP(_ssid(entry.ssid), _mac(entry.bssid), channel, width or 20,
                      band, entry.rssi, None,
                      "보안 사용 (방식 미제공)" if entry.capabilities & 16 else "Open",
                      PHY.get(entry.phy, ""), frequency=frequency,
                      width_known=width is not None, **fields))
    return deduplicate_aps(aps)


def parse_connection(data, interface, aps):
    if len(data) < c.sizeof(ConnectionAttributes):
        raise ValueError("Truncated WLAN connection attributes")
    attributes = ConnectionAttributes.from_buffer_copy(data)
    if attributes.state != 1:
        return ConnectionInfo(interface=interface)
    association = attributes.association
    bssid = _mac(association.bssid)
    current = next((ap for ap in aps if ap.bssid == bssid), None)
    for ap in aps:
        ap.cur = ap is current
    return ConnectionInfo(
        connected=True, ssid=_ssid(association.ssid), bssid=bssid,
        channel=current.ch if current else None, band=current.band if current else None,
        width=current.width if current and current.width_known else None,
        rssi=current.rssi if current else round(min(100, association.quality) / 2 - 100),
        rssi_estimated=current is None, tx_rate=association.tx_rate / 1000 or None,
        phy=PHY.get(association.phy, ""), security=current.sec if current else "",
        interface=interface)


class WlanClient:
    def __init__(self):
        if not hasattr(c, "WinDLL"):
            raise OSError("Native Wi-Fi is only available on Windows")
        self.api = c.WinDLL("wlanapi.dll")
        self._callbacks = []
        handle, pointer = c.c_void_p, c.POINTER
        signatures = {
            "WlanOpenHandle": [U32, handle, pointer(U32), pointer(handle)],
            "WlanCloseHandle": [handle, handle],
            "WlanEnumInterfaces": [handle, handle, pointer(handle)],
            "WlanFreeMemory": [handle],
            "WlanGetNetworkBssList": [handle, pointer(GUID), handle, U32, I32, handle, pointer(handle)],
            "WlanQueryInterface": [handle, pointer(GUID), U32, handle, pointer(U32), pointer(handle), pointer(U32)],
            "WlanScan": [handle, pointer(GUID), handle, handle, handle],
            "WlanRegisterNotification": [handle, U32, I32, handle, handle, handle, pointer(U32)],
        }
        for name, args in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = args
            function.restype = None if name == "WlanFreeMemory" else U32
        self.handle = handle()
        version = U32()
        self._check(self.api.WlanOpenHandle(2, None, c.byref(version), c.byref(self.handle)), "WlanOpenHandle")

    @staticmethod
    def _check(code, operation):
        if code:
            raise OSError(code, f"{operation} 오류 {code}")

    def interfaces(self):
        pointer = c.c_void_p()
        self._check(self.api.WlanEnumInterfaces(self.handle, None, c.byref(pointer)), "WlanEnumInterfaces")
        try:
            if not pointer.value:
                raise ValueError("Empty WLAN interface buffer")
            count = U32.from_address(pointer.value).value
            if count > 64:
                raise ValueError("Invalid WLAN interface count")
            data = c.string_at(pointer, 8 + count * c.sizeof(InterfaceInfo))
            return [InterfaceInfo.from_buffer_copy(data, 8 + index * c.sizeof(InterfaceInfo))
                    for index in range(count)]
        finally:
            self.api.WlanFreeMemory(pointer)

    def request_scan(self, guid):
        completed = threading.Event()
        outcome = []
        callback_type = getattr(c, "WINFUNCTYPE", c.CFUNCTYPE)(None, c.POINTER(Notification), c.c_void_p)

        def notify(pointer, _context):
            event = pointer.contents
            if event.source & 8 and bytes(event.guid) == bytes(guid) and event.code in {7, 8}:
                outcome.append(event.code)
                completed.set()

        callback = callback_type(notify)
        self._callbacks.append(callback)
        previous = U32()
        registered = self.api.WlanRegisterNotification(
            self.handle, 8, False, c.cast(callback, c.c_void_p), None, None, c.byref(previous))
        if registered:
            return f"스캔 알림 등록 실패 ({registered}) · 캐시 관측값 사용"
        try:
            code = self.api.WlanScan(self.handle, c.byref(guid), None, None, None)
            if code:
                return f"Wi-Fi 재스캔 실패 ({code}) · 캐시 관측값 사용"
            if not completed.wait(5):
                return "Wi-Fi 스캔 완료 대기 시간 초과 · 캐시 관측값 사용"
            if outcome[0] == 8:
                return "Wi-Fi 스캔 실패 알림 · 캐시 관측값 사용"
            return ""
        finally:
            # Unregister outside the callback and retain its Python reference
            # until the native API has stopped dispatching notifications.
            self.api.WlanRegisterNotification(self.handle, 0, False, None, None, None, c.byref(previous))

    def bss_list(self, guid):
        pointer = c.c_void_p()
        self._check(self.api.WlanGetNetworkBssList(
            self.handle, c.byref(guid), None, 3, False, None, c.byref(pointer)), "WlanGetNetworkBssList")
        try:
            if not pointer.value:
                raise ValueError("Empty WLAN BSS buffer")
            total = U32.from_address(pointer.value).value
            if not 8 <= total <= 64 * 1024 * 1024:
                raise ValueError("Invalid WLAN buffer size")
            return parse_bss_list(c.string_at(pointer, total))
        finally:
            self.api.WlanFreeMemory(pointer)

    def connection(self, guid, description, aps):
        pointer, size, kind = c.c_void_p(), U32(), U32()
        code = self.api.WlanQueryInterface(
            self.handle, c.byref(guid), 7, None, c.byref(size), c.byref(pointer), c.byref(kind))
        if code == 5023:  # ERROR_INVALID_STATE: not associated.
            return ConnectionInfo(interface=description)
        self._check(code, "WlanQueryInterface")
        try:
            if not pointer.value or size.value < c.sizeof(ConnectionAttributes):
                raise ValueError("Invalid WLAN connection size")
            return parse_connection(c.string_at(pointer, c.sizeof(ConnectionAttributes)), description, aps)
        finally:
            self.api.WlanFreeMemory(pointer)

    def close(self):
        self.api.WlanCloseHandle(self.handle, None)
        self._callbacks.clear()


def snapshot():
    client = WlanClient()
    try:
        interfaces = sorted(client.interfaces(), key=lambda item: item.state != 1)
        if not interfaces:
            raise OSError("사용 가능한 Wi-Fi 어댑터가 없습니다.")
        interface = interfaces[0]
        description = bytes(interface.description).decode("utf-16-le", errors="replace").split("\0", 1)[0]
        warning = client.request_scan(interface.guid)
        aps = client.bss_list(interface.guid)
        connection = client.connection(interface.guid, description, aps)
        warnings = [warning] if warning else []
        if any(not ap.width_known for ap in aps):
            warnings.append("일부 AP의 채널 폭 미제공 · 전체 겹침 판정 제한")
        if connection.connected:
            warnings.append("Windows Wi-Fi API는 잡음을 제공하지 않아 SNR은 미측정입니다.")
        return ScanSnapshot(aps, connection, "Windows Native Wi-Fi", warnings=tuple(warnings))
    finally:
        client.close()
