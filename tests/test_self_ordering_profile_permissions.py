import json
import unittest
from pathlib import Path


class SelfOrderingProfilePermissions(unittest.TestCase):
    def test_ury_admin_retains_v2_level_zero_permissions(self):
        root = Path(__file__).resolve().parents[1]
        path = root / 'ury/ury/doctype/ury_self_ordering_profile/ury_self_ordering_profile.json'
        rows = [row for row in json.loads(path.read_text())['permissions'] if row['role'] == 'URY Admin']
        self.assertEqual(rows, [{
            'role': 'URY Admin', 'create': 1, 'delete': 1, 'email': 1, 'export': 1, 'print': 1,
            'read': 1, 'report': 1, 'select': 1, 'share': 1, 'write': 1,
        }])
