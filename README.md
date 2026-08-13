# Wi-Fi Analyzer

주변 Wi-Fi AP의 채널 점유와 신호 세기를 보여주는 데스크톱 도구입니다. 하나의 GUI/CLI를
공유하고, 스캔 구현만 운영체제별 모듈로 분리했습니다.

## 플랫폼 구조

| 플랫폼 | 스캔 모듈 | 데이터 소스 | 실행 도구 |
| --- | --- | --- | --- |
| macOS | `platforms/macos.py` | CoreWLAN, `system_profiler` 대체 | `run_gui.command` |
| Ubuntu/Linux | `platforms/linux.py` | NetworkManager `nmcli` | `run_gui.sh` |
| Windows | `platforms/windows.py` | `netsh wlan` | `run_gui.bat` |

공통 모델, 채널 점유 계산, AP 행 식별자는 `wifi_core.py`에 있고, 실행 시
`wifi_platform.py`가 OS를 감지해 알맞은 수집기를 선택합니다. GUI와 CLI를 바꿀 때는
플랫폼 코드를 건드릴 필요가 없습니다. OS별 수집기 변경은 해당 `platforms/` 파일에만
반영합니다.

## Ubuntu 설치와 실행

Ubuntu Desktop에서 NetworkManager와 Tk를 설치합니다.

```bash
sudo apt update
sudo apt install network-manager python3-tk
python3 -m pip install -r requirements.txt
chmod +x run_gui.sh
./run_gui.sh
```

터미널 모니터는 다음과 같이 실행합니다.

```bash
python3 wifi_scan.py
python3 wifi_scan.py --once
```

`nmcli`는 스캔할 네트워크 인터페이스에 NetworkManager가 연결되어 있어야 합니다.
Linux와 Windows가 제공하는 스캔 정보에는 채널 폭과 noise가 없을 수 있으므로, 해당 경우
분석기는 20 MHz 폭으로 표시하고 SNR은 비워 둡니다.

기본 GUI는 **진단 AP만** 표시합니다. 현재 연결 AP와 실제 주파수가 겹치면서 현재 신호보다
15 dB 이내인 AP만 목록과 그래프에 남습니다. 숨김 SSID는 기본적으로 요약 수치에만 반영되고,
필요할 때만 **숨김 SSID** 토글로 펼칠 수 있습니다. `C/I`는 현재 연결 신호와 겹치는 AP의
합산 신호를 비교한 추정치이며, `높음` 또는 `심각`은 간섭 가능성을 뜻합니다. 실제 속도 문제는
인터넷 회선, AP 부하, 클라이언트 상태도 함께 확인해야 합니다.

## macOS 설치와 실행

```bash
python3 -m pip install -r requirements.txt
open WiFiAnalyzer.app
```

`pyobjc-framework-CoreWLAN`이 설치되면 BSSID를 포함한 CoreWLAN 스캔을 사용합니다.
없으면 `system_profiler` 대체 경로를 사용합니다. macOS 14 이상에서는 Wi-Fi 목록을 위해
WiFi Analyzer의 위치 접근을 허용해야 할 수 있습니다.

macOS 앱 번들은 기존과 같이 만들 수 있습니다.

```bash
python3 -m pip install py2app
python3 setup.py py2app
open "dist/WiFi Analyzer.app"
```

## Windows 설치와 실행

Python 3와 Tk가 포함된 Windows 환경에서 다음을 실행합니다.

```bat
python -m pip install -r requirements.txt
run_gui.bat
```

Windows는 기본 제공 `netsh wlan`을 사용합니다. WLAN AutoConfig 서비스와 무선 어댑터가
활성화되어 있어야 합니다.

## 검증

```bash
python3 -m unittest discover -s tests -v
```
