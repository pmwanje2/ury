from pathlib import Path
import re
import unittest


class ShippedPosTests(unittest.TestCase):
    def test_pos_entry_ships_transfer_rpc_and_roster_option(self):
        root = Path(__file__).resolve().parents[1] / "ury"
        shell = (root / "www/pos.html").read_text()
        entry, = re.findall(r'<script type="module"[^>]*src="/assets/ury/([^"]+)"', shell)
        js = (root / "public" / entry).read_text()
        for marker in ("get_transfer_destinations", "join_roster"):
            with self.subTest(marker=marker):
                self.assertTrue(marker in js, f"{entry} does not ship {marker}")
