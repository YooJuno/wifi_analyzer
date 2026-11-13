import ssl
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from diagnostics import (PathCheck, _path_conclusion, assess_rssi, assess_snr,
                         candidate_range, classify_relation, default_channel_candidates,
                         diagnose_connection, dns_check, evaluate_channels, https_check, ping_check,
                         potential_rf_pressure_dbm)


def ap(ssid, channel, width, band, rssi, start, end, noise=-90, sec="WPA2"):
    return SimpleNamespace(ssid=ssid, ch=channel, width=width, band=band,
                           rssi=rssi, noise=noise, sec=sec, rng=(start, end))


class MetricTests(unittest.TestCase):
    def test_signal_thresholds(self):
        self.assertEqual(assess_rssi(-67).status, "ok")
        self.assertEqual(assess_rssi(-74).status, "warning")
        self.assertEqual(assess_rssi(-80).status, "critical")
        self.assertEqual(assess_snr(25).status, "ok")
        self.assertEqual(assess_snr(14).status, "critical")
        self.assertEqual(assess_snr(None).status, "unknown")

    def test_relation_separates_primary_block_and_partial(self):
        target = ap("main", 149, 80, 5, -50, 5735, 5815)
        same_primary = ap("same", 149, 20, 5, -60, 5735, 5755)
        same_block = ap("block", 161, 80, 5, -65, 5735, 5815)
        partial = ap("partial", 165, 20, 5, -60, 5815, 5835)
        self.assertEqual(classify_relation(target, same_primary).kind, "same_primary")
        self.assertEqual(classify_relation(target, same_block).kind, "same_bonded_block")
        # Touching only at the boundary has zero MHz overlap.
        self.assertEqual(classify_relation(target, partial).kind, "none")

    def test_pressure_is_overlap_weighted(self):
        target = ap("main", 36, 20, 5, -50, 5170, 5190)
        full = ap("full", 36, 20, 5, -60, 5170, 5190)
        half = ap("half", 40, 20, 5, -60, 5180, 5200)
        pressure = potential_rf_pressure_dbm(target, [target, full, half])
        self.assertAlmostEqual(pressure, -58.24, places=1)

    def test_channel_candidates_and_ranking(self):
        crowded = ap("crowded", 1, 20, 2, -40, 2402, 2422)
        scores = evaluate_channels([crowded], 2, 20)
        self.assertEqual(default_channel_candidates(2, 20), [1, 6, 11])
        self.assertIn(scores[0].channel, (6, 11))
        self.assertEqual(scores[-1].channel, 1)
        self.assertEqual(candidate_range(5, 161, 80), (5735.0, 5815.0))

    def test_six_ghz_bonded_ranges_and_safe_default_candidates(self):
        self.assertEqual(candidate_range(6, 5, 40), (5945.0, 5985.0))
        self.assertEqual(candidate_range(6, 5, 80), (5945.0, 6025.0))
        self.assertEqual(candidate_range(6, 21, 80), (6025.0, 6105.0))
        self.assertEqual(candidate_range(6, 29, 160), (5945.0, 6105.0))
        self.assertEqual(default_channel_candidates(6, 20)[:3], [5, 21, 37])
        self.assertEqual(default_channel_candidates(6, 80)[-1], 213)
        self.assertEqual(default_channel_candidates(6, 160),
                         [5, 37, 69, 101, 133, 165, 197])


class FindingTests(unittest.TestCase):
    def test_connection_diagnosis_produces_evidence_and_action(self):
        current = ap("office", 6, 40, 2, -78, 2427, 2467, noise=-88)
        rival = ap("rival", 8, 20, 2, -55, 2437, 2457)
        better = ap("office", 1, 20, 2, -60, 2402, 2422)
        findings = diagnose_connection(current, [current, rival, better],
                                       [-78, -62, -79, -63])
        codes = {finding.code for finding in findings}
        self.assertTrue({"weak_rssi", "low_snr", "unstable_signal", "adjacent_overlap",
                         "wide_24ghz", "sticky_client"}.issubset(codes))
        self.assertTrue(all(finding.evidence and finding.action for finding in findings))

    def test_healthy_connection_has_explicit_ok_finding(self):
        current = ap("office", 36, 20, 5, -55, 5170, 5190, noise=-90)
        findings = diagnose_connection(current, [current])
        self.assertEqual([finding.code for finding in findings], ["rf_ok"])


