"""The v16 nightly runs every guest-session test module that gates _resolve_session (stdlib only)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUEST_SESSION = (
    "ury.ury.api.test_guest_hardening",
    "ury.ury.api.test_guest_session_reads",
    "ury.ury.api.test_self_ordering_session_endpoints",
)


class V16NightlyGuestModules(unittest.TestCase):
    def test_nightly_shards_list_the_guest_session_modules_once(self):
        workflow = (ROOT / ".github/workflows/test-v16-nightly.yml").read_text()
        listed = [line.strip() for line in workflow.splitlines() if line.strip().startswith("ury.")]
        for module in GUEST_SESSION:
            with self.subTest(module=module):
                self.assertEqual(listed.count(module), 1)
                self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file(), module)


if __name__ == "__main__":
    unittest.main()
