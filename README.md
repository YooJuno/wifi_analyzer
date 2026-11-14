# WiFi Analyzer

Windows, macOS, Ubuntu에서 주변 Wi-Fi와 현재 연결 상태를 함께 분석하는 데스크톱
진단 도구입니다. 단순히 AP 목록을 보여주는 대신 다음 질문에 답하도록 구성했습니다.

- 현재 신호나 SNR이 실제로 나쁜가?
- 같은 채널 경쟁 또는 부분 겹침이 있는가?
- 신호가 시간에 따라 불안정한가?
- Wi-Fi가 아니라 공유기, ISP, DNS 또는 HTTPS 구간의 문제인가?
- 주변 관측 기준으로 어떤 채널이 상대적으로 여유로운가?

## 주요 화면

- **모니터**: 현재 연결 수치, 최근 5분 신호 그래프, 채널 후보와 점검 결과
- **네트워크**: SSID/BSSID, 대역, 채널 폭, RSSI/SNR, BSS Load, PHY, 보안
- **채널 분석**: 2.4/5/6GHz 점유 지도와 후보 채널의 상대 RF 압력
- **시간 추이**: 선택한 BSS의 RSSI와 noise 변화, 5분·15분·1시간 범위 선택
- **경로 진단**: 게이트웨이, 외부 IP, DNS, HTTPS를 분리 검사
- **리포트 저장**: 연결·관측·진단·추천·경로 검사 결과를 JSON으로 저장

채널 곡선과 RF 압력은 주변 BSS 신호로 계산한 **상대 비교값**입니다. 실제 스펙트럼,
airtime, 재전송률 또는 비 Wi-Fi 간섭을 직접 측정한 값은 아닙니다.

채널 비교에서는 현재 연결된 BSS를 경쟁 신호에서 제외합니다. 관측 근거가 같은
후보끼리는 현재 채널 블록을 우선 유지하며, 신호 불안정 판정에는 최근 5분만
사용합니다. 경로 검사는 네 구간을 병렬로 확인하고 연결이 바뀌면 이전 결과를
해제합니다. 화면의 메모리 기록은 최대 1시간·512개 네트워크로 제한합니다.

화면은 시스템 글꼴과 단위가 표시된 표·그래프를 사용합니다. 파일 메뉴에서 JSON
리포트를 저장하고 측정 메뉴에서 스캔·일시정지·경로 검사를 실행할 수 있습니다.
macOS는 `⌘R`·`⌘P`·`⌘D`·`⌘S`, 다른 OS는 `Ctrl` 조합을 지원합니다.

## 연속 측정 로그

상단의 **기록 시작…**을 누르고 저장 폴더를 선택하면 별도의
`wifi-log-날짜-시각-세션ID` 폴더에 스캔 결과를 계속 저장합니다.
**기록 중지**로 마감하며, 파일 메뉴의 **로그 폴더 열기**로 결과를 확인합니다.
macOS는 `⌘L`, 다른 OS는 `Ctrl+L`로 시작·중지를 전환할 수 있습니다.

- `measurements-0001.csv`: 시간·연결·RSSI·잡음·SNR·채널·링크 속도·겹치는 BSS 수·
  상대 RF 값·진단 코드를 담는 요약입니다. UTF-8 BOM을 포함해 Excel에서도 읽을 수 있습니다.
- `measurements-0001.jsonl`: 주변 AP 목록, 겹치는 AP와 진단 근거, 채널 후보를 담습니다.
  직접 실행한 경로 검사 결과, 스캔 오류, 일시정지·재개도 별도 이벤트로 기록합니다.

각 스캔 결과는 백그라운드에서 저장 후 flush·fsync합니다. 파일은 약 10MiB마다
같은 번호의 CSV·JSONL 쌍으로 분할하며 기존 세션이나 이전 파일을 덮어쓰지 않습니다.
디스크 로그는 화면의 1시간 기록 제한과 별개로 중지할 때까지 계속되며,
기존 로그를 자동 삭제하지 않습니다. 저장 실패나 대기열 초과 시 기록을 중지하고
화면에 실패를 표시합니다. 앱을 정상 종료하면 이미 접수한 기록까지 저장합니다.

