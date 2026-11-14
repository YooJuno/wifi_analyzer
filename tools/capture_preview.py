"""Render the real GUI with synthetic AP data, without Wi-Fi or location access."""

import argparse
import copy
import sys
import time
from pathlib import Path
from unittest.mock import patch

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_core import AP, ConnectionInfo, ScanSnapshot
from wifi_gui import App


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/previews'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    aps = [
        AP('Example-Lab', '02:00:00:00:00:01', 36, 80, 5, -62, -94,
           sec='WPA2', phy='802.11ac', cur=True),
        AP('Example-Guest', '02:00:00:00:00:02', 40, 40, 5, -56, -93,
           sec='WPA2', phy='802.11ac'),
        AP('Example-Office', '02:00:00:00:00:03', 149, 80, 5, -71, -94,
           sec='WPA3', phy='802.11ax'),
        AP('Example-2G', '02:00:00:00:00:04', 6, 20, 2, -65, -90, sec='WPA2'),
        AP('Example-IoT', '02:00:00:00:00:05', 1, 20, 2, -73, -90, sec='WPA2'),
    ]
    connection = ConnectionInfo(
        connected=True, ssid='Example-Lab', bssid=aps[0].bssid,
        channel=36, width=80, band=5, rssi=-62, noise=-94,
        tx_rate=433, phy='802.11ac', security='WPA2', interface='demo0')

    with patch.object(App, 'scan_now', lambda self: None), \
            patch.object(App, '_request_location', lambda self: None):
        app = App()
        try:
            app.geometry('1480x920+0+0')
            app.attributes('-topmost', True)
            app.lift()
            app.paused = True
            for sample in range(45):
                snapshot_aps = copy.deepcopy(aps)
                snapshot_aps[0].rssi = -62 + [0, 2, 1, -2, -4, -1, 1][sample % 7]
                snapshot_connection = copy.copy(connection)
                snapshot_connection.rssi = snapshot_aps[0].rssi
                app.accept_snapshot(ScanSnapshot(
                    snapshot_aps, snapshot_connection,
                    source='Synthetic AP data - no real scan',
                    timestamp=time.time() - (44 - sample) * 10), 0.1)
            for filename, tab in [
                ('overview', app.overview_tab), ('networks', app.network_tab),
                ('channels', app.channel_tab), ('history', app.history_tab),
                ('path', app.path_tab),
            ]:
                app.notebook.select(tab)
                app.update()
                app.update_idletasks()
                bounds = (app.winfo_rootx(), app.winfo_rooty(),
                          app.winfo_rootx() + app.winfo_width(),
                          app.winfo_rooty() + app.winfo_height())
                image = ImageGrab.grab(bbox=bounds)
                if image.size != (1480, 920):
                    raise RuntimeError(f'Unexpected preview size: {image.size}')
                if len(image.convert('RGB').getcolors(image.width * image.height)) < 100:
                    raise RuntimeError('GUI did not render enough detail')
                image.save(args.output / (filename + '.png'))
                print(f'Rendered synthetic preview: {filename}')
        finally:
            app.close()


if __name__ == '__main__':
    main()
