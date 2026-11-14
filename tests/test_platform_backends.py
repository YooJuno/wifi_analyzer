"""Offline fixtures: no host network calls, DLL loading or Wi-Fi permissions."""

import ctypes as c
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from diagnostics import classify_relation, diagnose_connection, evaluate_channels, ping_check, potential_rf_pressure_dbm
from measurement_log import scan_record
from platforms import commands, linux, windows
from platforms import windows_native as native
from platforms.wifi_ie import parse_information_elements
from wifi_core import AP, ConnectionInfo, ScanSnapshot, frequency_to_channel


def put_ssid(target, value):
    data = value.encode('utf-8')
    target.length = len(data)
    target.data[:len(data)] = data


def bss_blob(*, frequency=5180, ie=b'\x3d\x02\x24\x00\xc0\x03\x01\x2a\x00', phy=8):
    entry = native.BssEntry()
    put_ssid(entry.ssid, '사무실')
    entry.bssid[:] = [2, 0, 0, 0, 0, 1]
    entry.phy, entry.rssi, entry.frequency_khz = phy, -43, frequency * 1000
    entry.capabilities = 16
    entry.ie_offset, entry.ie_size = c.sizeof(entry), len(ie)
    size = 8 + c.sizeof(entry) + len(ie)
    return struct.pack('<II', size, 1) + bytes(entry) + ie


class NativeWindowsTests(unittest.TestCase):
    def test_sdk_layouts_have_windows_sizes_on_every_test_host(self):
        self.assertEqual(c.sizeof(native.BssEntry), 360)
        self.assertEqual(c.sizeof(native.ConnectionAttributes), 604)
        self.assertEqual(c.sizeof(native.InterfaceInfo), 532)
        self.assertEqual(native.BssEntry.rssi.offset, 56)
        self.assertEqual(native.BssEntry.ie_offset.offset, 352)

    def test_native_bss_reads_driver_dbm_and_advertised_width(self):
        ap = native.parse_bss_list(bss_blob())[0]
        self.assertEqual((ap.ssid, ap.bssid), ('사무실', '02:00:00:00:00:01'))
        self.assertEqual((ap.band, ap.ch, ap.rssi, ap.width), (5, 36, -43, 80))
        self.assertTrue(ap.width_known)
        self.assertFalse(ap.rssi_estimated)
        self.assertIsNone(ap.noise)

    def test_native_six_ghz_never_guesses_band_from_small_channel_number(self):
        ap = native.parse_bss_list(bss_blob(frequency=5975, phy=10))[0]
        self.assertEqual((ap.band, ap.ch), (6, 5))
        self.assertFalse(ap.width_known)
        self.assertEqual(frequency_to_channel(5935), (6, 2))

    def test_native_bss_rejects_truncated_and_overflowed_buffers(self):
        for data in (b'', struct.pack('<II', 8, 100), bss_blob()[:-1]):
            with self.subTest(data=len(data)), self.assertRaises(ValueError):
                native.parse_bss_list(data)
        blob = bytearray(bss_blob())
        struct.pack_into('<I', blob, 8 + native.BssEntry.ie_offset.offset, 0xffffff00)
        ap = native.parse_bss_list(blob)[0]
        self.assertFalse(ap.width_known)

    def test_connection_uses_measured_bss_instead_of_quality_conversion(self):
        aps = native.parse_bss_list(bss_blob())
        attributes = native.ConnectionAttributes()
        attributes.state = 1
        association = attributes.association
        put_ssid(association.ssid, '사무실')
        association.bssid[:] = [2, 0, 0, 0, 0, 1]
        association.phy, association.quality, association.tx_rate = 8, 10, 866700
        connection = native.parse_connection(bytes(attributes), 'Wi-Fi', aps)
        self.assertEqual((connection.rssi, connection.width, connection.tx_rate), (-43, 80, 866.7))
        self.assertFalse(connection.rssi_estimated)
        self.assertTrue(aps[0].cur)
        fallback = native.parse_connection(bytes(attributes), 'Wi-Fi', [])
        self.assertEqual(fallback.rssi, -95)
        self.assertTrue(fallback.rssi_estimated)
        self.assertIsNone(fallback.band)

    def test_native_buffer_is_freed_even_if_parsing_fails(self):
        client = native.WlanClient.__new__(native.WlanClient)
        client.handle = c.c_void_p(1)
        buffer = c.create_string_buffer(struct.pack('<II', 8, 100))

        def get_list(*args):
            args[-1]._obj.value = c.addressof(buffer)
            return 0

        client.api = SimpleNamespace(WlanGetNetworkBssList=get_list, WlanFreeMemory=Mock())
        with self.assertRaises(ValueError):
            client.bss_list(native.GUID())
        client.api.WlanFreeMemory.assert_called_once()

    def test_scan_waits_for_matching_interface_notification_and_unregisters(self):
        client = native.WlanClient.__new__(native.WlanClient)
        client.handle, client._callbacks = c.c_void_p(1), []
        guid = native.GUID(*range(16))
        saved = {}

        def register(handle, source, _duplicates, callback, *_rest):
            saved['callback'] = callback
            return 0

        def scan(*_args):
            event = native.Notification()
            event.source, event.code, event.guid = 8, 7, guid
            callback_type = getattr(c, 'WINFUNCTYPE', c.CFUNCTYPE)(None, c.POINTER(native.Notification), c.c_void_p)
            c.cast(saved['callback'], callback_type)(c.byref(event), None)
            return 0

        client.api = SimpleNamespace(WlanRegisterNotification=Mock(side_effect=register), WlanScan=scan)
        self.assertEqual(client.request_scan(guid), '')
        self.assertEqual([call.args[1] for call in client.api.WlanRegisterNotification.call_args_list], [8, 0])

    def test_native_failure_uses_netsh_and_permission_failure_has_actionable_message(self):
        fallback = ScanSnapshot([], source='fallback')
        with patch.object(native, 'snapshot', side_effect=OSError(5, 'denied')), \
                patch.object(windows, '_netsh_snapshot', return_value=fallback):
            self.assertIs(windows.snapshot(), fallback)
            self.assertTrue(any('Native' in warning for warning in fallback.warnings))
        with patch.object(native, 'snapshot', side_effect=OSError(5, 'denied')), \
                patch.object(windows, '_netsh_snapshot', side_effect=RuntimeError('blocked')):
            with self.assertRaisesRegex(RuntimeError, '위치'):
                windows.snapshot()


