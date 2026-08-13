#!/usr/bin/env python3
"""Real-time Wi-Fi channel interference monitor.

같은 채널 또는 겹치는 채널을 쓰는 AP들이 서로 얼마나 간섭하는지 보여준다.

사용법:
  python3 wifi_scan.py                 5초 주기
  python3 wifi_scan.py -i 2            2초 주기
  python3 wifi_scan.py --once          한 번만 출력
"""

import argparse
import sys
import time

from wifi_core import interference_level, interference_margin_db, relevant_interferers
from wifi_platform import platform_label, scan_source, scan_wifi

def render(aps, source, elapsed):
    lines = []
    now = time.strftime("%H:%M:%S")
    lines.append(f"\033[1m실시간 Wi-Fi 채널 간섭 모니터\033[0m   {now}   "
                 f"AP {len(aps)}개   source={source}   ({elapsed:.1f}s)")
    lines.append("")

    cur = next((a for a in aps if a.cur), None)
    if cur:
        snr = cur.rssi - cur.noise if cur.noise is not None else None
        mark = "양호" if snr is not None and snr >= 25 else ("주의" if snr is not None and snr >= 15 else "미측정")
        b = f"  {cur.bssid}" if cur.bssid else ""
        lines.append(f"현재 접속  {cur.ssid}{b}")
        measurement = (f"noise {cur.noise}dBm   SNR {snr}dB" if snr is not None
                       else "noise/SNR 미제공")
        lines.append(f"           ch{cur.ch} ({cur.band}GHz, {cur.width}MHz)   "
                     f"{cur.rssi}dBm / {measurement}  [{mark}]")
        lines.append("")

    # Focus on interferers that are close enough in signal strength to matter.
    if cur:
        rivals = relevant_interferers(cur, aps)

        co = [a for ov, a in rivals if a.ch == cur.ch]
        adj = [a for ov, a in rivals if a.ch != cur.ch]
        margin = interference_margin_db(cur, rivals)
        level = interference_level(margin)
        hidden = [a for _, a in rivals if not a.ssid]
        named = [(ov, a) for ov, a in rivals if a.ssid]

        lines.append(f"\033[1m현재 연결 간섭 진단: {level}\033[0m   "
                     f"동일채널 {len(co)}대 · 부분겹침 {len(adj)}대 · "
                     + (f"추정 C/I {margin:+.0f}dB" if margin is not None else "유효 간섭원 없음"))
        if not rivals:
            lines.append("  현재 연결 신호보다 15dB 이내에서 겹치는 AP가 없습니다.")
        for ov, a in named[:6]:
            kind = "동일채널" if a.ch == cur.ch else f"겹침{ov:>3}MHz"
            b = f"  {a.bssid}" if a.bssid else ""
            lines.append(f"  {a.rssi:>4}dBm  ch{a.ch:<4}{a.width:>3}MHz  {kind}   "
                         f"{a.ssid[:28]:<28}{b}")
        if hidden:
            strongest = max(ap.rssi for ap in hidden)
            lines.append(f"  숨김 SSID {len(hidden)}대는 요약됨 (최강 신호 {strongest}dBm)")
        if len(named) > 6:
            lines.append(f"  이름 있는 AP {len(named) - 6}대는 신호가 더 약해 요약됨")
    lines.append("")
    lines.append("\033[2mCtrl+C 종료\033[0m")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="실시간 Wi-Fi 채널 간섭 모니터")
    p.add_argument("-i", "--interval", type=float, default=5.0, help="갱신 주기(초), 기본 5")
    p.add_argument("--once", action="store_true", help="한 번만 출력")
    args = p.parse_args()

    scan, source = scan_wifi, scan_source()
    source = f"{platform_label()} {source}"

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

            if not warned and platform_label() == "macOS" and any(
                    (a.ssid or "").startswith("<redacted") for a in aps):
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
