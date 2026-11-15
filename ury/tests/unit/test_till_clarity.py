"""Site-less contracts for till clarity: a PMS room lookup hook on the POS
Profile (M02) and a short guest label carried from an order to its kitchen
tickets and split bills without a Customer master (M03).

Both are inert by default: an empty room lookup method keeps today's
free-text room box, and an order without a label prints as before."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from ury import hooks
from ury.ury.doctype.ury_order import test_split_bill_release as split_harness
from ury.ury_pos import api

Row = split_harness.Row
ROOT = Path(__file__).resolve().parents[2]


def _fixture_fields():
    return {field["name"]: field for field in json.loads((ROOT / "fixtures/custom_field.json").read_text())}


def _exported_names():
    fixture = next(row for row in hooks.fixtures if row["doctype"] == "Custom Field")
    return next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])


class TestRoomLookupProfileField(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Salama"))
        self.framework.session.user = "cashier@example.com"
        self.framework.db.exists.return_value = "Salama POS"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Salama POS", branch="Salama", warehouse="Stores", company="Salama",
            table_attention_time=10, print_format="Receipt", paid_limit=0,
            custom_enable_discount=0, custom_enable_multiple_cashier=0,
            custom_edit_order_type=0, custom_enable_kot_reprint=0, qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
        )
        self.framework.get_doc.return_value = self.profile

    def test_fixture_declares_an_optional_data_field(self):
        field = _fixture_fields().get("POS Profile-custom_room_lookup_method", {})
        self.assertEqual(field.get("dt"), "POS Profile")
        self.assertEqual(field.get("fieldname"), "custom_room_lookup_method")
        self.assertEqual(field.get("fieldtype"), "Data")
        self.assertFalse(field.get("default"))
        self.assertFalse(field.get("reqd"))
        self.assertIn("POS Profile-custom_room_lookup_method", _exported_names())

    def test_profile_context_exposes_the_configured_method(self):
        self.profile.custom_room_lookup_method = "hospitality_kamra.room_charge_lookup"
        result = api.getPosProfile()
        self.assertEqual(result.get("room_lookup_method"), "hospitality_kamra.room_charge_lookup")

    def test_unset_method_is_empty_so_the_till_keeps_free_text(self):
        result = api.getPosProfile()
        self.assertIn("room_lookup_method", result)
        self.assertEqual(result.get("room_lookup_method"), "")


class TestGuestLabelFields(unittest.TestCase):
    def test_fixture_declares_guest_label_on_invoice_and_kot(self):
        fields = _fixture_fields()
        for name, dt in (("POS Invoice-custom_guest_label", "POS Invoice"),
                         ("URY KOT-custom_guest_label", "URY KOT")):
            with self.subTest(field=name):
                field = fields.get(name, {})
                self.assertEqual(field.get("dt"), dt)
                self.assertEqual(field.get("fieldname"), "custom_guest_label")
                self.assertEqual(field.get("fieldtype"), "Data")
                self.assertEqual(field.get("length"), 40)
                self.assertIn(name, _exported_names())


class TestGuestLabelOnKot(unittest.TestCase):
    """create_kot_doc copies the order's short guest label onto the ticket."""

    def setUp(self):
        from ury.ury.api.test_ury_kot_notes import OrderingCases

        class _Cases(OrderingCases, unittest.TestCase):
            def runTest(self):
                pass

        self.cases = _Cases()
        self.cases.setUp()
        self.addCleanup(self.cases.doCleanups)
        self.cases.framework.db.get_list.return_value = []
        # the order's label, as sync_order stored it on the POS Invoice
        self.cases.invoice.custom_guest_label = None

    def test_kot_carries_the_order_guest_label(self):
        self.cases.invoice.custom_guest_label = "Susan"
        self.cases.kot.kot_execute(self.cases.invoice.name, "Walk-in", None,
                                   [self.cases.line()], [])
        self.assertEqual([t.custom_guest_label for t in self.cases.tickets], ["Susan"])

    def test_kot_without_a_label_is_unchanged(self):
        self.cases.kot.kot_execute(self.cases.invoice.name, "Walk-in", None,
                                   [self.cases.line()], [])
        self.assertEqual(len(self.cases.tickets), 1)
        self.assertIsNone(getattr(self.cases.tickets[0], "custom_guest_label", None))

    def test_cancel_kot_carries_the_order_guest_label(self):
        self.cases.invoice.custom_guest_label = "Susan"
        self.cases.kot.create_cancel_kot_doc(
            self.cases.invoice.name, None, [self.cases.line()], "Partially cancelled", "Walk-in",
            None, "POS A", "CNCL-.#####", [self.cases.line()], "Kitchen",
        )
        self.assertEqual([t.custom_guest_label for t in self.cases.tickets], ["Susan"])

    def test_cancel_kot_without_a_label_is_unchanged(self):
        self.cases.kot.create_cancel_kot_doc(
            self.cases.invoice.name, None, [self.cases.line()], "Partially cancelled", "Walk-in",
            None, "POS A", "CNCL-.#####", [self.cases.line()], "Kitchen",
        )
        self.assertIsNone(getattr(self.cases.tickets[0], "custom_guest_label", None))