class InformationElementTests(unittest.TestCase):
    def test_six_ghz_he_operation_handles_optional_fields_and_truncation(self):
        for flags, extra in ((0x20000, b''), (0x2c000, b'\0\0\0\0')):
            body = bytes([36]) + flags.to_bytes(4, 'little') + b'\0\0' + extra + bytes([5, 2, 7, 0, 6])
            fields = parse_information_elements(bytes([255, len(body)]) + body, 6)
            self.assertEqual(fields['width'], 80)
            self.assertNotIn('width', parse_information_elements(bytes([255, len(body)]) + body[:-1], 6))
            self.assertNotIn('width', parse_information_elements(bytes([255, len(body)]) + body + bytes([255, 1, 106]), 6))

    def test_ht_width_and_bss_load(self):
        fields = parse_information_elements(bytes([61, 2, 6, 5, 11, 5, 3, 0, 128, 0, 0]), 2)
        self.assertEqual(fields, {'width': 40, 'secondary_offset': 'above',
                                  'station_count': 3, 'channel_utilization': 128})

    def test_vht_160_and_noncontiguous_80_plus_80(self):
        self.assertEqual(parse_information_elements(bytes([192, 3, 1, 42, 50]), 5)['width'], 160)
        self.assertNotIn('width', parse_information_elements(bytes([61, 2, 36, 0, 192, 3, 1, 42, 155]), 5))
        self.assertNotIn('width', parse_information_elements(bytes([61, 2, 36]), 5))


