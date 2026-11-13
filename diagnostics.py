"""Platform-neutral Wi-Fi and network-path diagnostics.

The RF values in this module describe *observed BSS pressure*.  They are not a
spectrum-analyser reading and are not a substitute for airtime, retry, or
channel-utilisation telemetry.  Keeping that distinction in the API prevents
the UI from presenting a scan-derived estimate as measured interference.

All radio functions use duck typing.  A scan object only needs attributes such
as ``band``, ``ch``/``channel``, ``width``, ``rssi`` and ``rng``.  This keeps
the diagnostic engine independent from the operating-system scanner.
"""

from __future__ import annotations

import math
import platform
import queue
import re
import socket
import ssl
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "unknown": 4}


@dataclass(frozen=True)
class MetricAssessment:
    status: str
    label: str
    value: Optional[float]
    detail: str


@dataclass(frozen=True)
class RFRelation:
    """Relationship between two advertised Wi-Fi channel ranges."""

    kind: str
    overlap_mhz: float
    target_overlap_ratio: float
    other_overlap_ratio: float
    other: Any


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    severity: str
    evidence: str
    action: str
    scope: str = "wifi"


@dataclass(frozen=True)
class ChannelScore:
    band: int
    channel: int
    width: int
    pressure_dbm: Optional[float]
    overlapping_bss: int
    strong_bss: int
    dfs: bool
    confidence: str
    reason: str


@dataclass(frozen=True)
class PathCheck:
    stage: str
    status: str
    target: str
    latency_ms: Optional[float] = None
    loss_percent: Optional[float] = None
    detail: str = ""


@dataclass(frozen=True)
class PathDiagnostics:
    checked_at: float
    gateway: Optional[str]
    checks: tuple[PathCheck, ...] = field(default_factory=tuple)
    conclusion: str = ""
    severity: str = "unknown"


def assess_rssi(rssi: Optional[float]) -> MetricAssessment:
    """Classify received signal strength using troubleshooting defaults."""
    if rssi is None:
        return MetricAssessment("unknown", "측정 불가", None, "RSSI를 수집하지 못했습니다.")
    if rssi >= -67:
        return MetricAssessment("ok", "양호", rssi, "일반적인 음성·영상 통신에 충분한 신호입니다.")
    if rssi >= -75:
        return MetricAssessment("warning", "주의", rssi, "거리·차폐 영향으로 속도 저하가 생길 수 있습니다.")
    return MetricAssessment("critical", "약함", rssi, "커버리지 또는 단말 위치 문제 가능성이 큽니다.")


def assess_snr(snr: Optional[float]) -> MetricAssessment:
    """Classify signal-to-noise ratio; ``None`` means no noise measurement."""
    if snr is None:
        return MetricAssessment("unknown", "측정 불가", None, "잡음 값이 없어 SNR을 계산하지 못했습니다.")
    if snr >= 25:
        return MetricAssessment("ok", "양호", snr, "신호와 잡음의 여유가 충분합니다.")
    if snr >= 15:
        return MetricAssessment("warning", "주의", snr, "잡음 또는 약한 신호의 영향을 받을 수 있습니다.")
    return MetricAssessment("critical", "낮음", snr, "RF 잡음이나 매우 약한 신호를 확인해야 합니다.")


