# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller definition for WiFi Analyzer."""

import os
import re
import sys


def normalized_version(raw):
    """Return a macOS-safe three-component version with a stable fallback."""
    value = (raw or "").strip().lstrip("vV")
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return "0.0.0"
    return ".".join(part or "0" for part in match.groups())


app_version = normalized_version(os.environ.get("WIFI_ANALYZER_VERSION"))


hidden_imports = [
    # wifi_platform imports these modules dynamically at runtime.
    "platforms.linux",
    "platforms.macos",
    "platforms.windows",
]

if sys.platform == "darwin":
    hidden_imports += ["CoreLocation", "CoreWLAN"]


analysis = Analysis(
    ["wifi_gui.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/wifi-analyzer.png", "assets")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="WiFiAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="universal2" if sys.platform == "darwin" and os.environ.get("WIFI_MAC_UNIVERSAL") == "1" else None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/wifi-analyzer.ico" if sys.platform == "win32" else None,
    manifest="assets/windows.manifest" if sys.platform == "win32" else None,
)

bundle_contents = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WiFiAnalyzer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        bundle_contents,
        name="WiFiAnalyzer.app",
        icon="assets/wifi-analyzer.icns",
        bundle_identifier="com.juno.wifi-analyzer",
        info_plist={
            "CFBundleDisplayName": "WiFi Analyzer",
            "CFBundleName": "WiFi Analyzer",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "NSLocationUsageDescription": (
                "주변 Wi-Fi 네트워크의 이름과 BSSID를 표시하기 위해 "
                "위치 접근이 필요합니다."
            ),
            "NSLocationWhenInUseUsageDescription": (
                "주변 Wi-Fi 네트워크의 이름과 BSSID를 표시하기 위해 "
                "위치 접근이 필요합니다."
            ),
        },
    )

