import unittest
import collections
from types import SimpleNamespace
from unittest.mock import Mock

from diagnostics import diagnose_connection
from wifi_core import AP, ConnectionInfo
from wifi_gui import (App, HISTORY_SECONDS, MAX_HISTORY_NETWORKS, compact_text,
                      connection_identity, recent_samples)


class GuiLogicTests(unittest.TestCase):
    def test_synthetic_current_row_keeps_tree_ids_unique(self):
        app = App.__new__(App)
        app.connection = ConnectionInfo(
            connected=True, ssid="Office", channel=6, width=20, band=2,
            rssi=-55, noise=-90)
        app.aps = [AP("Office", None, 6, 20, 2, -60, -90)]

        current = App._merge_current_connection(app)

        self.assertIs(current, app.aps[0])
        self.assertTrue(current.cur)
        self.assertEqual(len({item.key for item in app.aps}), len(app.aps))

    def test_ambiguous_anonymous_rows_are_disambiguated(self):
        app = App.__new__(App)
        app.connection = ConnectionInfo(
            connected=True, ssid="Office", channel=6, width=20, band=2,
            rssi=-55, noise=-90)
        app.aps = [
            AP("Office", None, 6, 20, 2, -60, -90),
            AP("Office", None, 6, 20, 2, -70, -90),
        ]

        current = App._merge_current_connection(app)

        self.assertEqual(len(app.aps), 3)
        self.assertIs(current, app.aps[-1])
        self.assertEqual(len({item.key for item in app.aps}), len(app.aps))

    def test_compact_text_only_changes_summary_value(self):
        self.assertEqual(compact_text("short"), "short")
        self.assertEqual(compact_text("abcdefgh", 5), "abcd…")

    def test_old_signal_dip_does_not_keep_stable_connection_in_alarm(self):
        current = AP("Office", "01:02:03:04:05:06", 36, 80, 5, -55, -90)
        history = [(0, -80, -90)] + [(timestamp, -55, -90) for timestamp in range(100, 1000)]
        values = [sample[1] for sample in recent_samples(history, now=999)]
        self.assertTrue(all(value == -55 for value in values))
        self.assertNotIn("unstable_signal", {
            finding.code for finding in diagnose_connection(current, [current], values)})

    def test_history_retention_releases_disappeared_networks(self):
        app = App.__new__(App)
        app.current_ap = None
        app.selected_key = None
        app.history = {"old": collections.deque([(1, -70, None)]),
                       "current": collections.deque([(HISTORY_SECONDS + 1, -55, None)])}
        App._prune_history(app, HISTORY_SECONDS + 2)
        self.assertEqual(set(app.history), {"current"})

    def test_history_limit_preserves_current_and_selected_networks(self):
        app = App.__new__(App)
        app.current_ap = AP("current", "current", 36, 80, 5, -55, -90)
        app.selected_key = "selected"
        app.history = {str(index): collections.deque([(100, -70, None)])
                       for index in range(MAX_HISTORY_NETWORKS + 10)}
        app.history["current"] = collections.deque([(90, -55, -90)])
        app.history["selected"] = collections.deque([(90, -60, -90)])
        App._prune_history(app, 100)
        self.assertEqual(len(app.history), MAX_HISTORY_NETWORKS)
        self.assertTrue({"current", "selected"}.issubset(app.history))

    def test_summary_uses_actual_wide_channel_width(self):
        for width in (40, 160):
            with self.subTest(width=width):
                app = App.__new__(App)
                app.current_ap = AP("Office", "current", 36, width, 5, -55, -90, cur=True)
                app.aps = [app.current_ap]
                app._score_cache = {}
                App._calculate_channel_scores(app)
                self.assertTrue(app.channel_scores)
                self.assertEqual({score.width for score in app.channel_scores}, {width})
                self.assertTrue(all(score.pressure_dbm is None for score in app.channel_scores))

    def test_inflight_path_result_is_discarded_after_connection_change(self):
        app = App.__new__(App)
        app._connection_generation = 2
        app.recorder = None
        app.path_result = None
        app.update_path_result = Mock()
        self.assertFalse(App._accept_path_result(app, 1, "old connection"))
        self.assertIsNone(app.path_result)
        app.update_path_result.assert_not_called()
        self.assertTrue(App._accept_path_result(app, 2, "current connection"))
        self.assertEqual(app.path_result, "current connection")
        app.update_path_result.assert_called_once()

    def test_connection_identity_normalizes_bssid_and_tracks_disconnect(self):
        connection = ConnectionInfo(connected=True, bssid="AA-BB-CC-DD-EE-FF")
        normalized = connection_identity(connection)
        connection.bssid = "aa:bb:cc:dd:ee:ff"
        self.assertEqual(connection_identity(connection), normalized)
        connection.connected = False
        self.assertIsNone(connection_identity(connection))

    def test_scan_interval_is_bounded_even_with_typed_invalid_values(self):
        app = App.__new__(App)
        for value, expected in ((0, 5000), (999, 120000), (float("inf"), 10000),
                                (float("nan"), 10000), (10, 10000)):
            with self.subTest(value=value):
                app.interval = SimpleNamespace(get=lambda: value)
                self.assertEqual(App.interval_ms(app), expected)


if __name__ == "__main__":
    unittest.main()