def _attribute(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(value, name):
            result = getattr(value, name)
            return result() if callable(result) else result
    return default


def _channel(value: Any) -> int:
    return int(_attribute(value, "ch", "channel", default=0) or 0)


def _width(value: Any) -> float:
    return float(_attribute(value, "width", "channel_width", default=20) or 20)


def _band(value: Any) -> int:
    return int(_attribute(value, "band", default=0) or 0)


def _rssi(value: Any) -> Optional[float]:
    result = _attribute(value, "rssi", "signal", default=None)
    return None if result is None else float(result)


def _range(value: Any) -> tuple[float, float]:
    explicit = _attribute(value, "rng", "occupied_range", default=None)
    if explicit is not None:
        return float(explicit[0]), float(explicit[1])
    return candidate_range(_band(value), _channel(value), int(_width(value)))


def overlap_mhz(first: tuple[float, float], second: tuple[float, float]) -> float:
    return max(0.0, min(first[1], second[1]) - max(first[0], second[0]))


def classify_relation(target: Any, other: Any) -> RFRelation:
    """Separate primary-channel contention, bonded-block sharing and overlap."""
    if target is other or _band(target) != _band(other):
        return RFRelation("none", 0.0, 0.0, 0.0, other)
    target_range, other_range = _range(target), _range(other)
    overlap = overlap_mhz(target_range, other_range)
    if not overlap:
        return RFRelation("none", 0.0, 0.0, 0.0, other)
    target_span = max(1.0, target_range[1] - target_range[0])
    other_span = max(1.0, other_range[1] - other_range[0])
    if _channel(target) == _channel(other):
        kind = "same_primary"
    elif math.isclose(target_range[0], other_range[0], abs_tol=0.5) and math.isclose(
            target_range[1], other_range[1], abs_tol=0.5):
        kind = "same_bonded_block"
    else:
        kind = "partial_overlap"
    return RFRelation(kind, overlap, overlap / target_span, overlap / other_span, other)


def rf_relations(target: Any, observations: Iterable[Any]) -> list[RFRelation]:
    relations = [classify_relation(target, other) for other in observations if other is not target]
    return sorted((relation for relation in relations if relation.kind != "none"),
                  key=lambda relation: (_rssi(relation.other) is not None,
                                        _rssi(relation.other) or -200), reverse=True)


def potential_rf_pressure_dbm(target: Any, observations: Iterable[Any]) -> Optional[float]:
    """Return an RSSI/overlap-weighted comparison value, not measured airtime.

    Each BSS contributes ``10 ** (RSSI / 10) * target_overlap_ratio``.  The
    result is useful for comparing channel candidates observed in the same
    scan, but must not be labelled channel utilisation or actual interference.
    """
    total_mw = 0.0
    for relation in rf_relations(target, observations):
        rssi = _rssi(relation.other)
        if rssi is not None:
            total_mw += (10 ** (rssi / 10.0)) * relation.target_overlap_ratio
    return None if total_mw <= 0 else 10 * math.log10(total_mw)


def _frequency(band: int, channel: int) -> float:
    if band == 2:
        return 2484.0 if channel == 14 else 2407.0 + 5 * channel
    if band == 6:
        return 5935.0 if channel == 2 else 5950.0 + 5 * channel
    return 5000.0 + 5 * channel


_BLOCKS = {
    40: ((36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112),
         (116, 120), (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)),
    80: ((36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)),
    160: ((36, 64), (100, 128)),
}


def candidate_range(band: int, channel: int, width: int) -> tuple[float, float]:
    """Return a candidate range used for relative channel scoring."""
    if band == 5:
        for low, high in _BLOCKS.get(width, ()):  # primary can be anywhere in the block
            if low <= channel <= high:
                return _frequency(5, low) - 10, _frequency(5, high) + 10
    if band == 6 and width in {40, 80, 160} and channel >= 1 and (channel - 1) % 4 == 0:
        # 6 GHz 20 MHz primaries are 1, 5, 9, ... .  Bonded blocks contain
        # width/20 adjacent primaries and start again at each block boundary.
        primaries_per_block = width // 20
        primary_index = (channel - 1) // 4
        block_start_index = (primary_index // primaries_per_block) * primaries_per_block
        low = 1 + block_start_index * 4
        high = low + (primaries_per_block - 1) * 4
        return _frequency(6, low) - 10, _frequency(6, high) + 10
    center = _frequency(band, channel)
    return center - width / 2.0, center + width / 2.0


class _Candidate:
    def __init__(self, band: int, channel: int, width: int):
        self.band, self.ch, self.width = band, channel, width
        self.rng = candidate_range(band, channel, width)


def default_channel_candidates(band: int, width: int) -> list[int]:
    if band == 2:
        return [1, 6, 11] if width == 20 else []
    if band == 5 and width == 20:
        return [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161]
    if band == 5 and width == 80:
        return [36, 52, 100, 116, 132, 149]
    if band == 6 and width == 20:
        # Preferred Scanning Channels (PSC) reduce discovery delay.
        return list(range(5, 230, 16))
    if band == 6 and width == 40:
        # One valid primary representing each complete 40 MHz block.
        return list(range(1, 226, 8))
    if band == 6 and width == 80:
        # One PSC in each complete 80 MHz block.
        return list(range(5, 214, 16))
    if band == 6 and width == 160:
        # One PSC in each complete 160 MHz block.
        return list(range(5, 198, 32))
    return []


def evaluate_channels(observations: Sequence[Any], band: int, width: int,
                      candidates: Optional[Sequence[int]] = None) -> list[ChannelScore]:
    """Rank channel candidates using nearby BSS signal and spectral overlap."""
    channels = list(candidates) if candidates is not None else default_channel_candidates(band, width)
    scores = []
    band_observations = [item for item in observations if _band(item) == band]
    for channel in channels:
        candidate = _Candidate(band, channel, width)
        relations = rf_relations(candidate, band_observations)
        pressure = potential_rf_pressure_dbm(candidate, band_observations)
        strong = sum(1 for relation in relations if (_rssi(relation.other) or -200) >= -67)
        dfs = band == 5 and 52 <= channel <= 144
        confidence = "보통" if len(band_observations) >= 3 else "낮음"
        if pressure is None:
            reason = "겹쳐 보이는 BSS 없음 (비 Wi-Fi 간섭·트래픽은 미측정)"
        else:
            reason = (f"겹침 {len(relations)}개 · 강한 BSS {strong}개 · "
                      f"추정 RF 압력 {pressure:.0f} dBm")
        scores.append(ChannelScore(band, channel, width, pressure, len(relations),
                                   strong, dfs, confidence, reason))
    # None means quieter than every measured pressure; non-DFS wins equal scores.
    return sorted(scores, key=lambda item: (
        item.pressure_dbm is not None,
        item.pressure_dbm if item.pressure_dbm is not None else -999.0,
        item.strong_bss,
        item.overlapping_bss,
        item.dfs,
    ))


def _snr_from(target: Any) -> Optional[float]:
    explicit = _attribute(target, "snr", default=None)
    if explicit not in (None, 0):
        return float(explicit)
    rssi = _rssi(target)
    noise = _attribute(target, "noise", default=None)
    if rssi is None or noise is None or float(noise) >= 0:
        return None
    return rssi - float(noise)


def diagnose_connection(current: Any, observations: Sequence[Any],
                        rssi_history: Optional[Sequence[Any]] = None,
                        source_reliable: bool = True) -> list[Finding]:
    """Generate evidence/action findings for the current Wi-Fi connection."""
    if current is None:
        return [Finding("not_connected", "Wi-Fi 연결 없음", "critical",
                        "현재 연결된 BSS를 확인하지 못했습니다.",
                        "Wi-Fi 전원, 연결 상태와 스캔 권한을 확인하세요.")]

    findings: list[Finding] = []
    rssi = _rssi(current)
    rssi_state = assess_rssi(rssi)
    if rssi_state.status != "ok":
        findings.append(Finding("weak_rssi", "현재 신호가 약함", rssi_state.status,
                                f"RSSI {rssi:.0f} dBm" if rssi is not None else "RSSI 미측정",
                                "AP와의 거리·차폐를 줄이거나 5/6GHz 커버리지를 확인하세요."))

    snr = _snr_from(current)
    snr_state = assess_snr(snr)
    if snr_state.status in {"critical", "warning"}:
        findings.append(Finding("low_snr", "신호 대 잡음 여유가 낮음", snr_state.status,
                                f"SNR {snr:.0f} dB",
                                "주변 전파원과 AP 채널을 확인하고 단말 위치를 바꿔 비교하세요."))
    elif snr_state.status == "unknown":
        findings.append(Finding("snr_unavailable", "SNR 측정 불가", "info",
                                snr_state.detail, "RSSI와 경로 지연을 함께 관찰하세요."))

    history_values = []
    for sample in rssi_history or ():
        value = sample[-1] if isinstance(sample, (tuple, list)) else sample
        if isinstance(value, (int, float)):
            history_values.append(float(value))
    if len(history_values) >= 4:
        swing = max(history_values) - min(history_values)
        deviation = statistics.pstdev(history_values)
        if swing >= 20 or deviation >= 7:
            findings.append(Finding("unstable_signal", "신호 변동이 큼", "critical",
                                    f"변동폭 {swing:.0f} dB · 표준편차 {deviation:.1f} dB",
                                    "이동·차폐·로밍 여부를 확인하고 BSSID 변경 시점을 비교하세요."))
        elif swing >= 12 or deviation >= 4:
            findings.append(Finding("unstable_signal", "신호가 불안정함", "warning",
                                    f"변동폭 {swing:.0f} dB · 표준편차 {deviation:.1f} dB",
                                    "단말을 고정한 상태로 다시 측정해 환경 요인을 분리하세요."))

    relations = rf_relations(current, observations)
    audible = [relation for relation in relations
               if (_rssi(relation.other) or -200) >= max(-80, (rssi or -80) - 15)]
    contention = [relation for relation in audible
                  if relation.kind in {"same_primary", "same_bonded_block"}]
    adjacent = [relation for relation in audible if relation.kind == "partial_overlap"]
    if contention:
        strongest = max(_rssi(item.other) or -200 for item in contention)
        findings.append(Finding("cochannel_contention", "같은 채널 블록의 경쟁 BSS", "warning",
                                f"영향 가능 BSS {len(contention)}개 · 최강 {strongest:.0f} dBm",
                                "사용량이 많은 시간대에 지연을 확인하고 더 여유로운 채널을 비교하세요."))
    if adjacent:
        strongest = max(_rssi(item.other) or -200 for item in adjacent)
        severity = "critical" if strongest >= -60 else "warning"
        findings.append(Finding("adjacent_overlap", "강한 부분 겹침 신호", severity,
                                f"부분 겹침 BSS {len(adjacent)}개 · 최강 {strongest:.0f} dBm",
                                "2.4GHz는 20MHz와 1/6/11 채널 계획을 우선 검토하세요."
                                if _band(current) == 2 else
                                f"{_band(current)}GHz에서 다른 채널 블록이나 더 좁은 채널 폭을 검토하세요."))

    if _band(current) == 2 and _width(current) > 20:
        findings.append(Finding("wide_24ghz", "2.4GHz에서 넓은 채널 사용", "warning",
                                f"채널 폭 {_width(current):.0f} MHz",
                                "혼잡 환경에서는 공유기 채널 폭을 20MHz로 설정해 비교하세요."))

    ssid = _attribute(current, "ssid", default=None)
    if ssid and rssi is not None:
        alternatives = [item for item in observations
                        if item is not current and _attribute(item, "ssid", default=None) == ssid
                        and _rssi(item) is not None and (_rssi(item) or -200) >= rssi + 12]
        if alternatives:
            best = max(alternatives, key=lambda item: _rssi(item) or -200)
            findings.append(Finding("sticky_client", "더 강한 동일 SSID BSSID가 보임", "warning",
                                    f"현재 {rssi:.0f} dBm · 대안 {_rssi(best):.0f} dBm",
                                    "로밍 설정과 AP 배치를 확인하고 Wi-Fi 재연결 전후를 비교하세요."))

    security = str(_attribute(current, "sec", "security", default="") or "").lower()
    if any(token in security for token in ("open", "none", "wep", "tkip")):
        findings.append(Finding("weak_security", "취약한 Wi-Fi 보안", "critical",
                                security or "보안 없음", "WPA2-AES 이상 또는 WPA3로 변경하세요."))

    if not source_reliable:
        findings.append(Finding("limited_scan_data", "진단 데이터 신뢰도 낮음", "info",
                                "BSSID·폭·잡음 중 일부를 수집하지 못했습니다.",
                                "운영체제 권한과 Wi-Fi 스캔 도구 설치 상태를 확인하세요."))

    if not findings:
        findings.append(Finding("rf_ok", "뚜렷한 Wi-Fi 무선 문제 없음", "ok",
                                "현재 RSSI/SNR과 관측된 채널 환경은 양호합니다.",
                                "경로 진단으로 게이트웨이·DNS·인터넷 구간을 확인하세요."))
    return sorted(findings, key=lambda finding: SEVERITY_ORDER.get(finding.severity, 99))


def analyze_connection(current: Any, observations: Sequence[Any],
                       history: Optional[Sequence[Any]] = None,
                       source_reliable: bool = True) -> list[Finding]:
    """Public UI-friendly alias for :func:`diagnose_connection`."""
    return diagnose_connection(current, observations, history, source_reliable)


def recommend_channels(observations: Sequence[Any], band: int, width: int,
                       candidates: Optional[Sequence[int]] = None) -> list[ChannelScore]:
    """Return quietest observed channel candidates first.

    The result is a relative recommendation from nearby BSS signals, not a
    measurement of non-Wi-Fi noise or actual channel airtime.
    """
    return evaluate_channels(observations, band, width, candidates)


def default_gateway(system: Optional[str] = None) -> Optional[str]:
    """Return the IPv4 default gateway using the host operating system."""
    system = (system or platform.system()).lower()
    try:
        if system == "darwin":
            result = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                    text=True, timeout=3, check=False)
            match = re.search(r"^\s*gateway:\s*(\S+)", result.stdout, re.MULTILINE)
        elif system == "windows":
            result = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True,
                                    text=True, timeout=3, check=False)
            match = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+(?:\.\d+){3})",
                              result.stdout, re.MULTILINE)
        else:
            result = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                                    text=True, timeout=3, check=False)
            match = re.search(r"\bvia\s+(\d+(?:\.\d+){3})", result.stdout)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def ping_check(stage: str, host: str, count: int = 4, timeout: float = 6.0,
               system: Optional[str] = None) -> PathCheck:
    system = (system or platform.system()).lower()
    command = (["ping", "-n", str(count), "-w", "1000", host] if system == "windows"
               else ["ping", "-c", str(count), host])
    try:
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                check=False)
        elapsed = (time.monotonic() - started) * 1000
        output = result.stdout + "\n" + result.stderr
        loss_match = re.search(r"([\d.]+)%\s*(?:packet )?loss", output, re.IGNORECASE)
        if not loss_match:  # Windows: Lost = 0 (0% loss)
            loss_match = re.search(r"\(([\d.]+)%\s*loss\)", output, re.IGNORECASE)
        loss = float(loss_match.group(1)) if loss_match else (0.0 if result.returncode == 0 else 100.0)
        avg_match = re.search(r"(?:min/avg/max(?:/[\w]+)?\s*=\s*[\d.]+/|Average = )(\d+(?:\.\d+)?)",
                              output, re.IGNORECASE)
        latency = float(avg_match.group(1)) if avg_match else elapsed / max(count, 1)
        status = "ok" if result.returncode == 0 and loss == 0 else ("warning" if loss < 100 else "failed")
        return PathCheck(stage, status, host, latency, loss,
                         f"{count}회 ping · 손실 {loss:.0f}%")
    except subprocess.TimeoutExpired:
        return PathCheck(stage, "failed", host, None, 100.0, "ping 시간 초과")
    except OSError as exc:
        return PathCheck(stage, "unknown", host, detail=f"ping 실행 실패: {exc}")


