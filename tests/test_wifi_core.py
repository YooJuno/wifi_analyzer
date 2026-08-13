import unittest
from types import SimpleNamespace
from unittest.mock import patch

from platforms import linux
from platforms.linux import _band_and_channel, _split_terse
from platforms import windows
from wifi_core import (AP, assign_keys, channel_to_frequency, interference_level,
                       interference_margin_db, occupied_range, overlap_mhz,
                       relevant_interferers)


class WifiCoreTests(unittest.TestCase):
    def test_six_ghz_channel_frequency(self):
        self.assertEqual(channel_to_frequency(5, 6), 5975)
        self.assertEqual(occupied_range(5, 20, 6), (5965.0, 5985.0))

    def test_overlap(self):
        self.assertEqual(overlap_mhz((2412, 2432), (2422, 2442)), 10)

    def test_assign_keys_disambiguates_hidden_networks(self):
        aps = assign_keys([AP(None, None, 1, 20, 2, -60, 0), AP(None, None, 1, 20, 2, -65, 0)])
        self.assertEqual(len({ap.key for ap in aps}), 2)

    def test_unknown_noise_has_no_snr(self):
        self.assertEqual(AP("network", None, 1, 20, 2, -60, None).snr, 0)

    def test_scanned_frequency_positions_24ghz_ap(self):
        ap = AP("network", None, 6, 20, 2, -60, None, frequency=2437)
        self.assertEqual(ap.center_frequency, 2437)
        self.assertEqual(ap.rng, (2427, 2447))

    def test_diagnosis_excludes_weak_overlapping_networks(self):
        current = AP("current", "00:00:00:00:00:01", 36, 20, 5, -60, None, cur=True)
        strong = AP("strong", "00:00:00:00:00:02", 36, 20, 5, -70, None)
        weak = AP("weak", "00:00:00:00:00:03", 36, 20, 5, -90, None)
        interferers = relevant_interferers(current, [current, strong, weak])
        self.assertEqual([ap.ssid for _, ap in interferers], ["strong"])
        self.assertEqual(interference_level(interference_margin_db(current, interferers)), "주의")

    def test_nmcli_escaped_delimiter(self):
        self.assertEqual(
            _split_terse(r"yes:Cafe\:Guest:12\:34\:56\:78\:9a\:bc:36:5180:72:WPA2"),
            ["yes", "Cafe:Guest", "12:34:56:78:9a:bc", "36", "5180", "72", "WPA2"],
        )

    def test_nmcli_frequency_with_unit_is_supported(self):
        self.assertEqual(_band_and_channel(2437, 6), (2, 6))

    def test_windows_parser_keeps_each_bssid(self):
        output = """SSID 1 : Office
    Authentication : WPA2-Personal
    BSSID 1 : aa:bb:cc:dd:ee:01
         Signal : 80%
         Channel : 36
    BSSID 2 : aa:bb:cc:dd:ee:02
         Signal : 60%
         Channel : 1
"""
        with patch.object(windows.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=output, stderr="")):
            aps = windows.scan()
        self.assertEqual([ap.bssid for ap in aps], ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"])
        self.assertEqual([ap.rssi for ap in aps], [-60, -70])

    def test_linux_uses_cached_results_between_rescans(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(linux.shutil, "which", return_value="/usr/bin/nmcli"), \
             patch.object(linux.subprocess, "run", return_value=result) as run, \
             patch.object(linux.time, "monotonic", side_effect=[100.0, 100.0, 100.5]):
            linux._last_rescan = 0.0
            linux.scan()
            linux.scan()
        self.assertEqual(run.call_args_list[0].args[0][-1], "yes")
        self.assertEqual(run.call_args_list[1].args[0][-1], "no")


if __name__ == "__main__":
    unittest.main()
