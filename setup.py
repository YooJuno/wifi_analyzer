"""Build the standalone macOS app: python3 setup.py py2app."""

from setuptools import setup

APP = ["wifi_gui.py"]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["CoreLocation", "CoreWLAN"],
    "plist": {
        "CFBundleIdentifier": "com.juno.wifi-analyzer",
        "CFBundleDisplayName": "WiFi Analyzer",
        "CFBundleName": "WiFi Analyzer",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSLocationUsageDescription": (
            "주변 Wi-Fi 네트워크의 이름과 BSSID를 표시하기 위해 위치 접근이 필요합니다."
        ),
        "NSLocationWhenInUseUsageDescription": (
            "주변 Wi-Fi 네트워크의 이름과 BSSID를 표시하기 위해 위치 접근이 필요합니다."
        ),
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
