"""Select the Wi-Fi scanner for the operating system running this program."""

import sys


def _backend():
    if sys.platform == "darwin":
        from platforms import macos
        return macos
    if sys.platform.startswith("linux"):
        from platforms import linux
        return linux
    if sys.platform == "win32":
        from platforms import windows
        return windows
    raise RuntimeError(f"지원하지 않는 운영체제: {sys.platform}")


def scan_wifi():
    return _backend().scan()


def scan_snapshot():
    """Return BSS observations plus current-link details when available."""
    backend = _backend()
    get_snapshot = getattr(backend, "snapshot", None)
    if get_snapshot:
        return get_snapshot()
    from wifi_core import ScanSnapshot
    return ScanSnapshot(backend.scan(), source=scan_source())


def scan_source():
    backend = _backend()
    source = getattr(backend, "source", None)
    return source() if source else backend.SOURCE


def platform_label():
    return {"darwin": "macOS", "win32": "Windows"}.get(
        sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform)


def request_location_permission():
    """Request the optional macOS location permission; no-op elsewhere."""
    request = getattr(_backend(), "request_location_permission", None)
    return request() if request else (None, None)
