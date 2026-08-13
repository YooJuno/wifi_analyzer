# Wi-Fi Analyzer for macOS

주변 Wi-Fi AP의 채널 점유와 신호 세기를 확인하는 간단한 macOS용 도구입니다.

## 구성

- `wifi_gui.py`: 2.4 GHz·5 GHz 스펙트럼, AP 목록, 선택 AP의 신호 추이를 보여주는 GUI
- `wifi_scan.py`: 터미널에서 채널별 밀집도와 현재 연결 AP에 겹치는 네트워크를 보여주는 CLI

## 설치

```bash
python3 -m pip install -r requirements.txt
```

`pyobjc-framework-CoreWLAN`을 설치하면 CoreWLAN으로 스캔해 BSSID까지 표시합니다.
설치하지 않아도 macOS 기본 `system_profiler` 경로로 동작하지만, macOS 버전에 따라
SSID/BSSID가 가려질 수 있습니다.

## 실행

```bash
# GUI — 처음에는 이 명령으로 열어 위치 권한을 허용하세요.
open WiFiAnalyzer.app

# 터미널 모니터 (기본: 5초 간격)
python3 wifi_scan.py

# 한 번만 스캔
python3 wifi_scan.py --once
```

## 위치 권한

macOS 14 이상에서는 Wi-Fi 목록이 위치 정보로 취급됩니다. 처음 실행할 때 뜨는
**WiFi Analyzer의 위치 접근** 요청을 허용하세요. 요청을 놓쳤거나 거부했다면
**시스템 설정 → 개인정보 보호 및 보안 → 위치 서비스 → WiFi Analyzer**에서 허용한 뒤
다시 실행하세요.

`(숨김 SSID)`는 공유기가 네트워크 이름을 브로드캐스트하지 않는 경우이고, `미제공`은
macOS가 해당 항목의 BSSID를 스캔 API에 제공하지 않은 경우입니다.

## 앱 번들 다시 만들기

Python 또는 의존성을 바꾼 뒤에는 아래 명령으로 독립 실행형 앱을 다시 만들 수 있습니다.

```bash
python3 -m pip install py2app
python3 setup.py py2app
open "dist/WiFi Analyzer.app"
```
