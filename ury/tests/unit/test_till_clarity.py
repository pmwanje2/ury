"""Site-less contracts for till clarity (ported from the fork, re-pointed to v16).

M02 / #49, #65, #68: an optional room lookup method on the POS Profile, checked
before a Room Charge, and the cashier's room/folio text sent to the POS Invoice
as room_number. The lookup method is configuration, never a hard-coded hotel
system: it must be a whitelisted method or an enabled API Server Script.

M03: a short guest label (Susan, Seat 2) on the order, carried to its kitchen
tickets, delay alerts, prints, split bills and cashier lists, without a
Customer master.

Inert by default: an empty room lookup method keeps the free-text room box,
and an order without a label prints as before."""

import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury import hooks
from ury.ury.api import ury_kot_generate, ury_kot_notification, ury_kot_validation
from ury.ury.doctype.ury_order import ury_order
from ury.ury.hooks import ury_pos_profile
from ury.ury_pos import api

ROOT = Path(__file__).resolve().parents[2]
ROOM_LOOKUP_INVALID = "[URY-ROOM-LOOKUP-METHOD-INVALID]"


def _raise(message, exc=frappe.ValidationError, *args, **kwargs):
    raise exc(message)


def _fixture_fields():
    rows = json.loads((ROOT / "fixtures" / "custom_field.json").read_text())
    return {row["name"]: row for row in rows}


def _exported_names():
    fixture = next(row for row in hooks.fixtures if isinstance(row, dict) and row.get("doctype") == "Custom Field")
    return next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])


