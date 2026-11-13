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

Ubuntu runtime scanning uses NetworkManager's `nmcli`. Install
`network-manager` when it is not already provided by the desktop distribution.
Windows uses the built-in `netsh wlan` command. macOS includes CoreWLAN inside
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
   the app metadata, builds Ubuntu x86_64, macOS arm64, and Windows x86_64
   packages, then creates a GitHub Release and attaches all three archives.

The macOS CI bundle is ad-hoc signed for structural validation. Public
distribution without a Gatekeeper warning additionally requires an Apple
Developer ID certificate, hardened runtime signing, and notarization.

The Windows executable is also unsigned. Windows SmartScreen may warn on first
launch; warning-free public distribution requires an Authenticode code-signing
certificate and a signing step in the release workflow.
