# WiFi Analyzer

Windows, macOS, Ubuntu에서 주변 Wi-Fi와 현재 연결 상태를 함께 분석하는 데스크톱
진단 도구입니다. 단순히 AP 목록을 보여주는 대신 다음 질문에 답하도록 구성했습니다.

- 현재 신호나 SNR이 실제로 나쁜가?
- 같은 채널 경쟁 또는 부분 겹침이 있는가?
- 신호가 시간에 따라 불안정한가?
- Wi-Fi가 아니라 공유기, ISP, DNS 또는 HTTPS 구간의 문제인가?
- 주변 관측 기준으로 어떤 채널이 상대적으로 여유로운가?

## 주요 화면

- **개요**: 현재 연결, RSSI, SNR, 링크 속도, 자동 진단 근거와 권장 조치
- **네트워크**: SSID/BSSID, 대역, 채널 폭, RSSI/SNR, BSS Load, PHY, 보안
- **채널 분석**: 2.4/5/6GHz 점유 지도와 후보 채널의 상대 RF 압력
- **시간 추이**: 선택한 BSS의 RSSI와 noise 변화
- **경로 진단**: 게이트웨이, 외부 IP, DNS, HTTPS를 분리 검사
- **리포트 저장**: 연결·관측·진단·추천·경로 검사 결과를 JSON으로 저장

채널 곡선과 RF 압력은 주변 BSS 신호로 계산한 **상대 비교값**입니다. 실제 스펙트럼,
airtime, 재전송률 또는 비 Wi-Fi 간섭을 직접 측정한 값은 아닙니다.

## 플랫폼별 수집

| 플랫폼 | 데이터 소스 | 비고 |
| --- | --- | --- |
| macOS | CoreWLAN, `system_profiler` fallback | RSSI/noise, 폭, PHY, 보안, 일부 BSS Load |
| Ubuntu/Linux | NetworkManager `nmcli` | 폭과 noise는 환경에 따라 미제공 |
| Windows | `netsh wlan` | 폭과 noise는 기본 명령에서 미제공 |

## 소스에서 실행

Python 3.12를 권장합니다.

macOS와 Ubuntu/Linux:

```bash
python3 -m pip install -r requirements.txt
python3 wifi_gui.py
```

Windows PowerShell 또는 명령 프롬프트:

```powershell
py -3 -m pip install -r requirements.txt
py -3 wifi_gui.py
```

운영체제별 실행 스크립트도 제공합니다.

```bash
# macOS
./run_gui.command

# Ubuntu
./run_gui.sh

# Windows
run_gui.bat
```

실행 스크립트는 저장소의 Python 소스를 직접 실행하므로 fresh clone에서도 사용할 수
있습니다. 먼저 위의 운영체제별 설치 명령으로 의존성을 설치하세요.

Ubuntu Desktop에서는 NetworkManager와 Tk가 필요합니다.

```bash
sudo apt update
sudo apt install network-manager python3-tk
```

터미널 진단:

```bash
python3 wifi_scan.py --once
python3 wifi_scan.py --once --path
python3 wifi_scan.py --interval 15
```

macOS 14 이상에서 SSID/BSSID가 가려지면 **시스템 설정 → 개인정보 보호 및 보안 →
위치 서비스**에서 WiFi Analyzer 또는 실행한 터미널 앱의 위치 접근을 허용하세요.

## 프로젝트 구조

```text
wifi_gui.py            진단 중심 Tkinter GUI
wifi_scan.py           터미널 진단
diagnostics.py         RF·신호·채널 추천·경로 판정
wifi_core.py           공통 모델과 주파수 계산
wifi_platform.py       실행 OS에 맞는 backend 선택
platforms/
  macos.py             CoreWLAN / system_profiler
  linux.py             nmcli
  windows.py           netsh
tests/                 파서·계산·진단 단위 테스트
```

## 테스트

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

`main` 브랜치의 push와 pull request에서는 GitHub Actions가 Ubuntu, macOS, Windows에서
컴파일 및 단위 테스트를 실행합니다.

## 빌드와 자동 배포

독립 실행형 패키지는 PyInstaller로 만듭니다.

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
WIFI_ANALYZER_VERSION=1.0.0 python3 -m PyInstaller --clean --noconfirm wifi_analyzer.spec
```

Windows용 로컬 빌드 명령과 버전 설정 방법은 [BUILDING.md](BUILDING.md)에 있습니다.

GitHub Actions의 **Build and release**를 수동 실행하면 입력한 버전으로 세 운영체제의
artifact를 생성합니다. `v*` 태그를 push하면 태그 버전이 앱 메타데이터에 반영되고,
테스트와 빌드 후 GitHub Release가 자동 생성됩니다.

```bash
git tag v1.0.0
git push origin v1.0.0
```

생성 파일:

- Ubuntu x86_64: `WiFiAnalyzer-linux-x86_64.tar.gz`
- macOS arm64: `WiFiAnalyzer-macOS-arm64.zip`
- Windows x86_64: `WiFiAnalyzer-Windows-x86_64.zip`

각 압축 파일과 함께 SHA-256 파일(`.sha256`)을 제공합니다. 수동 실행의 버전은 `X.Y.Z`, 릴리스 태그는 `vX.Y.Z` 형식으로 검사하며, 세 OS의 검사·빌드가 모두 성공해야 태그 릴리스를 공개합니다.

자세한 로컬 빌드와 서명 안내는 [BUILDING.md](BUILDING.md)를 참고하세요.

GitHub Actions에서 생성하는 macOS 앱은 ad-hoc 서명, Windows 실행 파일은 무서명
상태입니다. 따라서 공개 배포본을 처음 실행할 때 macOS Gatekeeper 또는 Windows
SmartScreen 경고가 나타날 수 있습니다. 경고 없는 공개 배포에는 각 플랫폼의 코드
서명 인증서와 macOS notarization이 추가로 필요합니다.