class TestRoomLookupProfileField(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.enterContext(patch.object(api, "get_default_customer_config", return_value={}))
        self.framework.session.user = "cashier@example.com"
        self.framework.db.exists.return_value = "Till A"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Till A", branch="Branch A", warehouse="Stores", company="Company A",
            country="Uganda", table_attention_time=10, print_format="Receipt", paid_limit=0,
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
        self.profile.custom_room_lookup_method = "  pms_app.room_charge_lookup "
        result = api.getPosProfile()
        self.assertEqual(result.get("room_lookup_method"), "pms_app.room_charge_lookup")

    def test_unset_method_is_empty_so_the_till_keeps_free_text(self):
        result = api.getPosProfile()
        self.assertIn("room_lookup_method", result)
        self.assertEqual(result.get("room_lookup_method"), "")


def _whitelisted_lookup(pos_profile=None, room=None):
    return {"room_number": room}


def _plain_function(pos_profile=None, room=None):
    return {"room_number": room}


class TestRoomLookupMethodIsValidated(unittest.TestCase):
    """POS Profile validate refuses a lookup method the till could not call:
    only a whitelisted method or an enabled API Server Script is accepted."""

    def setUp(self):
        self.enterContext(patch.object(ury_pos_profile, "_", side_effect=lambda message: message))
        self.enterContext(patch.object(frappe, "throw", side_effect=_raise))
        self.enterContext(patch.object(frappe, "msgprint"))
        self.api_scripts = {}
        self.enterContext(patch(
            "frappe.core.doctype.server_script.server_script_utils.get_server_script_map",
            side_effect=lambda: {"_api": dict(self.api_scripts)},
        ))
        self.methods = {
            "tests.whitelisted_lookup": _whitelisted_lookup,
            "tests.plain_function": _plain_function,
        }
        self.enterContext(patch.object(frappe, "get_attr", side_effect=self._get_attr))
        self.enterContext(patch.object(frappe, "whitelisted", {_whitelisted_lookup}))

    def _get_attr(self, path):
        if path not in self.methods:
            raise frappe.AppNotInstalledError(f"App {path.split('.')[0]} is not installed")
        return self.methods[path]

    def _validate(self, value):
        doc = frappe._dict(
            name="Till A", custom_room_lookup_method=value,
            printer_settings=[], custom_order_type_customers=[],
        )
        ury_pos_profile.validate(doc, "validate")
        return doc

    def _assert_refused(self, value):
        with self.assertRaises(frappe.ValidationError) as caught:
            self._validate(value)
        self.assertNotIsInstance(caught.exception, frappe.PermissionError)
        self.assertIn(ROOM_LOOKUP_INVALID, str(caught.exception))

    def test_blank_method_is_accepted(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self._validate(value)

    def test_enabled_api_server_script_is_accepted(self):
        self.api_scripts["pms_bridge.room_charge_lookup"] = "Room Charge Lookup"
        self._validate("pms_bridge.room_charge_lookup")

    def test_whitelisted_python_method_is_accepted(self):
        self._validate("tests.whitelisted_lookup")

    def test_a_function_that_is_not_whitelisted_is_refused(self):
        self._assert_refused("tests.plain_function")

    def test_a_method_that_does_not_resolve_is_refused(self):
        self._assert_refused("pms_bridge.room_charge_lookup")

    def test_a_disabled_or_missing_server_script_is_refused(self):
        self.api_scripts["other_bridge.lookup"] = "Other Lookup"
        self._assert_refused("pms_bridge.room_charge_lookup")


class TestRoomChargeSettlement(unittest.TestCase):
    """make_invoice records the cashier's room/folio text as room_number when
    the POS Invoice carries that field (a hotel bridge adds it), before the
    payments are applied; without the field nothing is written."""

    def setUp(self):
        self.invoice = MagicMock()
        self.invoice.name = "INV-1"
        self.invoice.custom_merged_pos_invoice = None
        self.invoice.restaurant_table = None
        self.seen_at_payment = []
        self.invoice.append.side_effect = lambda *args, **kwargs: self.seen_at_payment.append(
            self.invoice.__dict__.get("room_number")
        )
        for target, value in (
            ("_validate_additional_discount", MagicMock(return_value=0)),
            ("get_order_invoice", MagicMock(return_value=self.invoice)),
            ("_submit_and_release_tables", MagicMock()),
        ):
            self.enterContext(patch.object(ury_order, target, value))
        self.enterContext(patch.object(ury_order.frappe, "get_value", return_value="Take Away"))
        # make_invoice records the settling operator (frappe.session.user).
        self.enterContext(patch.object(
            ury_order.frappe, "session", SimpleNamespace(user="cashier@example.com"), create=True,
        ))
        self.enterContext(patch.object(
            ury_order.frappe, "get_cached_doc",
            return_value=frappe._dict(company="Company A", currency="UGX"),
        ))
        self.meta = MagicMock()
        self.get_meta = self.enterContext(patch.object(ury_order.frappe, "get_meta", return_value=self.meta))

    def _pay(self, **kwargs):
        # frappe.call is the request path: arguments the method does not
        # declare are dropped, exactly as for a POST from the till.
        return frappe.call(
            ury_order.make_invoice,
            customer="Walk-in", payments=[{"mode_of_payment": "Room Charge", "amount": 100}],
            cashier="cashier@example.com", pos_profile="Till A", owner="cashier@example.com",
            invoice="INV-1", **kwargs,
        )

    def test_room_number_is_set_before_payment_when_the_invoice_has_the_field(self):
        self.meta.has_field.return_value = True
        self._pay(room_number="ROOM-101")
        self.get_meta.assert_called_once_with("POS Invoice")
        self.meta.has_field.assert_called_once_with("room_number")
        self.assertEqual(self.seen_at_payment, ["ROOM-101"])
        self.assertEqual(self.invoice.room_number, "ROOM-101")

    def test_room_number_is_not_written_when_the_invoice_has_no_such_field(self):
        self.meta.has_field.return_value = False
        self._pay(room_number="ROOM-101")
        self.assertEqual(self.seen_at_payment, [None])
        self.assertNotIn("room_number", self.invoice.__dict__)

    def test_settlement_without_a_room_does_not_touch_the_field(self):
        self.meta.has_field.return_value = True
        self._pay()
        self.get_meta.assert_not_called()
        self.assertNotIn("room_number", self.invoice.__dict__)



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


class _Doc(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class TestGuestLabelOnKot(unittest.TestCase):
    """New, cancel and duplicate (scheduler) KOTs copy the order's label."""

    def setUp(self):
        self.tickets = []
        self.invoice = _Doc(
            name="POS-INV-1", custom_ury_order_number="0042", order_type="Take Away",
            custom_aggregator_id=None, custom_merged_tables=None, branch="Branch A",
            customer="Walk-in", restaurant_table=None, custom_guest_label=None,
        )
        self.enterContext(patch.object(ury_kot_generate.frappe, "get_doc", side_effect=self._get_doc))
        db = self.enterContext(patch("frappe.db", new=MagicMock()))
        db.get_value.return_value = None
        db.get_list.return_value = []
        self.enterContext(patch(
            "ury.ury.api.ury_mto_work_order_service.create_work_orders_for_kot", return_value={},
        ))

    def _get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            ticket = MagicMock()
            ticket.fields = dict(doctype)
            self.tickets.append(ticket)
            return ticket
        self.assertEqual(doctype, "POS Invoice")
        return self.invoice

    def _new_kot(self):
        ury_kot_generate.create_kot_doc(
            "POS-INV-1", "Walk-in", None,
            [{"item_code": "Fish", "item_name": "Fish", "qty": 1, "comments": ""}],
            "New Order", "", "POS A", "KOT-", "Kitchen",
        )
        return self.tickets[-1].fields

    def _cancel_kot(self):
        ury_kot_generate.create_cancel_kot_doc(
            "POS-INV-1", None,
            [{"item_code": "Fish", "item_name": "Fish", "qty": -1, "comments": ""}],
            "Partially cancelled", "Walk-in", None, "POS A", "CNCL-KOT-",
            [{"item_code": "Fish", "qty": 1}], "Kitchen",
        )
        return self.tickets[-1].fields

    def test_new_kot_carries_the_order_guest_label(self):
        self.invoice.custom_guest_label = "Susan"
        self.assertEqual(self._new_kot().get("custom_guest_label"), "Susan")

    def test_new_kot_without_a_label_is_unchanged(self):
        self.assertIsNone(self._new_kot().get("custom_guest_label"))

    def test_cancel_kot_carries_the_order_guest_label(self):
        self.invoice.custom_guest_label = "Susan"
        self.assertEqual(self._cancel_kot().get("custom_guest_label"), "Susan")

    def test_cancel_kot_without_a_label_is_unchanged(self):
        self.assertIsNone(self._cancel_kot().get("custom_guest_label"))

    def test_duplicate_recovery_kot_carries_the_order_guest_label(self):
        self.invoice.custom_guest_label = "Susan"
        created = []

        def new_doc(doctype):
            doc = MagicMock()
            doc.fields = {}
            doc.update.side_effect = doc.fields.update
            created.append(doc)
            return doc

        with patch.object(ury_kot_validation.frappe, "get_doc", return_value=self.invoice), \
                patch.object(ury_kot_validation.frappe, "new_doc", side_effect=new_doc), \
                patch.object(ury_kot_validation, "create_kot_log"):
            ury_kot_validation.create_kot(
                "POS-INV-1", frappe._dict(name="POS A"), "KOT-", [], "waiter@example.com", "Kitchen",
            )
        self.assertEqual(created[0].fields.get("custom_guest_label"), "Susan")


class TestGuestLabelOnDelayNotification(unittest.TestCase):
    """The kitchen's order-delayed alert names the check it is about."""

    def setUp(self):
        from frappe.utils.html_utils import escape_html

        notification = ury_kot_notification
        self.notification = notification
        self.framework = self.enterContext(patch.object(notification, "frappe"))
        self.framework.has_permission.return_value = True
        self.framework.utils.escape_html.side_effect = escape_html
        self.kot = frappe._dict(name="KOT-1", restaurant_table="T-05", custom_guest_label=None)
        self.framework.get_doc.return_value = self.kot
        self.framework.db.get_value.side_effect = lambda doctype, name, field: {
            "order_status": "Ready For Prepare", "invoice": "POS-INV-00042",
            "type": "New Order", "pos_profile": "POS A",
        }[field]
        self.framework.get_all.return_value = [frappe._dict(receiver_by_role="URY Captain")]
        self.enterContext(patch.object(
            notification, "get_users_with_role",
            return_value=[frappe._dict(name="waiter@example.com")],
        ))
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
        self.notification.order_delay_notification("KOT-1")
        subject, message = self.sent[0]
        self.assertNotIn("<b>Bob</b>", message)
        self.assertNotIn("<b>Bob</b>", subject)
        self.assertIn("&lt;b&gt;Bob&lt;/b&gt;", message)

    def test_delay_alert_without_a_label_is_unchanged(self):
        self.notification.order_delay_notification("KOT-1")
        subject, message = self.sent[0]
        self.assertEqual(subject, "Order # 00042 Delayed")
        self.assertNotIn("Guest", message)


def _print_format(folder):
    path = ROOT / "ury" / "print_format" / folder / f"{folder}.json"
    return json.loads(path.read_text())


class TestGuestLabelOnPrints(unittest.TestCase):
    """Waiter slips and the merged bill print the label, escaped."""

    FORMATS = ("ury_waiter_order_slip", "merged_pos_invoice_format")

    def test_both_formats_print_the_label_escaped_when_set(self):
        for folder in self.FORMATS:
            with self.subTest(folder=folder):
                html = _print_format(folder)["html"]
                self.assertIn("{% if doc.custom_guest_label %}", html)
                self.assertIn("{{ doc.custom_guest_label | e }}", html)
                self.assertNotIn("{{ doc.custom_guest_label }}", html)

    @staticmethod
    def _env():
        import jinja2

        # frappe renders print formats with autoescape off
        env = jinja2.Environment(autoescape=False)
        env.globals["_"] = lambda text: text
        env.globals["frappe"] = frappe._dict(utils=frappe._dict(
            fmt_money=lambda value, **kwargs: str(value), formatdate=str,
        ))
        return env

    def test_label_markup_is_escaped_under_autoescape_off(self):
        payload = "<img src=x onerror=alert(1)>"
        env = self._env()
        doc = MagicMock()
        doc.custom_guest_label = payload
        doc.custom_merged_pos_invoice = None
        doc.items = []
        doc.kot_items = []
        doc.get_formatted.side_effect = lambda field, *args, **kwargs: field
        for folder in self.FORMATS:
            with self.subTest(folder=folder):
                rendered = env.from_string(_print_format(folder)["html"]).render(doc=doc)
                self.assertNotIn(payload, rendered)
                self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)

    def test_label_line_is_absent_without_a_label(self):
        env = self._env()
        doc = MagicMock()
        doc.custom_guest_label = None
        doc.custom_merged_pos_invoice = None
        doc.items = []
        doc.kot_items = []
        doc.get_formatted.side_effect = lambda field, *args, **kwargs: field
        for folder in self.FORMATS:
            with self.subTest(folder=folder):
                rendered = env.from_string(_print_format(folder)["html"]).render(doc=doc)
                self.assertNotIn("Guest", rendered)

    def test_standard_merged_bill_is_reloaded_on_migrate(self):
        # a standard Print Format reloads only when its modified moves forward
        self.assertGreater(_print_format("merged_pos_invoice_format")["modified"], "2026-06-23 17:30:10.757495")

    def test_waiter_slip_patch_re_runs_after_upstream_lines(self):
        lines = [line.strip() for line in (ROOT / "patches.txt").read_text().splitlines()]
        rerun = [line for line in lines if line.startswith("ury.patches.v2_0.fix_waiter_order_slip_print_format #")]
        self.assertEqual(len(rerun), 1, "the guest-label slip patch needs a re-suffixed line")
        self.assertGreater(lines.index(rerun[0]), lines.index("ury.patches.v3_44.backfill_wastage_datetime"))


class TestGuestLabelInCashierLists(unittest.TestCase):
    """The cashier's order list, search and split-group panel get the label."""

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


class TestGuestLabelOnSplitBill(unittest.TestCase):
    def setUp(self):
        from ury.ury.doctype.ury_order.test_ury_order import _FakeInvoice, _FakeRow

        self.source = _FakeInvoice(
            name="POS-INV-SRC", docstatus=0, branch="Branch A", company="Company A",
            pos_profile="Till A", customer="Walk In", customer_name="Walk In",
            custom_split_group=None, custom_guest_label=None, rounded_total=200,
            items=[
                _FakeRow(name="line-1", item_code="FOOD", item_name="Food", qty=2, rate=100,
                         reservation_line_key="k1"),
            ],
        )
        self.new_invoice = _FakeInvoice(name=None, invoice_printed=0, invoice_created=0, custom_guest_label=None)
        for target, value in (
            ("_", lambda message: message),
            ("_enforce_order_access", MagicMock()),
            ("getBranch", MagicMock(return_value="Branch A")),
            ("get_branch_stock_policy", MagicMock(return_value=SimpleNamespace(reservation_control_enabled=False))),
        ):
            self.enterContext(patch.object(ury_order, target, value))
        self.get_doc = self.enterContext(patch.object(ury_order.frappe, "get_doc", return_value=self.source))
        self.enterContext(patch.object(ury_order.frappe, "has_permission", return_value=True))
        self.enterContext(patch.object(ury_order.frappe, "new_doc", return_value=self.new_invoice))
        self.enterContext(patch.object(ury_order.frappe, "get_all", return_value=[]))
        self.db = self.enterContext(patch("frappe.db", new=MagicMock()))
        self.db_get_value = self.db.get_value
        self.enterContext(patch("frappe.flags", new=frappe._dict()))

    def _split(self, **kwargs):
        return frappe.call(
            ury_order.split_bill,
            source_invoice="POS-INV-SRC", items_to_move=[{"name": "line-1", "qty": 1}], **kwargs,
        )

    def test_split_bill_names_the_new_check_without_a_customer(self):
        self.source.custom_guest_label = "Table 4"
        self._split(guest_label="  David  ")
        self.assertEqual(self.new_invoice.custom_guest_label, "David")
        self.assertEqual(self.source.custom_guest_label, "Table 4")
        # a short label, not a Customer master: nothing is looked up or created
        self.assertFalse(any(call.args[:1] == ("Customer",) for call in self.db_get_value.call_args_list))
        self.assertFalse(any(call.args[:1] == ("Customer",) for call in self.get_doc.call_args_list))

    def test_split_bill_keeps_the_source_label_when_none_is_given(self):
        self.source.custom_guest_label = "Susan"
        self._split()
        self.assertEqual(self.new_invoice.custom_guest_label, "Susan")

    def test_split_bill_refuses_a_label_over_forty_characters(self):
        with patch.object(ury_order.frappe, "throw", side_effect=_raise):
            with self.assertRaises(frappe.ValidationError) as caught:
                self._split(guest_label="x" * 41)
        self.assertNotIsInstance(caught.exception, frappe.PermissionError)
        self.assertIn("[URY-GUEST-LABEL-TOO-LONG]", str(caught.exception))
        self.assertIsNone(self.new_invoice.name)


class TestGuestLabelOnSyncOrder(unittest.TestCase):
    """sync_order stores the label on the order: a string sets it, a blank
    clears it, an absent label keeps the order's own."""

    def setUp(self):
        self.profile = _Doc(
            name="Till A", customer="Walk-in Customer", company="Company A", currency="UGX",
            role_allowed_for_billing=[], transfer_role_permissions=[],
            role_restricted_for_table_order=[], custom_enable_multiple_cashier=0,
            applicable_for_users=[], remove_items=1,
        )
        self.invoice = MagicMock()
        for name, value in {
            "name": None, "customer": None, "waiter": None, "invoice_printed": 0,
            "restaurant_table": None, "order_type": "Take Away", "invoice_created": 1,
            "items": [], "branch": "Branch A", "selling_price_list": "Standard Selling",
            "custom_merged_tables": "", "custom_guest_label": None, "company": "Company A",
        }.items():
            setattr(self.invoice, name, value)
        for target, value in (
            ("_", lambda message: message),
            ("resolve_default_customer", MagicMock(return_value="Walk-in Customer")),
            ("get_order_invoice", MagicMock(return_value=self.invoice)),
            ("price_items_for_invoice", MagicMock(return_value=[])),
            ("kot_execute", MagicMock()),
            ("_require_open_cashier_session", MagicMock()),
            ("_resolve_sync_opening_room", MagicMock(return_value=None)),
            ("_validate_sync_items_against_menu", MagicMock()),
            ("_resolve_menu_for_sync", MagicMock(return_value="Menu A")),
            ("get_branch_stock_policy", MagicMock(return_value=SimpleNamespace(reservation_control_enabled=False))),
        ):
            self.enterContext(patch.object(ury_order, target, value))
        self.enterContext(patch.object(ury_order.frappe, "get_roles", return_value=["URY Captain"]))
        self.enterContext(patch.object(ury_order.frappe, "has_permission", return_value=True))
        self.enterContext(patch.object(ury_order.frappe, "get_doc", side_effect=self._get_doc))
        db = self.enterContext(patch("frappe.db", new=MagicMock()))
        db.get_value.return_value = None
        self.enterContext(patch.object(ury_order.frappe, "session", SimpleNamespace(user="waiter@example.com")))

    def _get_doc(self, doctype, name=None):
        if doctype == "POS Profile":
            return self.profile
        self.assertEqual(doctype, "Customer")
        return SimpleNamespace(customer_name=name, mobile_number="")

    def send(self, **kwargs):
        return frappe.call(
            ury_order.sync_order,
            items="[]", cashier="x", owner="x", mode_of_payment="Cash", customer="",
            no_of_pax=1, last_invoice=None, waiter="x", pos_profile="Till A",
            order_type="Take Away", **kwargs,
        )

    def test_order_label_is_stored_on_the_invoice(self):
        self.send(guest_label="  Susan ")
        self.assertEqual(self.invoice.custom_guest_label, "Susan")
        # the label does not replace the customer: the profile default still applies
        self.assertEqual(self.invoice.customer, "Walk-in Customer")

    def test_no_label_leaves_an_existing_label_alone(self):
        self.invoice.custom_guest_label = "David"
        self.send()
        self.assertEqual(self.invoice.custom_guest_label, "David")

    def test_an_explicit_blank_label_clears_it(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                self.invoice.custom_guest_label = "David"
                self.send(guest_label=blank)
                self.assertEqual(self.invoice.custom_guest_label, "")

    def test_a_label_over_forty_characters_is_refused_before_saving(self):
        with patch.object(ury_order.frappe, "throw", side_effect=_raise):
            with self.assertRaises(frappe.ValidationError) as caught:
                self.send(guest_label="x" * 41)
        self.assertNotIsInstance(caught.exception, frappe.PermissionError)
        self.assertIn("[URY-GUEST-LABEL-TOO-LONG]", str(caught.exception))
        self.invoice.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