class TestGuestLabelOnDelayNotification(unittest.TestCase):
    """The kitchen's order-delayed alert names the check it is about."""

    def setUp(self):
        from ury.ury.api import ury_kot_notification as notification

        self.notification = notification
        self.framework = self.enterContext(patch.object(notification, "frappe"))
        self.framework.has_permission.return_value = True
        self.kot = frappe._dict(name="KOT-1", restaurant_table="T-05", custom_guest_label=None)
        self.framework.get_doc.return_value = self.kot
        self.framework.db.get_value.side_effect = lambda doctype, name, field: {
            "order_status": "Ready For Prepare", "invoice": "POS-INV-00042",
            "type": "New Order", "pos_profile": "POS A",
        }[field]
        self.framework.get_all.side_effect = lambda doctype, **kwargs: {
            "URY KOT Items": [],
            "URY Notification Recipient": [frappe._dict(receiver_by_role="URY Captain")],
            "Has Role": [{"user": "waiter@example.com"}],
            "User": [frappe._dict(name="waiter@example.com")],
        }[doctype]
        self.sent = []
        self.enterContext(patch.object(
            notification, "create_system_notification",
            side_effect=lambda message, user, subject: self.sent.append((subject, message))))

    def test_delay_alert_names_the_guest(self):
        self.kot.custom_guest_label = "Susan"
        self.notification.order_delay_notification("KOT-1")
        self.assertEqual(len(self.sent), 1)
        subject, message = self.sent[0]
        self.assertEqual(subject, "Order # 00042 Delayed · Susan")
        self.assertIn("<b> Guest : </b> Susan", message)

    def test_delay_alert_label_is_escaped(self):
        self.kot.custom_guest_label = "<b>Bob</b>"
        self.framework.utils.escape_html.side_effect = lambda value: (
            value.replace("<", "&lt;").replace(">", "&gt;"))
        self.notification.order_delay_notification("KOT-1")
        subject, message = self.sent[0]
        self.assertNotIn("<b>Bob</b>", message)
        self.assertIn("&lt;b&gt;Bob&lt;/b&gt;", message)

    def test_delay_alert_without_a_label_is_unchanged(self):
        self.notification.order_delay_notification("KOT-1")
        subject, message = self.sent[0]
        self.assertEqual(subject, "Order # 00042 Delayed")
        self.assertNotIn("Guest", message)


def _print_format_html(folder):
    path = ROOT / "ury" / "print_format" / folder / f"{folder}.json"
    return json.loads(path.read_text())["html"]


class TestGuestLabelOnPrints(unittest.TestCase):
    """Waiter slips and the bill print the check's guest label when it has one."""

    def test_waiter_order_slip_prints_the_label_when_set(self):
        html = _print_format_html("ury_waiter_order_slip")
        self.assertTrue("{% if doc.custom_guest_label %}" in html, "{% if doc.custom_guest_label %}")
        self.assertTrue("{{ doc.custom_guest_label }}" in html, "{{ doc.custom_guest_label }}")

    def test_merged_bill_prints_the_label_when_set(self):
        html = _print_format_html("merged_pos_invoice_format")
        self.assertTrue("{% if doc.custom_guest_label %}" in html, "{% if doc.custom_guest_label %}")
        self.assertTrue("{{ doc.custom_guest_label }}" in html, "{{ doc.custom_guest_label }}")

    def test_print_formats_still_render_as_jinja(self):
        try:
            import jinja2
        except ImportError:  # pragma: no cover - the bench image ships jinja2
            self.skipTest("jinja2 unavailable")
        for folder in ("ury_waiter_order_slip", "merged_pos_invoice_format"):
            with self.subTest(folder=folder):
                jinja2.Environment().parse(_print_format_html(folder))


class TestGuestLabelOnKitchenDisplay(unittest.TestCase):
    """Mosaic (the KDS) has no JS test runner: pin the template source."""

    def setUp(self):
        self.source = (ROOT.parent / "mosaic" / "src" / "components" / "kot.vue").read_text()

    def test_ticket_shows_the_label_for_every_order_type(self):
        self.assertTrue('v-if="kot.custom_guest_label"' in self.source, 'v-if="kot.custom_guest_label"')
        self.assertTrue("{{ kot.custom_guest_label }}" in self.source, "{{ kot.custom_guest_label }}")

    def test_delayed_order_banner_shows_the_label(self):
        self.assertTrue("guest_label: matchingKot?.custom_guest_label" in self.source, "guest_label: matchingKot?.custom_guest_label")
        self.assertTrue('v-if="kotErrorAlert.guest_label"' in self.source, 'v-if="kotErrorAlert.guest_label"')
        self.assertTrue("{{ kotErrorAlert.guest_label }}" in self.source, "{{ kotErrorAlert.guest_label }}")

    def test_kds_list_sends_whole_kot_documents(self):
        # kot_list / the realtime push serialise the full URY KOT, so the
        # custom field arrives without a field list to extend
        display = (ROOT / "ury" / "api" / "ury_kot_display.py").read_text()
        self.assertTrue('kotjson = json.loads(frappe.as_json(kotdoc))' in display, 'kotjson = json.loads(frappe.as_json(kotdoc))')


