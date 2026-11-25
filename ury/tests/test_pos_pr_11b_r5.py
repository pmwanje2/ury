"""Observe every client-message append during a raced cancellation lookup."""

import csv
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ury.ury.api.test_ury_kot_notes import OrderingCases


class RecordingLog(list):
    def __init__(self):
        super().__init__(["Previous message"])
        self.appended = []

    def append(self, message):
        self.appended.append(message)
        super().append(message)


class CancellationMessageTests(OrderingCases, TestCase):
    def test_deleted_ticket_only_appends_localized_message(self):
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        self.framework.get_roles.return_value = ["System Manager"]
        self.framework.DoesNotExistError = LookupError
        self.framework.flags.mute_messages = False

        # Frappe msgprint still raises a requested exception when output is muted.
        def throw(message, exc):
            if not self.framework.flags.mute_messages:
                self.framework.local.message_log.append(message)
            raise exc(message)

        def vanished(doctype, name):
            throw(f"{doctype} {name} not found", LookupError)

        self.framework.get_doc.side_effect = vanished
        self.framework.throw.side_effect = throw
        source = "Kitchen ticket {0} was not found."
        catalogs = Path(__file__).resolve().parents[1] / "translations"
        for locale in ("en", "fr", "es", "ar"):
            with self.subTest(locale=locale), (catalogs / f"{locale}.csv").open() as catalog:
                messages = {row[0]: row[1] for row in csv.reader(catalog) if len(row) >= 2}
                expected = messages[source].format("0001")
                log = self.framework.local.message_log = RecordingLog()
                with patch.object(display, "_", side_effect=messages.__getitem__), self.assertRaises(LookupError) as raised:
                    display.confirm_cancel_kot("0001")
                self.assertEqual(str(raised.exception), expected)
                self.assertEqual(log.appended, [expected])
                self.assertEqual(log, ["Previous message", expected])
                self.assertFalse(self.framework.flags.mute_messages)
        self.assertEqual(self.framework.get_doc.call_count, 4)
