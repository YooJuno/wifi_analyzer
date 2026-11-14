import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostics import analyze_connection, recommend_channels
from measurement_log import MeasurementRecorder, scan_record
from wifi_core import AP, ConnectionInfo, ScanSnapshot


def sample():
    current = AP('실험실,"5G"', '02:00:00:00:00:01', 149, 80, 5, -55, -90, cur=True)
    neighbor = AP('Neighbor', '02:00:00:00:00:02', 149, 80, 5, -60, -90)
    connection = ConnectionInfo(connected=True, ssid=current.ssid, bssid=current.bssid,
                                channel=149, width=80, band=5, rssi=-55, noise=-90,
                                tx_rate=433, interface='demo0')
    snapshot = ScanSnapshot([current, neighbor], connection, 'Synthetic', timestamp=1700000000)
    findings = analyze_connection(current, snapshot.aps)
    scores = recommend_channels(snapshot.aps, 5, 80, exclude=current)
    return snapshot, current, findings, scores, 0.125


def events(directory):
    return [json.loads(line) for path in sorted(directory.glob('*.jsonl'))
            for line in path.read_text(encoding='utf-8').splitlines()]


def rows(directory):
    result = []
    for path in sorted(directory.glob('*.csv')):
        with path.open(encoding='utf-8-sig', newline='') as handle:
            result.extend(csv.DictReader(handle))
    return result


class MeasurementLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def recorder(self, **kwargs):
        recorder = MeasurementRecorder(self.root, **kwargs)
        self.addCleanup(lambda: (recorder.stop(), recorder._worker.join(timeout=3)))
        return recorder

    def test_scan_keeps_summary_raw_observations_and_overlap_evidence(self):
        recorder = self.recorder()
        self.assertTrue(recorder.record_scan(*sample()))
        recorder.record_event('path_diagnostics', {'latency_ms': 2.5})
        recorder.close(timeout=3)
        records = events(recorder.directory)
        self.assertEqual(records[0]['type'], 'session_start')
        self.assertEqual(records[-1]['type'], 'session_end')
        self.assertEqual(records[-1]['samples_written'], 1)
        record = next(item for item in records if item['type'] == 'scan')
        self.assertEqual(record['summary']['snr_db'], 35)
        self.assertEqual(record['summary']['overlapping_bss'], 1)
        self.assertAlmostEqual(record['summary']['rf_pressure_dbm'], -60)
        self.assertEqual(record['overlaps'][0]['kind'], 'same_primary')
        self.assertEqual(len(record['observations']), 2)
        self.assertTrue(record['findings'])
        self.assertTrue(record['recommendations'])
        row = rows(recorder.directory)[0]
        self.assertEqual(row['ssid'], '실험실,"5G"')
        self.assertEqual(row['rssi_dbm'], '-55')
        self.assertEqual(row['scan_duration_ms'], '125.0')
        self.assertRegex(row['timestamp'], r'[+-]\d{2}:\d{2}$')

    def test_unavailable_measurements_remain_missing_and_zero_snr_is_valid(self):
        args = list(sample())
        args[1].noise = None
        args[1].width_known = False
        summary = scan_record(*args)['summary']
        self.assertIsNone(summary['snr_db'])
        self.assertIsNone(summary['width_mhz'])
        args[1].noise = args[1].rssi
        self.assertEqual(scan_record(*args)['summary']['snr_db'], 0)

    def test_rotation_retains_every_sample_and_new_sessions_keep_old_files(self):
        recorder = self.recorder(max_segment_bytes=4096)
        for _ in range(12):
            self.assertTrue(recorder.record_scan(*sample()))
        recorder.close(timeout=3)
        files = list(recorder.directory.glob('*.jsonl'))
        self.assertGreater(len(files), 1)
        self.assertTrue(all(path.stat().st_size <= 4096 for path in files))
        self.assertEqual(sum(item['type'] == 'scan' for item in events(recorder.directory)), 12)
        self.assertEqual(len(rows(recorder.directory)), 12)
        original = {path: path.read_bytes() for path in files}
        restarted = self.recorder()
        restarted.record_scan(*sample())
        restarted.close(timeout=3)
        self.assertNotEqual(restarted.directory, recorder.directory)
        self.assertEqual(original, {path: path.read_bytes() for path in files})

    def test_normal_stop_drains_every_accepted_event_and_refuses_later_records(self):
        recorder = self.recorder(queue_size=128)
        for index in range(80):
            self.assertTrue(recorder.record_event('marker', {'index': index}))
        recorder.close(timeout=3)
        self.assertFalse(recorder.record_event('after_close'))
        saved = [item['data']['index'] for item in events(recorder.directory) if item['type'] == 'marker']
        self.assertEqual(saved, list(range(80)))

    def test_writer_copies_scanner_state_before_it_changes(self):
        release = threading.Event()
        started = threading.Event()
        original = MeasurementRecorder._open_segment

        def delayed_open(recorder):
            started.set()
            release.wait(timeout=3)
            original(recorder)

        with patch.object(MeasurementRecorder, '_open_segment', delayed_open):
            recorder = self.recorder()
            try:
                self.assertTrue(started.wait(timeout=2))
                args = sample()
                recorder.record_scan(*args)
                mutable = {'values': [1]}
                recorder.record_event('marker', mutable)
                args[1].rssi = -99
                args[1].ssid = 'Changed'
                mutable['values'].append(2)
            finally:
                release.set()
            recorder.close(timeout=3)
        saved = events(recorder.directory)
        self.assertEqual(next(item for item in saved if item['type'] == 'scan')['observations'][0]['rssi'], -55)
        self.assertEqual(next(item for item in saved if item['type'] == 'marker')['data']['values'], [1])

    def test_disk_failure_stops_recording_and_notifies_owner(self):
        notifications = []
        with patch.object(MeasurementRecorder, '_write', side_effect=OSError('disk full')):
            recorder = self.recorder(on_finished=notifications.append)
            with self.assertRaisesRegex(OSError, 'disk full'):
                recorder.close(timeout=3)
        self.assertTrue(recorder.finished.is_set())
        self.assertFalse(recorder.recording)
        self.assertFalse(recorder.record_event('lost'))
        self.assertEqual(notifications, [recorder])
        self.assertIsNone(recorder._json_file)
        self.assertIsNone(recorder._csv_file)

    def test_full_queue_reports_gap_and_still_drains_previously_accepted_events(self):
        release = threading.Event()
        started = threading.Event()
        original = MeasurementRecorder._open_segment

        def delayed_open(recorder):
            started.set()
            release.wait(timeout=3)
            original(recorder)

        with patch.object(MeasurementRecorder, '_open_segment', delayed_open):
            recorder = self.recorder(queue_size=1)
            try:
                self.assertTrue(started.wait(timeout=2))
                self.assertTrue(recorder.record_event('accepted'))
                self.assertFalse(recorder.record_event('rejected'))
                self.assertFalse(recorder.recording)
            finally:
                release.set()
            with self.assertRaisesRegex(OSError, '대기열'):
                recorder.close(timeout=3)
        saved = events(recorder.directory)
        self.assertTrue(any(item['type'] == 'accepted' for item in saved))
        self.assertFalse(any(item['type'] == 'rejected' for item in saved))
        self.assertEqual(saved[-1]['rejected_records'], 1)
        self.assertTrue(saved[-1]['error'])

    def test_spreadsheet_formula_names_are_literal_and_raw_names_are_preserved(self):
        recorder = self.recorder()
        args = sample()
        args[1].ssid = '=SUM(1,2)'
        recorder.record_scan(*args)
        recorder.close(timeout=3)
        self.assertEqual(rows(recorder.directory)[0]['ssid'], "'=SUM(1,2)")
        raw = next(item for item in events(recorder.directory) if item['type'] == 'scan')
        self.assertEqual(raw['summary']['ssid'], '=SUM(1,2)')


if __name__ == '__main__':
    unittest.main()