class LinuxIwTests(unittest.TestCase):
    def baseline(self):
        return linux._parse(r'yes:Lab:AA\:BB\:CC\:DD\:EE\:01:36:5180:80:WPA2:866 Mbit/s:wlan0')

    def test_iw_enriches_actual_signal_width_rate_and_current_channel_noise(self):
        aps, connection = self.baseline()
        outputs = {
            ('link',): 'Connected to aa:bb:cc:dd:ee:01 (on wlan0)\n SSID: Lab\n freq: 5180\n signal: -48 dBm\n tx bitrate: 866.7 MBit/s',
            ('info',): 'channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz',
            ('survey', 'dump'): 'Survey data from wlan0\n frequency: 2412 MHz\n noise: -70 dBm\nSurvey data from wlan0\n frequency: 5180 MHz [in use]\n noise: -96 dBm',
            ('scan', 'dump'): '',
        }
        with patch.object(linux.shutil, 'which', return_value='/usr/sbin/iw'), \
                patch.object(linux, '_iw_output', side_effect=lambda _interface, *args: outputs[args]):
            self.assertTrue(linux._enrich_iw(aps, connection))
        self.assertEqual((connection.rssi, connection.width, connection.noise, connection.tx_rate), (-48, 80, -96, 866.7))
        self.assertEqual(aps[0].snr, 48)
        self.assertFalse(aps[0].rssi_estimated)
        self.assertEqual(aps[0].width, 80)

    def test_iw_does_not_apply_other_channel_or_invalid_noise(self):
        for survey in ('frequency: 2412 MHz [in use]\n noise: -70 dBm',
                       'frequency: 5180 MHz [in use]\n noise: 0 dBm'):
            aps, connection = self.baseline()
            def output(_interface, *args):
                if args == ('link',):
                    return 'Connected to aa:bb:cc:dd:ee:01\n freq: 5180\n signal: -50 dBm'
                return survey if args == ('survey', 'dump') else ''
            with patch.object(linux.shutil, 'which', return_value='iw'), patch.object(linux, '_iw_output', side_effect=output):
                linux._enrich_iw(aps, connection)
            self.assertIsNone(connection.noise)

    def test_iw_disconnection_clears_stale_networkmanager_connection(self):
        aps, connection = self.baseline()
        with patch.object(linux.shutil, 'which', return_value='iw'), \
                patch.object(linux, '_iw_output', side_effect=lambda _interface, *args: 'Not connected.' if args == ('link',) else ''):
            linux._enrich_iw(aps, connection)
        self.assertFalse(connection.connected)
        self.assertFalse(aps[0].cur)
        self.assertIsNone(connection.rssi)

    def test_missing_iw_preserves_marked_estimates_without_failing_scan(self):
        aps, connection = self.baseline()
        with patch.object(linux.shutil, 'which', return_value=None):
            self.assertFalse(linux._enrich_iw(aps, connection))
        self.assertTrue(connection.rssi_estimated)
        self.assertTrue(aps[0].rssi_estimated)
        self.assertIsNone(connection.width)

    def test_unassociated_interface_still_enriches_nearby_observations(self):
        aps, connection = linux._parse(r'no:Lab:aa\:bb\:cc\:dd\:ee\:01:36:5180:80:WPA2:866 Mbit/s:wlan0')
        self.assertFalse(connection.connected)
        self.assertEqual(connection.interface, 'wlan0')
        scan_dump = '''BSS aa:bb:cc:dd:ee:01(on wlan0)
 freq: 5180
 signal: -48.00 dBm
 SSID: Lab
 VHT operation:
   * channel width: 1 (80 MHz)
'''
        def output(_interface, *args):
            return 'Not connected.' if args == ('link',) else scan_dump if args == ('scan', 'dump') else ''
        with patch.object(linux.shutil, 'which', return_value='iw'), patch.object(linux, '_iw_output', side_effect=output):
            linux._enrich_iw(aps, connection)
        self.assertEqual((aps[0].rssi, aps[0].width), (-48, 80))
        self.assertTrue(aps[0].width_known)
        self.assertFalse(connection.connected)

    def test_iw_scan_uses_operation_width_and_six_ghz_frequency(self):
        output = '''BSS aa:bb:cc:dd:ee:01(on wlan0)
 freq: 5975
 signal: -48.00 dBm
 SSID: Six
 HE operation:
   * channel width: 2 (80 MHz)
BSS aa:bb:cc:dd:ee:02(on wlan0)
 freq: 5180
 signal: -72.00 dBm
 SSID: Unknown
 HT capabilities:
   Capabilities: HT20/HT40
'''
        aps = linux._parse_iw_scan(output)
        self.assertEqual((aps[0].band, aps[0].ch, aps[0].width), (6, 5, 80))
        self.assertTrue(aps[0].width_known)
        self.assertFalse(aps[1].width_known)

    def test_iw_hex_encoded_korean_ssid_and_supported_width_are_not_misread(self):
        self.assertEqual(linux._iw_ssid(r'\xec\x82\xac\xeb\xac\xb4\xec\x8b\xa4'), '사무실')
        output = '''BSS aa:bb:cc:dd:ee:01(on wlan0)
 freq: 5180
 signal: -48.00 dBm
 SSID: Lab
 VHT capabilities:
   Supported Channel Width: 160 MHz, 80+80 MHz
 VHT operation:
   * channel width: 1 (80 MHz)
   * center freq segment 1: 42
   * center freq segment 2: 0
'''
        self.assertEqual(linux._parse_iw_scan(output)[0].width, 80)