def dns_check(hostname: str = "example.com", timeout: float = 4.0) -> PathCheck:
    """Resolve a hostname without changing process-wide socket defaults.

    ``getaddrinfo`` has no portable per-call timeout.  A daemon resolver thread
    lets the diagnostic return on time without leaking a non-daemon worker or
    altering sockets used by another GUI/background thread.
    """
    result_queue = queue.Queue(maxsize=1)
    started = time.monotonic()

    def resolve():
        try:
            result_queue.put(("ok", socket.getaddrinfo(
                hostname, 443, type=socket.SOCK_STREAM)))
        except OSError as exc:
            result_queue.put(("error", exc))

    threading.Thread(target=resolve, daemon=True,
                     name="wifi-analyzer-dns").start()
    try:
        status, payload = result_queue.get(timeout=timeout)
        if status == "error":
            raise payload
        addresses = payload
        latency = (time.monotonic() - started) * 1000
        address = addresses[0][4][0] if addresses else "응답 없음"
        return PathCheck("dns", "ok", hostname, latency, 0.0, f"응답 {address}")
    except queue.Empty:
        return PathCheck("dns", "failed", hostname, detail=f"DNS 조회 시간 초과 ({timeout:g}초)")
    except OSError as exc:
        return PathCheck("dns", "failed", hostname, detail=f"DNS 조회 실패: {exc}")