class TestGuestLabelInCashierLists(unittest.TestCase):
    """The cashier's order list and the split-group panel get the label."""

    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.enterContext(patch.object(api, "_enrich_split_group_meta", side_effect=lambda rows: rows))
        self.framework.db.sql.return_value = []

    def test_every_order_list_query_selects_the_label(self):
        for status in ("Outstanding", "Draft", "Unbilled", "Recently Paid", "Paid"):
            with self.subTest(status=status):
                api.getPosInvoice(status, 2, 0)
                select = self.framework.db.sql.call_args.args[0].split("FROM")[0]
                self.assertIn("custom_guest_label", select)

    def test_order_search_selects_the_label(self):
        self.framework.get_all.return_value = []
        api.searchPosInvoice("Susan", "Draft")
        self.assertIn("custom_guest_label", self.framework.get_all.call_args.kwargs["fields"])

    def test_split_group_selects_the_label(self):
        invoice = frappe._dict(name="POS-1", branch="Branch A", custom_split_group="SG-1")
        self.framework.get_doc.return_value = invoice
        self.framework.has_permission.return_value = True
        self.framework.get_all.return_value = []
        api.get_split_group("POS-1")
        fields = self.framework.get_all.call_args_list[0].kwargs["fields"]
        self.assertIn("custom_guest_label", fields)


class _SplitHarness(split_harness.TestSplitBillAndPrintRelease):
    """The split-bill site-less harness, without re-running its own tests."""

    def runTest(self):
        pass


class TestGuestLabelOnSplitBill(unittest.TestCase):
    def setUp(self):
        self.h = _SplitHarness()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.order = self.h.order
        self.framework = self.h.framework

    def _invoice(self, label):
        invoice = self.h._invoice(items=[Row(name="line-1", item_code="FOOD", item_name="Food", qty=2)])
        invoice.custom_guest_label = label
        self.h._wire_invoice(invoice)
        new_invoice = self.framework.new_doc.return_value
        new_invoice.custom_guest_label = None
        return invoice, new_invoice

    def test_split_bill_names_the_new_check_without_a_customer(self):
        invoice, new_invoice = self._invoice("Table 4")
        self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}], guest_label="  David  ")
        self.assertEqual(new_invoice.custom_guest_label, "David")
        self.assertEqual(invoice.custom_guest_label, "Table 4")
        # a short label, not a Customer master: nothing is looked up or created
        self.assertFalse(any(call.args[:1] == ("Customer",)
                             for call in self.framework.db.get_value.call_args_list))
        self.framework.get_doc.assert_called()
        self.assertFalse(any(call.args[:1] == ("Customer",)
                             for call in self.framework.get_doc.call_args_list))

    def test_split_bill_keeps_the_source_label_when_none_is_given(self):
        invoice, new_invoice = self._invoice("Susan")
        self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}])
        self.assertEqual(new_invoice.custom_guest_label, "Susan")

    def test_split_bill_refuses_a_label_over_forty_characters(self):
        invoice, _new = self._invoice(None)
        with self.assertRaisesRegex(ValueError, "40"):
            self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}], guest_label="x" * 41)


class TestGuestLabelOnSyncOrder(unittest.TestCase):
    def setUp(self):
        from ury.ury.doctype.ury_order.test_sync_order_customer import TestSyncOrderCustomer

        class _Cases(TestSyncOrderCustomer):
            def runTest(self):
                pass

        self.cases = _Cases()
        self.cases.setUp()
        self.addCleanup(self.cases.doCleanups)
        self.cases.invoice.custom_guest_label = None

    def test_order_label_is_stored_on_the_invoice(self):
        self.cases.send("", guest_label="  Susan ")
        self.assertEqual(self.cases.invoice.custom_guest_label, "Susan")
        # the label does not replace the customer: the profile default still applies
        self.assertEqual(self.cases.invoice.customer, "Walk-in Customer")

    def test_no_label_leaves_an_existing_label_alone(self):
        self.cases.invoice.custom_guest_label = "David"
        self.cases.send("")
        self.assertEqual(self.cases.invoice.custom_guest_label, "David")

    def test_an_explicit_blank_label_clears_it(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                self.cases.invoice.custom_guest_label = "David"
                self.cases.send("", guest_label=blank)
                self.assertEqual(self.cases.invoice.custom_guest_label, "")

    def test_a_label_over_forty_characters_is_refused(self):
        with self.assertRaisesRegex(frappe.ValidationError, "40"):
            self.cases.send("", guest_label="x" * 41)
        self.cases.invoice.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
