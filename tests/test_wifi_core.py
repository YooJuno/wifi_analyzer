import unittest
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from platforms import linux
from platforms.linux import _band_and_channel, _split_terse
from platforms import windows
from platforms import macos
from wifi_core import (AP, ConnectionInfo, ScanSnapshot, assign_keys, channel_to_frequency, deduplicate_aps, interference_level,
                       interference_margin_db, occupied_range, overlap_mhz,
                       relevant_interferers)


class WifiCoreTests(unittest.TestCase):
    def test_six_ghz_channel_frequency(self):
        self.assertEqual(channel_to_frequency(5, 6), 5975)
        self.assertEqual(occupied_range(5, 20, 6), (5965.0, 5985.0))

    def test_six_ghz_bonded_ranges_follow_primary_channel_blocks(self):
        self.assertEqual(occupied_range(5, 40, 6), (5945, 5985))
        self.assertEqual(occupied_range(21, 80, 6), (6025, 6105))
        self.assertEqual(occupied_range(37, 160, 6), (6105, 6265))
        ap = AP("6E", "aa:bb:cc:dd:ee:06", 21, 80, 6, -50, -90,
                frequency=6055)
        self.assertEqual(ap.rng, (6025, 6105))

    def test_channel_frequency_special_cases(self):
        self.assertEqual(channel_to_frequency(14, 2), 2484)
        self.assertEqual(channel_to_frequency(2, 6), 5935)

    def test_corewlan_enum_mapping_is_not_shifted(self):
        self.assertEqual(macos._WIDTHS, {0: None, 1: 20, 2: 40, 3: 80, 4: 160})
        self.assertEqual(macos._BANDS, {1: 2, 2: 5, 3: 6})

    def test_information_elements_include_load_and_secondary_channel(self):
        raw = bytes([11, 5, 3, 0, 128, 0, 0, 61, 2, 1, 1])
        self.assertEqual(macos._information_elements(raw), {
            "station_count": 3,
            "channel_utilization": 128,
            "secondary_offset": "above",
        })

    def test_system_profiler_parser_returns_current_and_nearby_bss(self):
        payload = json.dumps({"root": {
            "spairport_current_network_information": {
                "_name": "Office",
                "spairport_network_channel": "161 (5GHz, 80MHz)",
                "spairport_signal_noise": "-42 dBm / -91 dBm",
            },
            "spairport_airport_other_local_wireless_networks": [{
                "_name": "Guest",
                "spairport_network_channel": "6 (2GHz, 20MHz)",
                "spairport_signal_noise": "-65 dBm / -90 dBm",
            }],
        }})
        aps = macos._parse_system_profiler(payload)
        self.assertEqual(len(aps), 2)
        self.assertTrue(aps[0].cur)
        self.assertEqual((aps[0].ch, aps[0].width, aps[0].band), (161, 80, 5))

    def test_corewlan_connection_survives_redacted_ssid(self):
        channel = SimpleNamespace(
            channelNumber=lambda: 149, channelWidth=lambda: 3,
            channelBand=lambda: 2)
        interface = SimpleNamespace(
            scanForNetworksWithName_error_=lambda *_args: ([], None),
            ssid=lambda: None, bssid=lambda: None, wlanChannel=lambda: channel,
            rssiValue=lambda: -55, noiseMeasurement=lambda: -92,
            transmitRate=lambda: 600.0, activePHYMode=lambda: 6,
            security=lambda: 4, interfaceName=lambda: "en0")
        client = SimpleNamespace(interface=lambda: interface)
        corewlan = SimpleNamespace(
            CWWiFiClient=SimpleNamespace(sharedWiFiClient=lambda: client))
        with patch.dict(sys.modules, {"CoreWLAN": corewlan}):
            snapshot = macos._corewlan_snapshot()
        self.assertTrue(snapshot.connection.connected)
        self.assertIsNone(snapshot.connection.ssid)
        self.assertEqual(snapshot.connection.rssi, -55)
        self.assertEqual(snapshot.connection.width, 80)

    def test_overlap(self):
        self.assertEqual(overlap_mhz((2412, 2432), (2422, 2442)), 10)

    def test_assign_keys_disambiguates_hidden_networks(self):
        aps = assign_keys([AP(None, None, 1, 20, 2, -60, 0), AP(None, None, 1, 20, 2, -65, 0)])
        self.assertEqual(len({ap.key for ap in aps}), 2)

    def test_duplicate_bssid_is_merged_with_named_observation(self):
        hidden = AP(None, "AA-BB-CC-DD-EE-FF", 36, 80, 5, -40, -90)
        named = AP("Office", "aa:bb:cc:dd:ee:ff", 36, 80, 5, -45, -90)
        merged = deduplicate_aps([hidden, named])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].ssid, "Office")
        self.assertEqual(merged[0].key, "aa:bb:cc:dd:ee:ff")

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
            aps = windows._netsh_snapshot().aps
        self.assertEqual([ap.bssid for ap in aps], ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"])
        self.assertEqual([ap.rssi for ap in aps], [-60, -70])

    def test_windows_connection_parser(self):
        connection = windows._parse_interface("""    Name : Wi-Fi
    State : connected
    SSID : Office
    BSSID : aa:bb:cc:dd:ee:01
    Radio type : 802.11ax
    Authentication : WPA2-Personal
    Band : 5 GHz
    Channel : 36
    Signal : 80%
    Transmit rate (Mbps) : 1200
""")
        self.assertTrue(connection.connected)
        self.assertEqual(connection.band, 5)
        self.assertEqual(connection.rssi, -60)
        self.assertEqual(connection.tx_rate, 1200.0)

    def test_windows_korean_label_variants_and_six_ghz_band(self):
        networks = """SSID 1 : 사무실
    인증 : WPA3-Personal
    밴드 : 6 GHz
    BSSID 1 : aa:bb:cc:dd:ee:06
         신호 : 80%
         무선 유형 : 802.11ax
         채널 : 5
"""
        aps = windows._parse_networks(networks)
        self.assertEqual((aps[0].band, aps[0].phy), (6, "802.11ax"))
        connection = windows._parse_interface("""    이름 : Wi-Fi
    상태 : 연결됨
    SSID : 사무실
    BSSID : aa:bb:cc:dd:ee:06
    무선 유형 : 802.11ax
    인증 : WPA3-Personal
    밴드 : 6 GHz
    채널 : 5
    신호 : 80%
    전송 속도(Mbps) : 1200
""")
        self.assertTrue(connection.connected)
        self.assertEqual(connection.band, 6)
        self.assertEqual(connection.phy, "802.11ax")
        self.assertEqual(connection.tx_rate, 1200.0)

    def test_linux_parser_returns_connection_snapshot_fields(self):
        output = (r"yes:Cafe\:Guest:aa\:bb\:cc\:dd\:ee\:01:36:5180:80:WPA2:866 Mbit/s"
                  "\n")
        aps, connection = linux._parse(output)
        self.assertEqual(aps[0].ssid, "Cafe:Guest")
        self.assertTrue(connection.connected)
        self.assertIsNone(connection.tx_rate)

    def test_snapshot_model_keeps_legacy_aps(self):
        ap = AP("Office", "aa:bb:cc:dd:ee:01", 36, 80, 5, -50, -90)
        snapshot = ScanSnapshot([ap], ConnectionInfo(connected=True), "test")
        self.assertIs(snapshot.aps[0], ap)

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