def https_check(url: str = "https://example.com/", timeout: float = 5.0) -> PathCheck:
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "WiFiAnalyzer/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            response.read(1)
        latency = (time.monotonic() - started) * 1000
        status = "ok" if 200 <= status_code < 400 else "warning"
        return PathCheck("https", status, url, latency, 0.0, f"HTTP {status_code}")
    except urllib.error.HTTPError as exc:
        # TLS and the HTTP server were reached; only the application endpoint
        # returned an error response.  This is not a path-connectivity failure.
        latency = (time.monotonic() - started) * 1000
        return PathCheck("https", "warning", url, latency, 0.0,
                         f"HTTP {exc.code} 응답 · HTTPS 경로 도달 성공")
    except Exception as exc:  # urllib raises several protocol-specific subclasses
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        certificate_error = (reason if isinstance(reason, ssl.SSLCertVerificationError)
                             else None)
        # Retry without verification only to separate network reachability from
        # certificate trust.  No response content is trusted or consumed.
        if certificate_error is not None:
            verify_reason = (getattr(certificate_error, "verify_message", None)
                             or str(certificate_error))
            verify_code = getattr(certificate_error, "verify_code", None)
            if verify_code is not None:
                verify_reason = f"code {verify_code}: {verify_reason}"
            try:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                request = urllib.request.Request(url, headers={"User-Agent": "WiFiAnalyzer/1.0"})
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    status_code = response.getcode()
                    response.read(1)
                latency = (time.monotonic() - started) * 1000
                return PathCheck(
                    "https", "warning", url, latency, 0.0, detail=(
                        f"인증서 검증 실패 ({verify_reason}) · "
                        f"검증 없는 재시도로 HTTP {status_code} 도달 확인"))
            except urllib.error.HTTPError as retry_error:
                latency = (time.monotonic() - started) * 1000
                return PathCheck(
                    "https", "warning", url, latency, 0.0, detail=(
                        f"인증서 검증 실패 ({verify_reason}) · 검증 없는 재시도로 "
                        f"HTTP {retry_error.code} 도달 확인"))
            except Exception as retry_error:
                return PathCheck(
                    "https", "failed", url, detail=(
                        f"인증서 검증 실패 ({verify_reason}); "
                        f"검증 없는 도달 확인도 실패: {retry_error}"))
        return PathCheck("https", "failed", url, detail=f"HTTPS 연결 실패: {exc}")


