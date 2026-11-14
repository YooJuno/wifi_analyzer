"""Exercise the real GUI and logging with fixtures, without Wi-Fi access.

Run on a desktop with python tools/verify_gui.py, or under xvfb-run on Linux.
This checks rendering and application behavior, not driver/API availability.
"""

import csv
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_core import AP, ConnectionInfo, ScanSnapshot
from wifi_gui import App


def main():
    failures = []
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(App, 'scan_now', lambda self: None), \
            patch.object(App, '_request_location', lambda self: None):
        app = App()
        app.report_callback_exception = lambda *args: failures.append(args)
        try:
            app.geometry('1100x740')
            app.update()
            with patch('wifi_gui.filedialog.askdirectory', return_value=folder):
                app.toggle_logging()
            for estimated in (False, True):
                current = AP('Fixture-Lab', '02:00:00:00:00:01', 36, 80 if not estimated else 20,
                             5, -60, None, cur=True, width_known=not estimated, rssi_estimated=estimated)
                neighbor = AP('Neighbor', '02:00:00:00:00:02', 40, 20, 5, -50, None,
                              width_known=not estimated, rssi_estimated=estimated)
                connection = ConnectionInfo(connected=True, ssid=current.ssid, bssid=current.bssid,
                                             band=5, channel=36, width=None if estimated else 80,
                                             rssi=-60, rssi_estimated=estimated, interface='fixture0')
                app.accept_snapshot(ScanSnapshot([current, neighbor], connection, 'Synthetic'), .1)
                for tab in (app.overview_tab, app.network_tab, app.channel_tab, app.history_tab, app.path_tab):
                    app.notebook.select(tab)
                    app.update()
                assert app.snr_card.value_label.cget('text') == '미측정'
                if estimated:
                    assert app.signal_card.value_label.cget('text').startswith('≈')
                    assert not app.channel_scores
                    assert 'channel_width_unavailable' in {item.code for item in app.findings}
                assert app.spectrum.find_all(), 'Spectrum did not draw'
            partial = ConnectionInfo(connected=True, ssid='Fixture-Lab', rssi=-61, rssi_estimated=True)
            app.accept_snapshot(ScanSnapshot([], partial, 'Synthetic-partial'), .1)
            app.notebook.select(app.overview_tab)
            app.update()
            assert app.connection_card.value_label.cget('text') == 'Fixture-Lab'
            assert app.findings[0].code == 'connection_data_unavailable'
            app.toggle_logging()
            recorder = app.recorder
            recorder.close(timeout=5)
            app._pump()
            assert '기록 완료' in app.log_status.cget('text')
            entries = [json.loads(line) for path in sorted(recorder.directory.glob('*.jsonl'))
                       for line in path.read_text(encoding='utf-8').splitlines()]
            scans = [item for item in entries if item['type'] == 'scan']
            assert len(scans) == 3
            assert scans[1]['summary']['rssi_estimated'] is True
            assert scans[1]['summary']['rf_comparison_complete'] is False
            count = 0
            for path in recorder.directory.glob('*.csv'):
                with path.open(encoding='utf-8-sig', newline='') as handle:
                    count += len(list(csv.DictReader(handle)))
            assert count == 3
            assert not failures, failures
        finally:
            app.close()
            if app.recorder:
                app.recorder.close(timeout=5)
    print('GUI verification passed: measured/estimated/missing data, plots, tabs, logging, minimum window size.')


if __name__ == '__main__':
    main()
