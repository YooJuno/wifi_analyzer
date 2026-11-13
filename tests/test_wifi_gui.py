import unittest

from wifi_core import AP, ConnectionInfo
from wifi_gui import App, compact_text


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


if __name__ == "__main__":
    unittest.main()