class PathTests(unittest.TestCase):
    def test_ping_parser_reads_unix_summary(self):
        output = ("4 packets transmitted, 4 packets received, 0.0% packet loss\n"
                  "round-trip min/avg/max/stddev = 1.000/2.500/4.000/0.5 ms\n")
        with patch("diagnostics.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=output, stderr="")), \
             patch("diagnostics.time.monotonic", side_effect=[1.0, 1.1]):
            result = ping_check("gateway", "192.168.1.1", system="darwin")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.loss_percent, 0.0)
        self.assertEqual(result.latency_ms, 2.5)

    def test_path_conclusion_isolates_dns(self):
        conclusion, severity = _path_conclusion([
            PathCheck("gateway", "ok", "gateway"),
            PathCheck("internet", "ok", "1.1.1.1"),
            PathCheck("dns", "failed", "example.com"),
            PathCheck("https", "failed", "https://example.com"),
        ])
        self.assertEqual(severity, "critical")
        self.assertIn("DNS", conclusion)

    def test_https_ca_failure_is_separated_from_reachability(self):
        verified_failure = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed"))
        response = MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 204
        response.read.return_value = b""
        with patch("diagnostics.urllib.request.urlopen",
                   side_effect=[verified_failure, response]), \
             patch("diagnostics.time.monotonic", side_effect=[10.0, 10.125]):
            result = https_check()
        self.assertEqual(result.status, "warning")
        self.assertIn("인증서 검증 실패", result.detail)
        self.assertIn("certificate verify failed", result.detail)
        self.assertEqual(result.latency_ms, 125.0)
        conclusion, severity = _path_conclusion([
            PathCheck("gateway", "ok", "gateway"),
            PathCheck("internet", "ok", "1.1.1.1"),
            PathCheck("dns", "ok", "example.com"),
            result,
        ])
        self.assertEqual(severity, "warning")
        self.assertIn("인증서", conclusion)

    def test_http_error_still_proves_https_reachability(self):
        error = urllib.error.HTTPError(
            "https://example.com/", 503, "Service Unavailable", {}, None)
        with patch("diagnostics.urllib.request.urlopen", side_effect=error), \
             patch("diagnostics.time.monotonic", side_effect=[3.0, 3.08]):
            result = https_check()
        self.assertEqual(result.status, "warning")
        self.assertAlmostEqual(result.latency_ms, 80.0)
        self.assertIn("HTTP 503", result.detail)
        self.assertIn("도달 성공", result.detail)
        conclusion, severity = _path_conclusion([
            PathCheck("gateway", "ok", "gateway"),
            PathCheck("internet", "ok", "1.1.1.1"),
            PathCheck("dns", "ok", "example.com"),
            result,
        ])
        self.assertEqual(severity, "warning")
        self.assertIn("HTTP 오류", conclusion)
        self.assertNotIn("인증서", conclusion)

    def test_dns_check_does_not_change_global_socket_timeout(self):
        addresses = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("diagnostics.socket.getaddrinfo", return_value=addresses), \
             patch("diagnostics.socket.setdefaulttimeout") as set_timeout:
            result = dns_check(timeout=0.2)
        self.assertEqual(result.status, "ok")
        set_timeout.assert_not_called()

    def test_dns_timeout_returns_while_daemon_resolver_is_blocked(self):
        release = threading.Event()
        with patch("diagnostics.socket.getaddrinfo",
                   side_effect=lambda *_args, **_kwargs: release.wait(1.0)):
            result = dns_check(timeout=0.01)
        release.set()
        self.assertEqual(result.status, "failed")
        self.assertIn("시간 초과", result.detail)

    def test_working_dns_and_https_make_ping_failure_icmp_warning(self):
        conclusion, severity = _path_conclusion([
            PathCheck("gateway", "unknown", "-"),
            PathCheck("internet", "failed", "1.1.1.1"),
            PathCheck("dns", "ok", "example.com"),
            PathCheck("https", "ok", "https://example.com"),
        ])
        self.assertEqual(severity, "warning")
        self.assertIn("ICMP", conclusion)
        self.assertNotIn("ISP", conclusion)


if __name__ == "__main__":
    unittest.main()