class FallbackAndDiagnosticTests(unittest.TestCase):
    def test_windows_korean_oem_and_utf16_output(self):
        text = '이름 : Wi-Fi\n상태 : 연결됨\nSSID : 사무실\n채널 : 36\n신호 : 80%'
        decoded = commands.decode_output(text.encode('cp949'), windows=True, oem_encoding='cp949')
        connection = windows._parse_interface(decoded)
        self.assertEqual(connection.ssid, '사무실')
        self.assertTrue(connection.rssi_estimated)
        self.assertEqual(commands.decode_output(text.encode('utf-16'), windows=True), text)

    def test_netsh_keeps_connected_adapter_when_second_adapter_is_disconnected(self):
        connection = windows._parse_interface('Name : Wi-Fi\nState : connected\nSSID : Office\nSignal : 80%\nTransmit rate (Mbps) : 866,7\nName : Wi-Fi 2\nState : disconnected')
        self.assertTrue(connection.connected)
        self.assertEqual((connection.ssid, connection.tx_rate), ('Office', 866.7))

    def test_missing_netsh_signal_does_not_become_a_fake_weak_measurement(self):
        self.assertEqual(windows._parse_networks('SSID 1 : Office\n BSSID 1 : aa:bb:cc:dd:ee:01\n Channel : 36'), [])

    def test_korean_ping_reads_loss_and_latency_and_never_uses_process_time(self):
        result = SimpleNamespace(returncode=0, stdout='패킷: 보냄 = 4, 받음 = 3, 손실 = 1 (25% 손실)\n최소 = 1ms, 최대 = 3ms, 평균 = 2ms', stderr='')
        with patch('platforms.commands.subprocess.run', return_value=result):
            check = ping_check('gateway', '192.0.2.1', system='windows')
        self.assertEqual((check.loss_percent, check.latency_ms, check.status), (25, 2, 'warning'))
        result.stdout = 'reply without a round-trip summary'
        with patch('platforms.commands.subprocess.run', return_value=result):
            self.assertIsNone(ping_check('gateway', '192.0.2.1', system='windows').latency_ms)

    def test_unknown_channel_width_prevents_false_overlap_and_channel_ranking(self):
        current = AP('current', 'a', 36, 80, 5, -55, None, cur=True)
        unknown = AP('unknown', 'b', 40, 20, 5, -40, None, width_known=False, rssi_estimated=True)
        self.assertEqual(classify_relation(current, unknown).kind, 'none')
        self.assertIsNone(potential_rf_pressure_dbm(current, [current, unknown]))
        self.assertEqual(evaluate_channels([current, unknown], 5, 80, exclude=current), [])
        codes = {item.code for item in diagnose_connection(current, [current, unknown])}
        self.assertIn('channel_width_unavailable', codes)
        self.assertNotIn('adjacent_overlap', codes)
        unknown.ch = 36
        self.assertEqual(classify_relation(current, unknown).kind, 'same_primary')

    def test_partial_connected_data_is_not_reported_as_disconnected(self):
        self.assertEqual(diagnose_connection(None, [], connected=True)[0].code, 'connection_data_unavailable')
        self.assertEqual(diagnose_connection(None, [], connected=False)[0].code, 'not_connected')

    def test_log_records_provenance_and_incomplete_rf_comparison(self):
        current = AP('current', 'a', 36, 20, 5, -60, None, cur=True, width_known=False, rssi_estimated=True)
        summary = scan_record(ScanSnapshot([current], ConnectionInfo(connected=True)), current, [], [], .1)['summary']
        self.assertTrue(summary['rssi_estimated'])
        self.assertFalse(summary['rf_comparison_complete'])
        self.assertIsNone(summary['rf_pressure_dbm'])
        self.assertIsNone(summary['width_mhz'])

    def test_command_locale_is_local_to_subprocess(self):
        result = SimpleNamespace(returncode=0, stdout=b'ok', stderr=b'')
        original = os.environ.get('LC_ALL')
        with patch('platforms.commands.subprocess.run', return_value=result) as run:
            self.assertEqual(commands.run_command(['nmcli'], timeout=1, stable_locale=True).stdout, 'ok')
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertEqual(os.environ.get('LC_ALL'), original)


if __name__ == '__main__':
    unittest.main()
