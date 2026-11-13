#!/usr/bin/env python3
"""Cross-platform terminal Wi-Fi diagnostics."""

from __future__ import annotations

import argparse
import sys
import time

from diagnostics import analyze_connection, recommend_channels, run_path_diagnostics
from wifi_core import AP, assign_keys
from wifi_platform import platform_label, scan_snapshot


COLORS = {
    "critical": "\033[31m", "warning": "\033[33m", "info": "\033[36m",
    "ok": "\033[32m", "unknown": "\033[2m",
}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"


def _current_ap(snapshot):
    connection = snapshot.connection
    current = None
    if connection.bssid:
        wanted = connection.bssid.lower().replace("-", ":")
        current = next((ap for ap in snapshot.aps
                        if (ap.bssid or "").lower().replace("-", ":") == wanted), None)
    current = current or next((ap for ap in snapshot.aps if ap.cur), None)
    if current is None and connection.connected and connection.channel and connection.band:
        current = AP(connection.ssid, connection.bssid, connection.channel,
                     connection.width or 20, connection.band,
                     connection.rssi if connection.rssi is not None else -99,
                     connection.noise, connection.security, connection.phy, True,
                     width_known=connection.width is not None)
        snapshot.aps.append(current)
    if current:
        current.cur = True
        current.rssi = connection.rssi if connection.rssi is not None else current.rssi
        current.noise = connection.noise if connection.noise is not None else current.noise
        current.width = connection.width or current.width
        current.ch = connection.channel or current.ch
        current.band = connection.band or current.band
        current.phy = connection.phy or current.phy
        current.sec = connection.security or current.sec
    return current


def render(snapshot, elapsed, path_result=None):
    aps = assign_keys(snapshot.aps)
    current = _current_ap(snapshot)
    now = time.strftime("%H:%M:%S", time.localtime(snapshot.timestamp))
    lines = [
        f"{BOLD}WiFi Analyzer · 진단 요약{RESET}",
        f"{platform_label()} · {snapshot.source} · {now} · BSS {len(aps)}개 · {elapsed:.1f}초",
        "",
    ]

    if current:
        connection = snapshot.connection
        snr = current.snr or None
        rate = f"{connection.tx_rate:.0f} Mbps" if connection.tx_rate else "미제공"
        width = f"{current.width} MHz" if current.width_known else "폭 미제공"
        lines.extend([
            f"{BOLD}현재 연결{RESET}  {current.ssid or '(숨김 SSID)'}",
            f"  {current.bssid or 'BSSID 미제공'}",
            f"  {current.band} GHz · ch{current.ch} · {width} · "
            f"{current.rssi} dBm · SNR {snr if snr is not None else '–'} dB",
            f"  PHY {connection.phy or current.phy or '–'} · Tx rate {rate} · "
            f"보안 {connection.security or current.sec or '–'}",
            "",
        ])
    else:
        lines.extend([f"{COLORS['critical']}현재 Wi-Fi 연결을 찾지 못했습니다.{RESET}", ""])

    findings = analyze_connection(current, aps, source_reliable=not snapshot.warnings)
    lines.append(f"{BOLD}자동 진단{RESET}")
    for finding in findings:
        color = COLORS.get(finding.severity, "")
        lines.append(f"  {color}● {finding.title}{RESET} — {finding.evidence}")
        lines.append(f"    조치: {finding.action}")

    if current:
        width = 20 if current.band == 2 else (80 if current.width >= 80 else 20)
        candidates = recommend_channels(aps, current.band, width)[:3]
        if candidates:
            lines.extend(["", f"{BOLD}상대적으로 여유로운 채널{RESET}"])
            for score in candidates:
                pressure = ("겹침 없음" if score.pressure_dbm is None
                            else f"추정 압력 {score.pressure_dbm:.0f} dBm")
                dfs = " · DFS" if score.dfs else ""
                lines.append(
                    f"  {score.band} GHz ch{score.channel}/{score.width} MHz · "
                    f"{pressure} · 신뢰도 {score.confidence}{dfs}")
            lines.append(f"  {DIM}주변 BSS 신호 기반 비교이며 실제 airtime 측정값이 아닙니다.{RESET}")

    if snapshot.warnings:
        lines.extend(["", f"{COLORS['warning']}데이터 제한: "
                      + " · ".join(snapshot.warnings) + RESET])

    if path_result:
        lines.extend(["", f"{BOLD}경로 진단{RESET}"])
        for check in path_result.checks:
            color = COLORS.get(check.status, "")
            latency = f"{check.latency_ms:.1f} ms" if check.latency_ms is not None else "–"
            lines.append(f"  {color}● {check.stage:<8}{RESET} {check.target:<24} "
                         f"{latency:<10} {check.detail}")
        color = COLORS.get(path_result.severity, "")
        lines.append(f"  {color}{path_result.conclusion}{RESET}")

    lines.extend(["", f"{DIM}Ctrl+C 종료{RESET}"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="크로스플랫폼 Wi-Fi 문제 진단")
    parser.add_argument("-i", "--interval", type=float, default=10.0,
                        help="주변 BSS 스캔 주기(초), 기본 10")
    parser.add_argument("--once", action="store_true", help="한 번만 출력")
    parser.add_argument("--path", action="store_true",
                        help="게이트웨이·인터넷·DNS·HTTPS 경로 진단도 실행")
    args = parser.parse_args()
    path_result = None
    try:
        while True:
            started = time.monotonic()
            try:
                snapshot = scan_snapshot()
                if args.path and (path_result is None or args.once):
                    path_result = run_path_diagnostics()
            except Exception as exc:
                print(f"스캔 실패: {exc}", file=sys.stderr)
                if args.once:
                    return 1
                time.sleep(max(1.0, args.interval))
                continue
            if not args.once:
                sys.stdout.write("\033[H\033[J")
            print(render(snapshot, time.monotonic() - started, path_result))
            if args.once:
                return 0
            time.sleep(max(5.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
