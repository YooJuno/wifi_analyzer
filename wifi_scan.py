#!/usr/bin/env python3
"""실시간 Wi-Fi 채널 간섭 모니터 (macOS)

같은 채널 또는 겹치는 채널을 쓰는 AP들이 서로 얼마나 간섭하는지 보여준다.

데이터 소스는 두 가지이며 자동으로 선택한다.
  CoreWLAN        : BSSID 까지 나온다. pip install pyobjc-framework-CoreWLAN 필요
  system_profiler : 의존성 없이 동작하지만 BSSID 를 주지 않는다 (macOS 14+ 제약)

SSID 가 <redacted> 로 나오면 위치 서비스 권한 문제다.
  시스템 설정 → 개인정보 보호 및 보안 → 위치 서비스 → 터미널 켜기

사용법:
  python3 wifi_scan.py                 5초 주기
  python3 wifi_scan.py -i 2            2초 주기
  python3 wifi_scan.py --once          한 번만 출력
"""

import argparse
import json
import re
import subprocess
import sys
import time

# ── 5 GHz 채널 본딩 블록 ────────────────────────────────────────────
BLOCKS_40 = [(36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112),
             (116, 120), (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)]
BLOCKS_80 = [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)]
BLOCKS_160 = [(36, 64), (100, 128)]


def ch_to_freq(ch):
    return 2407 + 5 * ch if ch <= 14 else 5000 + 5 * ch


def occupied_range(ch, width):
    """주 채널과 대역폭으로 실제 점유 주파수 구간(MHz)을 구한다."""
    if ch <= 14:                                    # 2.4 GHz 는 항상 20 MHz 로 취급
        c = ch_to_freq(ch)
        return c - 10, c + 10
    blocks = {40: BLOCKS_40, 80: BLOCKS_80, 160: BLOCKS_160}.get(width)
    if blocks:
        for lo, hi in blocks:
            if lo <= ch <= hi:
                return ch_to_freq(lo) - 10, ch_to_freq(hi) + 10
    c = ch_to_freq(ch)                              # 20 MHz 또는 블록 밖
    return c - 10, c + 10


def overlap_mhz(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def dbm_to_mw(d):
    return 10 ** (d / 10.0)


# ── 데이터 수집 ─────────────────────────────────────────────────────
class AP:
    __slots__ = ("ssid", "bssid", "ch", "width", "band", "rssi", "noise", "phy", "sec", "cur")

    def __init__(self, ssid, bssid, ch, width, band, rssi, noise, phy, sec, cur=False):
        self.ssid, self.bssid, self.ch, self.width = ssid, bssid, ch, width
        self.band, self.rssi, self.noise = band, rssi, noise
        self.phy, self.sec, self.cur = phy, sec, cur

    @property
    def rng(self):
        return occupied_range(self.ch, self.width)


CH_RE = re.compile(r"(\d+)\s*\((\d)GHz(?:,\s*(\d+)MHz)?\)")
SN_RE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")


def _parse_entry(name, e, cur=False):
    m = CH_RE.search(str(e.get("spairport_network_channel", "")))
    if not m:
        return None
    ch = int(m.group(1))
    band = int(m.group(2))
    width = int(m.group(3)) if m.group(3) else 20
    sn = SN_RE.search(str(e.get("spairport_signal_noise", "")))
    rssi, noise = (int(sn.group(1)), int(sn.group(2))) if sn else (-99, -99)
    return AP(name, None, ch, width, band, rssi, noise,
              str(e.get("spairport_network_phymode", "")),
              str(e.get("spairport_security_mode", "")).replace("spairport_security_mode_", ""),
              cur)


def scan_system_profiler():
    out = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                         capture_output=True, text=True, timeout=40)
    data = json.loads(out.stdout)
    aps = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "spairport_current_network_information" and isinstance(v, dict):
                    ap = _parse_entry(v.get("_name", "?"), v, cur=True)
                    if ap:
                        aps.append(ap)
                elif k == "spairport_airport_other_local_wireless_networks" and isinstance(v, list):
                    for it in v:
                        ap = _parse_entry(it.get("_name", "?"), it)
                        if ap:
                            aps.append(ap)
                else:
                    walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(data)
    return aps


def scan_corewlan():
    import CoreWLAN
    iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
    nets, _ = iface.scanForNetworksWithName_error_(None, None)
    cur_bssid = iface.bssid()
    aps = []
    for n in (nets or []):
        wc = n.wlanChannel()
        if wc is None:
            continue
        ch = int(wc.channelNumber())
        width = {0: 20, 1: 20, 2: 40, 3: 40, 4: 80, 5: 160}.get(int(wc.channelWidth()), 20)
        band = 2 if ch <= 14 else 5
        bssid = n.bssid()
        aps.append(AP(n.ssid() or "<hidden>", bssid, ch, width, band,
                      int(n.rssiValue()), int(n.noiseMeasurement()),
                      "", "", bssid is not None and bssid == cur_bssid))
    return aps


