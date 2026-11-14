# Building and releasing

WiFi Analyzer is packaged with PyInstaller. A build must run on the target
operating system; PyInstaller does not cross-compile Windows, macOS, and Linux
binaries from one host.

## Local build

Set the version explicitly when producing a distributable build. Invalid or
missing values safely fall back to `0.0.0`; a leading `v` and prerelease suffix
are accepted and normalized for the macOS bundle metadata.

macOS and Linux:

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
WIFI_ANALYZER_VERSION=1.2.0 python3 -m PyInstaller --clean --noconfirm wifi_analyzer.spec
```

Windows PowerShell:

```powershell
py -3 -m pip install -r requirements.txt -r requirements-build.txt
$env:WIFI_ANALYZER_VERSION = "1.2.0"
py -3 -m PyInstaller --clean --noconfirm wifi_analyzer.spec
```

The output is written to `dist/`:

- macOS: `dist/WiFiAnalyzer.app`
- Windows: `dist/WiFiAnalyzer/WiFiAnalyzer.exe`
- Ubuntu/Linux: `dist/WiFiAnalyzer/WiFiAnalyzer`

The application icons are versioned in `assets/` and included in each build
(`.icns` on macOS, `.ico` on Windows, and a runtime PNG). To regenerate them
from the shared vector geometry, run `python3 tools/build_icons.py` after
installing the build requirements. The source SVG is generated alongside them.

Ubuntu runtime scanning uses NetworkManager's `nmcli`, with optional `iw`
enrichment. Install `network-manager`, `iw`, and `python3-tk` for source execution.
Missing `iw`, permission failures, and unsupported survey data do not disable
NetworkManager collection. Windows prefers the built-in Native Wi-Fi API and
falls back to `netsh wlan`, using the Windows OEM code page when necessary.
Windows subprocesses are created without console windows. The executable
manifest enables system DPI awareness and long paths without requesting
administrator privileges. macOS includes CoreWLAN inside
the application bundle and asks for location access when SSID/BSSID visibility
requires it.

## GitHub Actions

`.github/workflows/release.yml` builds all three platforms in either of these
ways:

1. Run **Build and release** manually from the Actions page and optionally enter
   an application version. This creates downloadable workflow artifacts for 14
   days without publishing a release.
2. Push a tag matching `v*`, for example:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

   The workflow removes the leading `v`, writes the normalized tag version into
   the app metadata, builds Ubuntu x86_64, macOS universal2, and Windows x86_64
   packages, then creates a GitHub Release and attaches all three archives.

The macOS CI bundle is ad-hoc signed for structural validation. Public
distribution without a Gatekeeper warning additionally requires an Apple
Developer ID certificate, hardened runtime signing, and notarization.

The Windows executable is also unsigned. Windows SmartScreen may warn on first
launch; warning-free public distribution requires an Authenticode code-signing
certificate and a signing step in the release workflow.

## Verification before a public release

Offline parser and logger tests run without a wireless adapter. The CI workflow
can be dispatched manually and also runs for `codex/**`, `dev`, and `main` pushes.
Linux CI exercises the GUI and logging under Xvfb. This prepares the changes for
testing; it does not certify scanning on physical Windows or Linux hardware.

On the target desktop, first run `python3 tools/verify_gui.py` (Windows:
`py -3 tools/verify_gui.py`) with the runtime dependencies installed. It uses
synthetic measurements and temporary log files, and requests no Wi-Fi access.

Then verify these behaviors against the actual device:

1. **Windows 10/11:** Native Wi-Fi is shown as the source; current BSSID, RSSI,
   channel, and link rate match the connected network. If location access is
   denied, confirm an actionable permission message or working netsh fallback.
   Missing noise/SNR is expected. HT/VHT/HE widths depend on the advertised data.
2. **Ubuntu with NetworkManager:** Compare the selected interface and signal
   against `iw dev <interface> link`; compare current width against `info`, and
   noise against the in-use entry in `survey dump`. Missing optional data must
   remain missing. Also verify the app still works when `iw` is unavailable.
3. **Both systems:** Change AP/channel and disconnect/reconnect. Old path-test
   results must clear. Unknown widths must limit recommendations; approximate
   RSSI must carry the `≈` marker. An unsupported width must not become 20 MHz.
4. Start logging, collect several samples, run a path test, pause/resume, stop,
   and close the app. Check CSV/JSONL timestamps, provenance flags, and the final
   session event. Try a Korean directory name and a path containing spaces.
5. Open the packaged app without Python installed and check 100%/150%/200% display
   scaling, Korean text, horizontal scrolling, icon, and keyboard shortcuts.

Publish a version tag after these device checks. A manual **Build and release**
run can prepare platform artifacts without publishing a GitHub Release.

For a downloadable test build before device verification, use a tag such as
`v1.1.0-rc.1`. RC tags run the same platform checks and packaging, and publish a
GitHub prerelease with a device-verification notice. A tag without the RC suffix
publishes a regular release. The numeric portion is used for macOS bundle versions.