미제공 측정값은 CSV에서 빈칸, JSONL에서 `null`로 남습니다. RF 값과 겹침 기록은
주변 Wi-Fi 관측에 근거한 추정으로, 실제 간섭이나 채널 사용률의 직접 측정값은 아닙니다.

## 플랫폼별 수집

| 플랫폼 | 데이터 소스 | 비고 |
| --- | --- | --- |
| macOS | CoreWLAN, `system_profiler` fallback | RSSI/noise, 폭, PHY, 보안, 일부 BSS Load |
| Ubuntu/Linux | NetworkManager `nmcli` + 선택적 `iw` | 드라이버가 제공하면 실제 RSSI·폭·현재 채널 잡음 보강 |
| Windows | Native Wi-Fi API, `netsh` fallback | 실제 RSSI·HT/VHT/HE 채널 폭 수집, 잡음·SNR 미제공 |

`≈`가 붙은 RSSI는 신호 백분율에서 환산한 추정값입니다. 로그에도
`rssi_estimated`로 구분합니다. 현재 대역에 폭을 알 수 없는 주변 AP가 있으면
전체 겹침 수치와 채널 추천을 제한하고 동일 주채널 경쟁 신호만 판정합니다.
폭 미제공 AP는 채널 그래프에 세로 표식으로 표시합니다. 지원하지 않는
80+80MHz·EHT 동작 정보는 폭 미제공으로 처리합니다.

Windows는 영문·한글 `netsh` 출력과 OEM 문자 인코딩을 처리하며 Native API 실패 시
대체 수집합니다. 위치 권한이 필요하면 Windows 설정의 위치 서비스와 데스크톱 앱
위치 접근 안내를 표시합니다. Linux는 `iw` 명령이 없거나 권한·드라이버 제한이 있어도
`nmcli` 수집을 계속합니다. `iw`는 장치 설정을 바꾸지 않고 상태·캐시 조회에만 사용합니다.

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
sudo apt install network-manager python3-tk iw
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
measurement_log.py     비동기 CSV·JSONL 측정 세션 기록
wifi_platform.py       실행 OS에 맞는 backend 선택
platforms/
  macos.py             CoreWLAN / system_profiler
  linux.py             nmcli + iw 상태·캐시 조회
  windows.py           Windows 수집 및 netsh fallback
  windows_native.py    Native Wi-Fi API 및 고정 폭 구조체 파서
  commands.py          한글 출력 디코딩·콘솔 창 숨김
tests/                 파서·계산·진단 단위 테스트
```

## 테스트

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

무선 스캔과 위치 접근 없이 실제 GUI·그래프·로깅을 점검하려면 데스크톱에서
`python3 tools/verify_gui.py`를 실행하세요. Windows에서는 `py -3 tools/verify_gui.py`를
사용합니다. 이 검사는 OS별 드라이버·무선 장치의 최종 검증과 별개입니다.

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
- macOS Apple Silicon·Intel: `WiFiAnalyzer-macOS-universal2.zip`
- Windows x86_64: `WiFiAnalyzer-Windows-x86_64.zip`

각 압축 파일과 함께 SHA-256 파일(`.sha256`)을 제공합니다. 수동 실행의 버전은 `X.Y.Z`, 릴리스 태그는 `vX.Y.Z` 형식으로 검사하며, 세 OS의 검사·빌드가 모두 성공해야 태그 릴리스를 공개합니다.

macOS 패키지는 Intel 러너에서 체크섬으로 검증한 Python.org universal2 배포판을 사용합니다. 앱과 포함된 모든 Mach-O 파일의 Apple Silicon·Intel 슬라이스를 검사합니다. ARM 러너 대기에 의존하지 않지만, 아키텍처 검사와 Apple Silicon 실기 실행은 별개입니다. 패키지에는 Python이 포함되므로 사용자가 Python을 따로 설치할 필요는 없습니다.

자세한 로컬 빌드와 서명 안내는 [BUILDING.md](BUILDING.md)를 참고하세요.

GitHub Actions에서 생성하는 macOS 앱은 ad-hoc 서명, Windows 실행 파일은 무서명
상태입니다. 따라서 공개 배포본을 처음 실행할 때 macOS Gatekeeper 또는 Windows
SmartScreen 경고가 나타날 수 있습니다. 경고 없는 공개 배포에는 각 플랫폼의 코드
서명 인증서와 macOS notarization이 추가로 필요합니다.