# ── 출력 ────────────────────────────────────────────────────────────
BAR = "█"

def bar(v, vmax, n=12):
    if vmax <= 0:
        return " " * n
    f = int(round(n * min(1.0, v / vmax)))
    return BAR * f + "·" * (n - f)


def render(aps, source, elapsed):
    lines = []
    now = time.strftime("%H:%M:%S")
    lines.append(f"\033[1m실시간 Wi-Fi 채널 간섭 모니터\033[0m   {now}   "
                 f"AP {len(aps)}개   source={source}   ({elapsed:.1f}s)")
    lines.append("")

    cur = next((a for a in aps if a.cur), None)
    if cur:
        snr = cur.rssi - cur.noise
        mark = "양호" if snr >= 25 else ("주의" if snr >= 15 else "불량")
        b = f"  {cur.bssid}" if cur.bssid else ""
        lines.append(f"현재 접속  {cur.ssid}{b}")
        lines.append(f"           ch{cur.ch} ({cur.band}GHz, {cur.width}MHz)   "
                     f"{cur.rssi}dBm / noise {cur.noise}dBm   SNR {snr}dB  [{mark}]")
        lines.append("")

    # 채널별 집계
    chans = {}
    for a in aps:
        chans.setdefault((a.band, a.ch, a.width), []).append(a)

    lines.append("\033[1m채널별 AP 밀집도\033[0m")
    lines.append("  대역  ch   폭    AP  최강신호  합산간섭")
    rows = []
    for (band, ch, width), group in chans.items():
        tot = sum(dbm_to_mw(a.rssi) for a in group)
        rows.append((band, ch, width, len(group), max(a.rssi for a in group), tot,
                     any(a.cur for a in group)))
    vmax = max((r[5] for r in rows), default=1)
    for band, ch, width, n, best, tot, is_cur in sorted(rows, key=lambda r: -r[5])[:12]:
        tag = " \033[36m← 현재\033[0m" if is_cur else ""
        lines.append(f"  {band}GHz  {ch:<4}{width:>3}MHz {n:>4}  {best:>6}dBm  "
                     f"{bar(tot, vmax)}{tag}")
    lines.append("")

    # 현재 채널을 침범하는 AP
    if cur:
        mine = cur.rng
        rivals = []
        for a in aps:
            if a is cur or a.band != cur.band:
                continue
            ov = overlap_mhz(mine, a.rng)
            if ov > 0:
                rivals.append((ov, a))
        rivals.sort(key=lambda t: (-t[1].rssi))

        co = [a for ov, a in rivals if a.ch == cur.ch]
        adj = [a for ov, a in rivals if a.ch != cur.ch]
        noise_mw = sum(dbm_to_mw(a.rssi) for ov, a in rivals)
        margin = cur.rssi - (10 * __import__("math").log10(noise_mw) if noise_mw > 0 else -99)

        lines.append(f"\033[1m내 채널을 침범하는 AP\033[0m   "
                     f"동일채널 {len(co)}대 · 부분겹침 {len(adj)}대 · "
                     f"신호 대 간섭 여유 {margin:+.0f}dB")
        if not rivals:
            lines.append("  없음 — 깨끗한 채널입니다")
        for ov, a in rivals[:14]:
            kind = "동일채널" if a.ch == cur.ch else f"겹침{ov:>3}MHz"
            b = f"  {a.bssid}" if a.bssid else ""
            lines.append(f"  {a.rssi:>4}dBm  ch{a.ch:<4}{a.width:>3}MHz  {kind}   "
                         f"{a.ssid[:28]:<28}{b}")
        if len(rivals) > 14:
            lines.append(f"  … 외 {len(rivals) - 14}대")
    lines.append("")
    lines.append("\033[2mCtrl+C 종료\033[0m")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="실시간 Wi-Fi 채널 간섭 모니터")
    p.add_argument("-i", "--interval", type=float, default=5.0, help="갱신 주기(초), 기본 5")
    p.add_argument("--once", action="store_true", help="한 번만 출력")
    args = p.parse_args()

    try:
        import CoreWLAN  # noqa: F401
        scan, source = scan_corewlan, "CoreWLAN (BSSID 포함)"
    except ImportError:
        scan, source = scan_system_profiler, "system_profiler (BSSID 없음)"

    warned = False
    try:
        while True:
            t0 = time.time()
            try:
                aps = scan()
            except Exception as e:
                print(f"스캔 실패: {e}", file=sys.stderr)
                if args.once:
                    return 1
                time.sleep(args.interval)
                continue

            out = render(aps, source, time.time() - t0)
            if not args.once:
                sys.stdout.write("\033[H\033[J")
            print(out)

            if not warned and any(a.ssid.startswith("<redacted") for a in aps):
                print("\n\033[33mSSID가 가려져 있습니다. 시스템 설정 → 개인정보 보호 및 보안"
                      " → 위치 서비스 에서 터미널을 켜세요.\033[0m")
                warned = True

            if args.once:
                return 0
            time.sleep(max(0.5, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