def _path_conclusion(checks: Sequence[PathCheck]) -> tuple[str, str]:
    states = {check.stage: check.status for check in checks}
    https_result = next((check for check in checks if check.stage == "https"), None)
    certificate_warning = bool(
        https_result and "인증서 검증 실패" in https_result.detail)
    ping_limited = any(states.get(stage) in {"failed", "unknown"}
                       for stage in ("gateway", "internet"))
    web_reachable = states.get("dns") == "ok" and states.get("https") in {"ok", "warning"}
    if ping_limited and web_reachable:
        certificate_note = (" HTTPS 인증서 경고도 함께 확인하세요."
                            if certificate_warning else "")
        http_note = (" HTTPS 서버는 도달했지만 HTTP 응답 상태도 확인하세요."
                     if states.get("https") == "warning" and not certificate_warning else "")
        return ("DNS와 HTTPS 경로는 도달합니다. 게이트웨이/외부 IP의 ICMP 응답이 "
                f"차단됐거나 제한됐을 수 있습니다.{certificate_note}{http_note}", "warning")
    if states.get("gateway") in {"failed", "unknown"}:
        return "게이트웨이부터 응답이 없어 Wi-Fi/LAN/AP 구간을 우선 확인하세요.", "critical"
    if states.get("internet") in {"failed", "unknown"}:
        return "게이트웨이는 정상이나 외부 IP가 실패해 라우터 상위 또는 ISP 구간이 의심됩니다.", "critical"
    if states.get("dns") == "failed":
        return "외부 IP는 도달하지만 DNS 조회가 실패했습니다. DNS 설정을 확인하세요.", "critical"
    if states.get("https") == "failed":
        return "IP와 DNS는 정상이나 HTTPS가 실패했습니다. 프록시·방화벽·캡티브 포털을 확인하세요.", "warning"
    if states.get("https") == "warning":
        if certificate_warning:
            return ("HTTPS 경로는 도달하지만 인증서 검증에 경고가 있습니다. 시스템 시간, "
                    "인증서 신뢰 체인과 캡티브 포털을 확인하세요."), "warning"
        return "HTTPS 서버는 도달했지만 HTTP 오류 응답을 반환했습니다. 서비스 상태를 확인하세요.", "warning"
    if any(state == "warning" for state in states.values()):
        return "경로는 연결되지만 손실 또는 지연 징후가 있습니다.", "warning"
    return "게이트웨이, 외부 IP, DNS, HTTPS 경로가 모두 응답합니다.", "ok"


def run_path_diagnostics(gateway: Optional[str] = None, external_ip: str = "1.1.1.1",
                         dns_name: str = "example.com",
                         https_url: str = "https://example.com/") -> PathDiagnostics:
    """Explicitly run LAN/WAN/DNS/HTTPS checks; importing this module does no I/O."""
    gateway = gateway or default_gateway()
    checks: list[PathCheck] = []
    if gateway:
        checks.append(ping_check("gateway", gateway))
    else:
        checks.append(PathCheck("gateway", "unknown", "-", detail="기본 게이트웨이를 찾지 못함"))
    checks.append(ping_check("internet", external_ip))
    checks.append(dns_check(dns_name))
    checks.append(https_check(https_url))
    conclusion, severity = _path_conclusion(checks)
    return PathDiagnostics(time.time(), gateway, tuple(checks), conclusion, severity)

