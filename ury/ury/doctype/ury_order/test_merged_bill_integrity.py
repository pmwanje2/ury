"""One guest bill is one check; site-less (frappe double, no bench).

Oracle Simphony Add/Transfer Check consolidates the lines of one or more open checks onto one
destination check, so the guest has one tax calculation, one discount, one fiscal receipt and a
clean return. consolidate_checks does that natively: the destination POS Invoice takes every line
(with its rate, line identity and attribution), the kitchen tickets follow the lines, the source
checks are discarded with an audit note, and no merge link is written.

Bills linked before consolidation existed still settle as two invoices; make_invoice applies the
one bill discount to both halves (G12).

preview_settlement_totals returns the totals the native controller charges at a discount, so the
till never rounds money itself (G17).
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call, patch

APP = Path(__file__).resolve().parents[2]  # ury/ury


class Row(dict):
    __getattr__ = dict.get


class Line(Row):
    """A POS Invoice Item double: default and child-table fields as Frappe stores them."""

    def as_dict(self, no_default_fields=False, no_child_table_fields=False, no_private_properties=False, **kw):
        row = dict(self)
        row["doctype"] = "POS Invoice Item"
        if no_default_fields:
            for key in ("doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"):
                row.pop(key, None)
        if no_child_table_fields:
            for key in ("parent", "parentfield", "parenttype"):
                row.pop(key, None)
        return row


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


def _site_less_frappe():
    framework = MagicMock()
    framework.PermissionError = NotPermitted
    framework.ValidationError = Refused
    framework._ = lambda *messages: messages[0]
    framework.whitelist = lambda *args, **kwargs: (lambda function: function)
    framework.cache = MagicMock()

    def throw(message, exception=None, **kwargs):
        raise (exception or Refused)(message)

    framework.throw = throw
    framework.utils = utils = _stub(
        "frappe.utils",
        cint=lambda value: int(value or 0),
        flt=lambda value, precision=None: round(float(value or 0), precision) if precision else float(value or 0),
        get_datetime=lambda value=None: value,
        get_fullname=lambda value=None: str(value or ""),
        get_system_timezone=lambda: "Africa/Kampala",
        now_datetime=lambda: None,
    )
    return framework, {
        "frappe": framework,
        "frappe.auth": _stub("frappe.auth", LoginAttemptTracker=MagicMock()),
        "frappe.utils": utils,
        "frappe.utils.password": _stub(
            "frappe.utils.password", check_password=MagicMock(), get_encryption_key=lambda: "test-key"
        ),
        "frappe.core": _stub("frappe.core"),
        "frappe.core.doctype": _stub("frappe.core.doctype"),
        "frappe.core.doctype.activity_log": _stub("frappe.core.doctype.activity_log"),
        "frappe.core.doctype.activity_log.activity_log": _stub(
            "frappe.core.doctype.activity_log.activity_log", add_authentication_log=MagicMock()
        ),
        "frappe.core.doctype.user_permission": _stub("frappe.core.doctype.user_permission"),
        "frappe.core.doctype.user_permission.user_permission": _stub(
            "frappe.core.doctype.user_permission.user_permission", get_user_permissions=lambda *a, **k: {}
        ),
        "frappe.model.document": _stub("frappe.model.document", Document=object),
        "erpnext.controllers.queries": _stub("erpnext.controllers.queries", item_query=MagicMock()),
        "ury.ury_pos.api": _stub(
            "ury.ury_pos.api", getBranch=MagicMock(), getBranchRoom=MagicMock(), getRoom=MagicMock(),
            posOpening=MagicMock(),
        ),
        "ury.ury_pos.default_customer": _stub(
            "ury.ury_pos.default_customer", get_default_customer_config=MagicMock(),
            resolve_default_customer=MagicMock(),
        ),
        "ury.ury.api.ury_kot_generate": _stub(
            "ury.ury.api.ury_kot_generate", _execute_kot=MagicMock(), process_items_for_cancel_kot=MagicMock(),
        ),
        "ury.ury.api.ury_order_reservation_service": _stub(
            "ury.ury.api.ury_order_reservation_service", _line_key=MagicMock(), _line_ref=MagicMock(),
            reconcile_order_reservations=MagicMock(), release_order_reservations=MagicMock(),
            resolve_production_context=MagicMock(return_value=None), _warehouse_for_context=MagicMock(),
        ),
        "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy", get_branch_stock_policy=MagicMock()),
        "ury.ury.doctype.alert_settings.alert_settings": _stub(
            "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock(),
        ),
        "ury.ury.api.ury_kot_notification": _stub(
            "ury.ury.api.ury_kot_notification", create_system_notification=MagicMock(),
            get_users_with_role=MagicMock(),
        ),
        "ury.ury.api.item_price": _stub(
            "ury.ury.api.item_price", item_price_release_states=MagicMock(), price_valid_on=MagicMock(),
        ),
    }


def _load():
    framework, modules = _site_less_frappe()
    modules["ury.brand"] = _stub("ury.brand", coded_message=lambda key, msg="": f"[{key}] {msg}".strip())
    with patch.dict(sys.modules, modules):
        for stale in ("ury.ury_pos.discount", "ury.ury_pos._discount_override_stub", "ury.ury_pos.override"):
            sys.modules.pop(stale, None)
        discount_path = APP.parent / "ury_pos/discount.py"
        if discount_path.exists():
            dspec = importlib.util.spec_from_file_location("ury.ury_pos.discount", discount_path)
            dmod = importlib.util.module_from_spec(dspec)
            sys.modules[dspec.name] = dmod
            dspec.loader.exec_module(dmod)
        spec = importlib.util.spec_from_file_location(
            "ury.ury.doctype.ury_order.ury_order", APP / "doctype/ury_order/ury_order.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return framework, module


class Check:
    """A POS Invoice double that records what the controller is asked to do."""

    def __init__(self, name, items=(), **fields):
        self.name = name
        self.items = list(items)
        self.payments = []
        self.flags = SimpleNamespace()
        self.events = []
        self.branch = "Lakeside"
        self.company = "Lakeside Hotel"
        self.order_type = "Dine In"
        self.pos_profile = "Lakeside Till"
        self.restaurant_table = None
        self.custom_merged_tables = None
        self.custom_merged_pos_invoice = None
        self.invoice_printed = 0
        self.docstatus = 0
        self.additional_discount_percentage = 0
        self.discount_amount = 0
        self.apply_discount_on = "Grand Total"
        self.paid_amount = 0
        self.room_number = None
        self.grand_total = self.rounded_total = 0
        self.__dict__.update(fields)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, row):
        getattr(self, key).append(Line(row))

    def calculate_taxes_and_totals(self):
        self.events.append(("totals", self.additional_discount_percentage, len(self.items)))

    def save(self, **kwargs):
        self.events.append(("save", len(self.items), self.invoice_printed))

    def discard(self):
        self.events.append(("discard",))
        self.docstatus = 2

    def db_set(self, field, value, *args, **kwargs):
        self.events.append(("db_set", field, value))
        setattr(self, field, value)

    def set_paid_amount(self):
        pass

    def set_outstanding_amount(self):
        pass

    def check_permission(self, permtype="read"):
        self.events.append(("permission", permtype))


def dish(name, item_code, qty, rate, key, employee="EMP-WAITER", **extra):
    row = dict(
        name=name, parent="SRC", parenttype="POS Invoice", parentfield="items", idx=1, owner="waiter@example.com",
        item_code=item_code, item_name=item_code.title(), qty=qty, rate=rate, price_list_rate=rate,
        reservation_line_key=key, custom_entered_by_employee=employee, comment=None, ury_held=0,
    )
    row.update(extra)
    return Line(row)


class Loaded(unittest.TestCase):
    def setUp(self):
        self.frappe, self.order = _load()
        self.policy = SimpleNamespace(reservation_control_enabled=False)
        self.enterContext(patch.object(self.order, "get_branch_stock_policy", return_value=self.policy))
        self.reconcile = self.enterContext(patch.object(self.order, "reconcile_order_reservations"))
        self.free_tables = self.enterContext(patch.object(self.order, "_free_tables_if_no_open_invoices"))
        self.kots = {}
        self.frappe.get_all.side_effect = lambda doctype, filters=None, pluck=None, **kw: (
            list(self.kots.get(filters["invoice"], [])) if doctype == "URY KOT" else [])


class TestAddCheck(Loaded):
    """G12: consolidate_checks moves the guest's lines onto one check."""

    def setUp(self):
        super().setUp()
        self.destination = Check("CHK-1", items=[dish("d1", "fish", 1, 30000, "k-fish")],
                                 restaurant_table="Pool 1", invoice_printed=1)
        self.source = Check("CHK-2", items=[
            dish("s1", "soda", 2, 5000, "k-soda-1", employee="EMP-BAR"),
            dish("s2", "soda", 1, 5000, "k-soda-2", employee="EMP-BAR", comment="no ice"),
        ], restaurant_table="Pool 2")
        self.kots = {"CHK-2": ["KOT-7", "KOT-8"]}

    def consolidate(self, *sources):
        consolidate = getattr(self.order, "consolidate_checks", None)
        self.assertTrue(callable(consolidate), "consolidate_checks is missing: merged checks are still two invoices")
        return consolidate(self.destination, list(sources or [self.source]))

    def test_every_source_line_moves_with_its_price_identity_and_attribution(self):
        self.consolidate()
        moved = self.destination.items[1:]
        self.assertEqual(
            [(row.item_code, row.qty, row.rate, row.price_list_rate, row.reservation_line_key,
              row.custom_entered_by_employee, row.comment) for row in moved],
            [("soda", 2, 5000, 5000, "k-soda-1", "EMP-BAR", None),
             ("soda", 1, 5000, 5000, "k-soda-2", "EMP-BAR", "no ice")],
        )
        for row in moved:
            for field in ("name", "parent", "parenttype", "parentfield", "idx", "owner"):
                self.assertNotIn(field, row, f"moved line still carries the source row's {field}")

    def test_the_destination_is_totalled_once_and_saved_with_all_lines(self):
        self.consolidate()
        self.assertEqual([e for e in self.destination.events if e[0] in ("totals", "save")],
                         [("totals", 0, 3), ("save", 3, 0)])

    def test_the_consolidated_check_must_be_printed_again(self):
        """Its printed bill no longer shows what the guest owes."""
        self.consolidate()
        self.assertEqual(self.destination.invoice_printed, 0)

    def test_no_merge_link_is_written(self):
        """One invoice: one tax calculation, one fiscal receipt, a native return."""
        self.consolidate()
        self.assertFalse(self.destination.custom_merged_pos_invoice)
        self.assertFalse(self.source.custom_merged_pos_invoice)
        written = [c for c in self.frappe.db.set_value.call_args_list if "custom_merged_pos_invoice" in str(c)]
        self.assertEqual(written, [])

    def test_the_source_check_is_discarded_with_an_audit_note(self):
        self.consolidate()
        self.assertEqual(self.source.docstatus, 2)
        self.assertIn(("db_set", "cancel_reason", "Added to check CHK-1"), self.source.events)

    def test_the_kitchen_tickets_follow_the_lines(self):
        self.consolidate()
        self.frappe.db.set_value.assert_has_calls([
            call("URY KOT", "KOT-7", "invoice", "CHK-1", update_modified=False),
            call("URY KOT", "KOT-8", "invoice", "CHK-1", update_modified=False),
        ])

    def test_the_source_table_is_freed_once_no_open_check_remains_on_it(self):
        self.consolidate()
        self.free_tables.assert_called_once_with("Pool 2", None)

    def test_a_source_on_the_destinations_table_frees_nothing(self):
        self.source.restaurant_table = "Pool 1"
        self.consolidate()
        self.free_tables.assert_not_called()

    def test_several_source_checks_consolidate_in_one_step(self):
        third = Check("CHK-3", items=[dish("t1", "chips", 1, 7000, "k-chips")])
        self.consolidate(self.source, third)
        self.assertEqual([row.item_code for row in self.destination.items], ["fish", "soda", "soda", "chips"])
        self.assertEqual((self.source.docstatus, third.docstatus), (2, 2))
        self.assertEqual([e for e in self.destination.events if e[0] == "save"], [("save", 4, 0)])

    def test_reservations_follow_the_lines_when_stock_reservation_is_on(self):
        self.policy.reservation_control_enabled = True
        self.consolidate()
        lines = lambda rows: [
            {"reservation_line_key": key, "item_code": code, "item_name": code.title(), "qty": qty}
            for key, code, qty in rows]
        self.reconcile.assert_has_calls([
            call(order_ref="CHK-2", previous_items=lines([("k-soda-1", "soda", 2), ("k-soda-2", "soda", 1)]),
                 accepted_items=[], branch="Lakeside", company="Lakeside Hotel", actor=self.frappe.session.user),
            call(order_ref="CHK-1", previous_items=lines([("k-fish", "fish", 1)]),
                 accepted_items=lines([("k-fish", "fish", 1), ("k-soda-1", "soda", 2), ("k-soda-2", "soda", 1)]),
                 branch="Lakeside", company="Lakeside Hotel", actor=self.frappe.session.user),
        ])

    def test_reservations_are_untouched_when_stock_reservation_is_off(self):
        self.consolidate()
        self.reconcile.assert_not_called()


class TestLegacyLinkedSettlement(Loaded):
    """G12: a bill linked before consolidation existed takes the one discount on both halves."""

    def setUp(self):
        super().setUp()
        self.profile = Row(name="Lakeside Till", company="Lakeside Hotel", currency="UGX", customer="Walk-in",
                           custom_enable_discount=1, allow_discount_change=1)
        self.frappe._dict = Row
        self.frappe.get_cached_doc.return_value = self.profile
        self.frappe.get_value.return_value = "Dine In"
        self.frappe.db.get_value.side_effect = self._db_get_value
        self.frappe.db.exists.side_effect = lambda dt, name=None: name != "POS Privilege Rule"
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: f in (
            "custom_discount_reason", "custom_discount_note", "custom_discount_approver",
            "custom_discount_approved_percent", "ury_discount_meta", "allow_discount_change",
            "max_document_discount_percent", "discount_limits_by_role", "discount_approver_role",
        ))
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.session = SimpleNamespace(user="cashier")
        self.primary = Check("CHK-1", custom_merged_pos_invoice="CHK-2", rounded_total=100, grand_total=100)
        self.partner = Check("CHK-2", custom_merged_pos_invoice="CHK-1", rounded_total=80, grand_total=80)
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: (
            self.profile if doctype == "POS Profile" else self.partner
        )
        for helper in ("_validate_client_ids", "_lock_settlement_customer", "_client_order_invoice",
                       "_check_held_before_settlement", "_refuse_client_order_rebind", "_set_settlement_customer",
                       "_enforce_on_account"):
            self.enterContext(patch.object(self.order, helper, return_value=None))
        self.enterContext(patch.object(self.order, "get_order_invoice", return_value=self.primary))

    def _db_get_value(self, doctype, filters, fieldname=None, as_dict=False, **kw):
        if doctype == "Approval Limit":
            data = {"max_document_discount_percent": "0", "discount_limits_by_role": "URY Cashier: 20", "discount_approver_role": "URY Manager"}
            if isinstance(fieldname, (list, tuple)):
                return Row({f: data.get(f) for f in fieldname}) if as_dict else tuple(data.get(f) for f in fieldname)
            return data.get(fieldname)
        if doctype == "Discount Reason":
            data = {"name": "Complaint", "code": "Complaint", "description": "Complaint", "applies_to": "Both", "requires_supervisor": 0, "requires_note": 0, "disabled": 0}
            if isinstance(fieldname, (list, tuple)):
                return Row({f: data.get(f) for f in fieldname}) if as_dict else tuple(data.get(f) for f in fieldname)
            return data.get(fieldname)
        return 0

    def test_the_bill_discount_applies_to_the_linked_partner_before_it_is_totalled(self):
        result = self.order.make_invoice(
            customer="Walk-in", payments=[{"mode_of_payment": "Cash", "amount": 162}], cashier="cashier",
            pos_profile="Lakeside Till", owner="cashier", discount_percent=10, discount_reason="Complaint",
            invoice="CHK-1", submit_invoice=0,
        )
        self.assertEqual(result["status"], "Draft")
        self.assertEqual(self.partner.additional_discount_percentage, 10)
        self.assertEqual([e for e in self.partner.events if e[0] == "totals"][0], ("totals", 10, 0))


class TestSettlementTotalsPreview(Loaded):
    """G17: the till shows the totals the native controller will charge, not its own rounding."""

    def setUp(self):
        super().setUp()
        self.profile = Row(name="Lakeside Till", company="Lakeside Hotel", currency="USD", customer="Walk-in",
                           custom_enable_discount=1, allow_discount_change=1)
        self.check = Check("CHK-1", currency="USD", grand_total=1234.0, rounded_total=1234.0, total=1234.0, net_total=1234.0)
        self.partner = Check("CHK-2", currency="USD", grand_total=500.28, rounded_total=500.28, total=500.28, net_total=500.28)
        self.frappe.get_cached_doc.return_value = self.profile
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.session = SimpleNamespace(user="cashier")
        self.frappe.db.exists.return_value = True
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: f in ("allow_discount_change", "custom_discount_reason"))
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: (
            self.profile if doctype == "POS Profile" else {"CHK-1": self.check, "CHK-2": self.partner}[name]
        )
        self.access = self.enterContext(patch.object(self.order, "_enforce_order_access"))

        def priced(bill, grand, rounded):
            def totals():
                bill.events.append(("totals", bill.additional_discount_percentage, len(bill.items)))
                bill.grand_total, bill.rounded_total = grand, rounded
            return totals

        self.check.calculate_taxes_and_totals = priced(self.check, 1110.5, 1110)
        self.partner.calculate_taxes_and_totals = priced(self.partner, 450.25, 450)

    def preview(self, **kwargs):
        preview = getattr(self.order, "preview_settlement_totals", None)
        self.assertTrue(callable(preview), "preview_settlement_totals is missing: the till rounds money itself")
        return preview(invoice="CHK-1", pos_profile="Lakeside Till", discount_percent=10, **kwargs)

    def test_returns_the_native_totals_at_the_validated_discount_without_saving(self):
        res = self.preview()
        self.assertEqual(res["grand_total"], 1110.5)
        self.assertEqual(res["rounded_total"], 1110)
        self.assertEqual(self.check.events[-1], ("totals", 10, 0))
        self.assertFalse([e for e in self.check.events if e[0] == "save"])
        self.assertIn(("permission", "read"), self.check.events)
        self.access.assert_called_once_with(self.check)

    def test_a_linked_bill_previews_both_halves_at_the_one_discount(self):
        self.check.custom_merged_pos_invoice = "CHK-2"
        res = self.preview()
        self.assertEqual(res["grand_total"], 1560.75)
        self.assertEqual(res["rounded_total"], 1560)
        self.assertEqual(self.partner.events[-1], ("totals", 10, 0))

    def test_another_tills_check_is_refused(self):
        self.check.pos_profile = "Pool Bar Till"
        with self.assertRaises(NotPermitted):
            self.preview()


class PaymentRow(Row):
    """A POS Invoice Payment child-row double."""

    def as_dict(self, no_default_fields=False, no_child_table_fields=False, no_private_properties=False, **kw):
        row = dict(self)
        row["doctype"] = "Sales Invoice Payment"
        if no_default_fields:
            for key in ("doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"):
                row.pop(key, None)
        if no_child_table_fields:
            for key in ("parent", "parentfield", "parenttype"):
                row.pop(key, None)
        return row


class TestAddCheckDiscountsAndTenders(Loaded):
    """P1: Simphony Add Check carries source bill-level discounts and tenders onto one check,
    or refuses cleanly when a tender/discount cannot be moved."""

    def setUp(self):
        super().setUp()
        self.allowed_modes = []
        self.frappe.get_all.side_effect = self._get_all
        self.destination = Check(
            "CHK-1",
            items=[dish("d1", "chicken", 1, 15.0, "k-chicken")],
            restaurant_table="T-101",
            payments=[PaymentRow(mode_of_payment="Cash", amount=0.0)],
        )
        self.source = Check(
            "CHK-2",
            items=[dish("s1", "tea", 1, 5.0, "k-tea")],
            restaurant_table="T-102",
            payments=[PaymentRow(mode_of_payment="Cash", amount=0.0)],
        )

    def _get_all(self, doctype, filters=None, pluck=None, **kw):
        if doctype == "URY KOT":
            return list(self.kots.get(filters["invoice"], []))
        if doctype == "POS Payment Method":
            return list(self.allowed_modes)
        return []

    def test_merging_a_discounted_source_onto_an_undiscounted_destination_keeps_the_discount(self):
        self.source.additional_discount_percentage = 10.0
        self.source.discount_amount = 0.59
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.destination.additional_discount_percentage, 0)
        self.assertAlmostEqual(self.destination.discount_amount, 0.59, places=2)
        self.assertEqual(self.destination.apply_discount_on, "Grand Total")

    def test_merging_two_differently_discounted_checks_sums_both_discount_amounts(self):
        self.destination.additional_discount_percentage = 10.0
        self.destination.discount_amount = 1.77
        self.source.additional_discount_percentage = 25.0
        self.source.discount_amount = 1.48
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.destination.additional_discount_percentage, 0)
        self.assertAlmostEqual(self.destination.discount_amount, 3.25, places=2)

    def test_merging_an_undiscounted_source_onto_a_discounted_destination_does_not_expand_discount(self):
        self.destination.additional_discount_percentage = 10.0
        self.destination.discount_amount = 1.77
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.destination.additional_discount_percentage, 0)
        self.assertAlmostEqual(self.destination.discount_amount, 1.77, places=2)

    def test_merging_checks_with_conflicting_discount_bases_is_refused(self):
        self.destination.apply_discount_on = "Net Total"
        self.destination.discount_amount = 1.50
        self.source.apply_discount_on = "Grand Total"
        self.source.discount_amount = 0.59
        with self.assertRaisesRegex(Refused, "different bases"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_source_tender_moves_onto_destination_as_one_tender_set(self):
        self.source.payments = [
            PaymentRow(
                name="p2",
                parent="CHK-2",
                mode_of_payment="Cash",
                amount=5.90,
                custom_tender_received=1,
            )
        ]
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(
            [
                (row.mode_of_payment, round(float(row.amount), 2), int(row.get("custom_tender_received") or 0))
                for row in self.destination.payments
            ],
            [("Cash", 5.90, 1)],
        )

    def test_matching_tenders_on_destination_and_source_combine_without_duplicates(self):
        self.destination.payments = [
            PaymentRow(mode_of_payment="Cash", amount=10.0, custom_tender_received=1)
        ]
        self.source.payments = [
            PaymentRow(mode_of_payment="Cash", amount=5.90, custom_tender_received=1),
            PaymentRow(mode_of_payment="Mobile Money", amount=4.0, custom_tender_received=1),
        ]
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(
            [
                (row.mode_of_payment, round(float(row.amount), 2), int(row.get("custom_tender_received") or 0))
                for row in self.destination.payments
            ],
            [("Cash", 15.90, 1), ("Mobile Money", 4.0, 1)],
        )

    def test_source_tender_with_external_reference_refuses_merge(self):
        self.source.payments = [
            PaymentRow(
                mode_of_payment="Card",
                amount=5.90,
                reference_no="AUTH-991",
                custom_tender_received=1,
            ),
        ]
        with self.assertRaisesRegex(Refused, "external payment reference"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_destination_tender_with_reference_no_refuses_merge(self):
        self.destination.payments = [
            PaymentRow(
                mode_of_payment="Card",
                amount=10.0,
                reference_no="AUTH-DEST-1",
                custom_tender_received=1,
            ),
        ]
        self.source.payments = [
            PaymentRow(mode_of_payment="Card", amount=5.0, custom_tender_received=1),
        ]
        with self.assertRaisesRegex(Refused, "external payment reference"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertAlmostEqual(float(self.destination.payments[0].amount), 10.0, places=2)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_destination_tender_with_clearance_date_refuses_merge(self):
        self.destination.payments = [
            PaymentRow(
                mode_of_payment="Card",
                amount=10.0,
                clearance_date="2026-11-24",
                custom_tender_received=1,
            ),
        ]
        with self.assertRaisesRegex(Refused, "external payment reference"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_source_tender_not_configured_on_destination_profile_refuses_merge(self):
        self.allowed_modes = ["Cash", "Mobile Money"]
        self.source.payments = [
            PaymentRow(mode_of_payment="Voucher", amount=5.0, custom_tender_received=1)
        ]
        with self.assertRaisesRegex(Refused, "not configured on POS Profile"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_unreceived_positive_draft_rows_are_ignored_on_merge(self):
        """Legacy non-zero placeholders, unconfirmed Phone rows, and failed-print rows
        (custom_tender_received=0) are not received money and must neither move nor block merge."""
        self.destination.payments = [
            PaymentRow(mode_of_payment="Cash", amount=10.0, custom_tender_received=0),
        ]
        self.source.payments = [
            PaymentRow(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72, custom_tender_received=0),
            PaymentRow(mode_of_payment="Cash", amount=5.90, custom_tender_received=0),
        ]
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 2)
        # Unreceived rows from source must never be carried onto destination as received tenders.
        self.assertFalse(
            any(int(r.get("custom_tender_received") or 0) == 1 for r in self.destination.payments)
        )
        self.assertFalse(
            any(r.get("mode_of_payment") == "Online Mobile Money" for r in self.destination.payments)
        )

    def test_moved_received_tender_discards_unreceived_destination_placeholder_and_handles_dict_rows(self):
        class FrappeStyleDict(dict):
            def __getattr__(self, key):
                return self.get(key)

        self.destination.payments = [
            PaymentRow(mode_of_payment="Cash", amount=10.0, custom_tender_received=0),
        ]
        self.source.payments = [
            FrappeStyleDict({"mode_of_payment": "Cash", "amount": 5.90, "custom_tender_received": 1}),
        ]
        self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(
            [
                (r.get("mode_of_payment"), round(float(r.get("amount")), 2), int(r.get("custom_tender_received") or 0))
                for r in self.destination.payments
            ],
            [("Cash", 5.90, 1)],
        )

    def test_confirmed_paid_phone_payment_request_on_source_refuses_merge(self):
        self.paid_requests = {
            "CHK-2": [
                Row(
                    name="PR-PAID-1",
                    mode_of_payment="Online Mobile Money",
                    payment_account="MoMo - LH",
                    grand_total=5.90,
                    outstanding_amount=0.0,
                    status="Paid",
                )
            ]
        }

        def get_all(doctype, filters=None, pluck=None, **kw):
            if doctype == "Payment Request":
                return list(self.paid_requests.get((filters or {}).get("reference_name"), []))
            return self._get_all(doctype, filters=filters, pluck=pluck, **kw)

        self.frappe.get_all.side_effect = get_all
        self.source.payments = [
            PaymentRow(mode_of_payment="Online Mobile Money", type="Phone", amount=5.90, custom_tender_received=0),
        ]
        with self.assertRaisesRegex(Refused, "external payment reference"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_negative_source_tender_refuses_merge(self):
        self.source.payments = [PaymentRow(mode_of_payment="Cash", amount=-5.0)]
        with self.assertRaisesRegex(Refused, "negative tender"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_conflicting_room_charge_references_refuse_merge(self):
        self.destination.room_number = "101"
        self.source.room_number = "202"
        with self.assertRaisesRegex(Refused, "room-charge"):
            self.order.consolidate_checks(self.destination, [self.source])
        self.assertEqual(self.source.docstatus, 0)
        self.assertFalse([e for e in self.destination.events if e[0] == "save"])

    def test_make_invoice_without_new_discount_preserves_saved_check_discounts(self):
        profile = Row(name="Lakeside Till", company="Lakeside Hotel", currency="USD", customer="Walk-in",
                      custom_enable_discount=1, allow_discount_change=1)
        self.frappe.get_cached_doc.return_value = profile
        self.frappe.get_value.return_value = "Dine In"
        self.frappe.db.get_value.return_value = 0
        self.frappe.get_roles.return_value = ["URY Cashier"]
        for helper in ("_validate_client_ids", "_lock_settlement_customer", "_client_order_invoice",
                       "_check_held_before_settlement", "_refuse_client_order_rebind", "_set_settlement_customer",
                       "_enforce_on_account"):
            self.enterContext(patch.object(self.order, helper, return_value=None))

        carried = Check("CHK-CARRIED", additional_discount_percentage=0, discount_amount=0.59,
                        rounded_total=23.01, grand_total=23.01)
        with patch.object(self.order, "get_order_invoice", return_value=carried):
            self.order.make_invoice(
                customer="Walk-in", payments=[{"mode_of_payment": "Mobile Money", "amount": 23.01}],
                cashier="cashier", pos_profile="Lakeside Till", owner="cashier",
                invoice="CHK-CARRIED", submit_invoice=0,
            )
        self.assertEqual(carried.additional_discount_percentage, 0)
        self.assertAlmostEqual(carried.discount_amount, 0.59, places=2)

        # Saved open-check % discount applied before opening PaymentDialog is preserved on settle
        saved_pct = Check("CHK-PCT", additional_discount_percentage=10, discount_amount=25.0,
                          rounded_total=225.0, grand_total=225.0)
        with patch.object(self.order, "get_order_invoice", return_value=saved_pct):
            self.order.make_invoice(
                customer="Walk-in", payments=[{"mode_of_payment": "Mobile Money", "amount": 225.0}],
                cashier="cashier", pos_profile="Lakeside Till", owner="cashier",
                invoice="CHK-PCT", submit_invoice=0,
            )
        self.assertEqual(saved_pct.additional_discount_percentage, 10)
        self.assertAlmostEqual(saved_pct.discount_amount, 25.0, places=2)


class TestSettlementPreservesExistingTenders(Loaded):
    """P1: make_invoice must keep every earlier tender (mode, amount, reference) plus new tenders,
    never silently dropping or reducing a tender already recorded on the check."""

    def setUp(self):
        super().setUp()
        self.profile = Row(
            name="Lakeside Till",
            company="Lakeside Hotel",
            currency="USD",
            customer="Walk-in",
            custom_enable_discount=1,
            allow_discount_change=1,
        )
        self.frappe.get_cached_doc.return_value = self.profile
        self.frappe.get_value.return_value = "Dine In"
        self.frappe.db.get_value.return_value = 0
        self.frappe.get_roles.return_value = ["URY Cashier"]
        for helper in (
            "_validate_client_ids",
            "_lock_settlement_customer",
            "_client_order_invoice",
            "_check_held_before_settlement",
            "_refuse_client_order_rebind",
            "_set_settlement_customer",
            "_enforce_on_account",
        ):
            self.enterContext(patch.object(self.order, helper, return_value=None))

    def _settle(self, check, payments, submit_invoice=0, room_number=None):
        with patch.object(self.order, "get_order_invoice", return_value=check):
            return self.order.make_invoice(
                customer="Walk-in",
                payments=payments,
                cashier="cashier",
                pos_profile=self.profile.name,
                owner="cashier",
                additionalDiscount=None,
                invoice=check.name,
                submit_invoice=submit_invoice,
                room_number=room_number,
            )

    def test_refuses_dropping_an_existing_tender_on_settle(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            discount_amount=0.59,
            payments=[PaymentRow(mode_of_payment="Cash", amount=5.31, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "Cash"):
            self._settle(check, [{"mode_of_payment": "Mobile Money", "amount": 23.01}])
        self.assertEqual([(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments], [("Cash", 5.31)])
        self.assertFalse([e for e in check.events if e[0] == "save"])

    def test_refuses_reducing_an_existing_tender_on_settle(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            payments=[PaymentRow(mode_of_payment="Cash", amount=5.31, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "Cash"):
            self._settle(
                check,
                [
                    {"mode_of_payment": "Cash", "amount": 2.00},
                    {"mode_of_payment": "Mobile Money", "amount": 21.01},
                ],
            )
        self.assertEqual([(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments], [("Cash", 5.31)])
        self.assertFalse([e for e in check.events if e[0] == "save"])

    def test_keeps_earlier_tender_with_reference_plus_new_tender(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            payments=[
                PaymentRow(
                    name="pay-1",
                    parent="LKG-00061",
                    mode_of_payment="Card",
                    amount=10.0,
                    reference_no="AUTH-991",
                    reference_date="2026-11-24",
                    clearance_date="2026-11-24",
                    account="Card - LH",
                    custom_tender_received=1,
                )
            ],
        )
        self._settle(
            check,
            [
                {"mode_of_payment": "Card", "amount": 10.0, "reference_no": "AUTH-991"},
                {"mode_of_payment": "Cash", "amount": 13.01},
            ],
        )
        self.assertEqual(
            [(r.mode_of_payment, round(float(r.amount), 2), r.get("reference_no")) for r in check.payments],
            [("Card", 10.0, "AUTH-991"), ("Cash", 13.01, None)],
        )
        self.assertEqual(check.payments[0].get("clearance_date"), "2026-11-24")
        self.assertEqual(check.payments[0].get("account"), "Card - LH")
        self.assertEqual(int(check.payments[0].get("custom_tender_received") or 0), 1)
        self.assertEqual(int(check.payments[1].get("custom_tender_received") or 0), 0)

    def test_refuses_altering_an_existing_tender_reference(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            payments=[
                PaymentRow(
                    mode_of_payment="Card",
                    amount=10.0,
                    reference_no="AUTH-991",
                    custom_tender_received=1,
                )
            ],
        )
        with self.assertRaisesRegex(Refused, "Card"):
            self._settle(
                check,
                [
                    {"mode_of_payment": "Card", "amount": 10.0, "reference_no": "TAMPERED"},
                    {"mode_of_payment": "Cash", "amount": 13.01},
                ],
            )
        self.assertFalse([e for e in check.events if e[0] == "save"])

    def test_keeps_earlier_and_new_tenders_and_is_idempotent_on_retry(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            payments=[PaymentRow(mode_of_payment="Cash", amount=5.31, custom_tender_received=1)],
        )
        payload = [
            {"mode_of_payment": "Cash", "amount": 5.31},
            {"mode_of_payment": "Mobile Money", "amount": 17.70},
        ]
        self._settle(check, payload)
        self.assertEqual(
            [(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments],
            [("Cash", 5.31), ("Mobile Money", 17.70)],
        )
        # Re-submitting the same full tender list (e.g. after a failed print) must not double tenders.
        self._settle(check, payload)
        self.assertEqual(
            [(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments],
            [("Cash", 5.31), ("Mobile Money", 17.70)],
        )
        # Switching the unreceived balance tender (Mobile Money -> Card) after print failure is allowed!
        self._settle(
            check,
            [
                {"mode_of_payment": "Cash", "amount": 5.31},
                {"mode_of_payment": "Card", "amount": 17.70},
            ],
        )
        self.assertEqual(
            [
                (r.mode_of_payment, round(float(r.amount), 2), int(r.get("custom_tender_received") or 0))
                for r in check.payments
            ],
            [("Cash", 5.31, 1), ("Card", 17.70, 0)],
        )

    def test_refuses_negative_payment_offsetting_existing_tender(self):
        check = Check(
            "LKG-00061",
            grand_total=23.01,
            rounded_total=23.01,
            payments=[PaymentRow(mode_of_payment="Cash", amount=5.31, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "Negative"):
            self._settle(
                check,
                [
                    {"mode_of_payment": "Cash", "amount": 5.31},
                    {"mode_of_payment": "Cash", "amount": -5.31},
                    {"mode_of_payment": "Mobile Money", "amount": 23.01},
                ],
            )
        self.assertFalse([e for e in check.events if e[0] == "save"])

    def test_ugx_100_unit_rounding_over_tender_and_change_with_prior_tender(self):
        self.profile.currency = "UGX"
        check = Check(
            "KBG-00003",
            currency="UGX",
            grand_total=19057,
            rounded_total=19100,
            payments=[PaymentRow(mode_of_payment="Cash", amount=19000, custom_tender_received=1)],
        )
        check.precision = lambda field: 0
        check.set_paid_amount = lambda: setattr(
            check, "paid_amount", sum(float(r.amount) for r in check.payments)
        )
        check.set_outstanding_amount = lambda: (
            setattr(check, "change_amount", max(0, check.paid_amount - check.rounded_total)),
            setattr(check, "outstanding_amount", max(0, check.rounded_total - check.paid_amount)),
        )
        self._settle(
            check,
            [
                {"mode_of_payment": "Cash", "amount": 19000},
                {"mode_of_payment": "Cash", "amount": 1000},
            ],
        )
        self.assertEqual([(r.mode_of_payment, int(r.amount)) for r in check.payments], [("Cash", 19000), ("Cash", 1000)])
        self.assertEqual(check.paid_amount, 20000)
        self.assertEqual(check.change_amount, 900)
        self.assertEqual(check.outstanding_amount, 0)

    def test_refuses_changing_existing_room_reference_after_tender_recorded(self):
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda field: field == "room_number")
        check = Check(
            "LKG-ROOM",
            grand_total=23.01,
            rounded_total=23.01,
            room_number="Room 204",
            payments=[PaymentRow(mode_of_payment="Guest Folio", amount=5.31, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "Room 204"):
            self._settle(
                check,
                [
                    {"mode_of_payment": "Guest Folio", "amount": 5.31},
                    {"mode_of_payment": "Cash", "amount": 17.70},
                ],
                room_number="Room 999",
            )
        self.assertEqual(check.room_number, "Room 204")
        self.assertFalse([e for e in check.events if e[0] == "save"])

    def test_whitespace_room_number_does_not_overwrite_stored_room_and_trims_valid_room(self):
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda field: field == "room_number")
        check = Check(
            "LKG-ROOM-WS",
            grand_total=23.01,
            rounded_total=23.01,
            room_number="Room 204",
            payments=[PaymentRow(mode_of_payment="Guest Folio", amount=5.31, custom_tender_received=1)],
        )
        self._settle(
            check,
            [
                {"mode_of_payment": "Guest Folio", "amount": 5.31},
                {"mode_of_payment": "Cash", "amount": 17.70},
            ],
            room_number="   ",
        )
        self.assertEqual(check.room_number, "Room 204")

        with self.assertRaisesRegex(Refused, "Room 204"):
            self._settle(
                check,
                [
                    {"mode_of_payment": "Guest Folio", "amount": 5.31},
                    {"mode_of_payment": "Cash", "amount": 17.70},
                ],
                room_number="  Room 999  ",
            )
        self.assertEqual(check.room_number, "Room 204")

        # On a check with no received tender (e.g. after print failure), room number can be corrected and is trimmed.
        unreceived = Check(
            "LKG-ROOM-UNRCV",
            grand_total=5.90,
            rounded_total=5.90,
            room_number="Room 204",
            payments=[PaymentRow(mode_of_payment="Guest Folio", amount=5.90, custom_tender_received=0)],
        )
        self._settle(
            unreceived,
            [{"mode_of_payment": "Guest Folio", "amount": 5.90}],
            room_number="  Room 305  ",
        )
        self.assertEqual(unreceived.room_number, "Room 305")

    def test_unreceived_positive_draft_rows_do_not_block_switching_payment_method(self):
        """Legacy non-zero placeholder drafts, unconfirmed self-order Phone rows, and
        failed Step-1 print rows (custom_tender_received=0) must never lock payment method."""
        for label, row in (
            ("legacy_placeholder", PaymentRow(mode_of_payment="Cash", amount=5.90, custom_tender_received=0)),
            ("unconfirmed_phone", PaymentRow(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72, custom_tender_received=0)),
            ("step1_print_fail", PaymentRow(mode_of_payment="Mobile Money", amount=5.90, custom_tender_received=0)),
        ):
            with self.subTest(case=label):
                check = Check("LKG-SWITCH", grand_total=5.90, rounded_total=5.90, payments=[row])
                self._settle(check, [{"mode_of_payment": "Card", "amount": 5.90}])
                self.assertEqual(
                    [(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments],
                    [("Card", 5.90)],
                )

    def test_client_cannot_mark_tender_received_via_make_invoice_and_print_fail_allows_mode_switch(self):
        check = Check("LKG-PRINT-FAIL", grand_total=5.90, rounded_total=5.90)
        # Step 1 (submit_invoice=0) — even a hostile client passing custom_tender_received=1 cannot set it!
        self._settle(
            check,
            [{"mode_of_payment": "Mobile Money", "amount": 5.90, "custom_tender_received": 1}],
            submit_invoice=0,
        )
        self.assertEqual(int(check.payments[0].get("custom_tender_received") or 0), 0)

        # Print fails -> cashier switches to Cash -> succeeds cleanly!
        self._settle(check, [{"mode_of_payment": "Cash", "amount": 5.90}], submit_invoice=0)
        self.assertEqual(
            [(r.mode_of_payment, round(float(r.amount), 2), int(r.get("custom_tender_received") or 0)) for r in check.payments],
            [("Cash", 5.90, 0)],
        )

    def test_make_invoice_refuses_unconfirmed_phone_payment_even_on_draft_save_but_allows_counter_cash(self):
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            "Phone" if dt == "Mode of Payment" and name == "Online Mobile Money" and field == "type" else 0
        )
        check = Check(
            "LKG-00122",
            grand_total=5.90,
            rounded_total=5.90,
            payments=[PaymentRow(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72, custom_tender_received=0)],
        )
        with self.assertRaisesRegex(Refused, "not been confirmed"):
            self._settle(check, [{"mode_of_payment": "Online Mobile Money", "amount": 5.90}], submit_invoice=0)
        self.assertFalse([e for e in check.events if e[0] == "save"])

        # Counter Cash on the same abandoned self-order check succeeds cleanly.
        self._settle(check, [{"mode_of_payment": "Cash", "amount": 5.90}], submit_invoice=0)
        self.assertEqual([(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments], [("Cash", 5.90)])

    def test_make_invoice_preserves_confirmed_paid_phone_request_and_allows_balance_in_cash(self):
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            "Phone" if dt == "Mode of Payment" and name == "P2 Phone" and field == "type" else 0
        )
        self.frappe.get_all.side_effect = lambda doctype, filters=None, **kw: (
            [
                Row(
                    name="PR-PAID-9",
                    mode_of_payment="P2 Phone",
                    payment_account="MoMo - LH",
                    grand_total=100.0,
                    outstanding_amount=0.0,
                    status="Paid",
                )
            ]
            if doctype == "Payment Request"
            else []
        )
        check = Check(
            "LKG-PHONE-PAID",
            grand_total=200.0,
            rounded_total=200.0,
            payments=[PaymentRow(mode_of_payment="P2 Phone", type="Phone", amount=100.0, custom_tender_received=0)],
        )
        # Dropping the confirmed Phone payment is refused.
        with self.assertRaisesRegex(Refused, "P2 Phone"):
            self._settle(check, [{"mode_of_payment": "Cash", "amount": 200.0}], submit_invoice=0)

        # Settling confirmed Phone + Cash balance preserves reference_no and custom_tender_received=1.
        self._settle(
            check,
            [
                {"mode_of_payment": "P2 Phone", "amount": 100.0},
                {"mode_of_payment": "Cash", "amount": 100.0},
            ],
            submit_invoice=0,
        )
        self.assertEqual(
            [
                (r.mode_of_payment, round(float(r.amount), 2), r.get("reference_no"), int(r.get("custom_tender_received") or 0))
                for r in check.payments
            ],
            [("P2 Phone", 100.0, "PR-PAID-9", 1), ("Cash", 100.0, None, 0)],
        )

    def test_cancelled_or_failed_phone_request_releases_deadlock_and_allows_cash_settlement(self):
        """P1 #1: even if a draft Phone payment row still carries custom_tender_received=1
        in DB after its Payment Request was cancelled, failed, expired, or refunded,
        _received_payment_rows must re-check live Payment Request status so Cash works."""
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            "Phone" if dt == "Mode of Payment" and name == "Online Mobile Money" and field == "type" else 0
        )
        # Live Paid Payment Request list is empty (PR-DEAD was cancelled/failed/expired/refunded).
        self.frappe.get_all.side_effect = lambda doctype, filters=None, **kw: []
        check = Check(
            "LKG-DEADLOCK",
            grand_total=5.90,
            rounded_total=5.90,
            payments=[
                PaymentRow(
                    mode_of_payment="Online Mobile Money",
                    type="Phone",
                    amount=5.90,
                    reference_no="PR-DEAD",
                    custom_tender_received=1,
                )
            ],
        )
        self.assertEqual(self.order._received_payment_rows(check), [])
        self._settle(check, [{"mode_of_payment": "Cash", "amount": 5.90}], submit_invoice=0)
        self.assertEqual(
            [(r.mode_of_payment, round(float(r.amount), 2)) for r in check.payments],
            [("Cash", 5.90)],
        )

    def test_clearance_date_only_received_row_clears_clearance_on_surplus_remainder(self):
        check = Check(
            "LKG-CLEARANCE",
            grand_total=15.0,
            rounded_total=15.0,
            payments=[
                PaymentRow(
                    mode_of_payment="Card",
                    amount=10.0,
                    clearance_date="2026-11-24",
                    custom_tender_received=1,
                )
            ],
        )
        self._settle(
            check,
            [{"mode_of_payment": "Card", "amount": 15.0, "clearance_date": "2026-11-24"}],
            submit_invoice=0,
        )
        self.assertEqual(len(check.payments), 2)
        self.assertEqual(
            (check.payments[0].mode_of_payment, round(float(check.payments[0].amount), 2), check.payments[0].get("clearance_date")),
            ("Card", 10.0, "2026-11-24"),
        )
        self.assertEqual(
            (check.payments[1].mode_of_payment, round(float(check.payments[1].amount), 2), check.payments[1].get("clearance_date")),
            ("Card", 5.0, None),
        )

    def test_preview_settlement_totals_at_zero_clears_saved_percentage_but_keeps_carried_fixed(self):
        saved_pct = Check(
            "CHK-PCT",
            additional_discount_percentage=10,
            discount_amount=3.54,
            grand_total=31.86,
            rounded_total=31.86,
        )
        carried_fixed = Check(
            "CHK-FIXED",
            additional_discount_percentage=0,
            discount_amount=0.59,
            grand_total=23.01,
            rounded_total=23.01,
        )

        def calc(bill, gross):
            def _calc():
                disc = float(bill.discount_amount or 0)
                if bill.additional_discount_percentage:
                    disc = round(gross * bill.additional_discount_percentage / 100.0, 2)
                    bill.discount_amount = disc
                bill.grand_total = bill.rounded_total = round(gross - disc, 2)
            return _calc

        saved_pct.calculate_taxes_and_totals = calc(saved_pct, 35.40)
        carried_fixed.calculate_taxes_and_totals = calc(carried_fixed, 23.60)
        self.frappe.get_cached_doc.return_value = Row(
            name="Lakeside Till", company="Lakeside Hotel", currency="USD", allow_discount_change=1
        )
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.session = SimpleNamespace(user="cashier")
        self.frappe.db.exists.return_value = True
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: f in ("allow_discount_change",))
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: (
            self.frappe.get_cached_doc.return_value if doctype == "POS Profile" else {
                "CHK-PCT": saved_pct,
                "CHK-FIXED": carried_fixed,
            }[name]
        )
        self.enterContext(patch.object(self.order, "_enforce_order_access"))

        p_pct = self.order.preview_settlement_totals("CHK-PCT", "Lakeside Till", clear_discount=1)
        self.assertEqual((p_pct["grand_total"], p_pct["rounded_total"]), (35.40, 35.40))
        p_fixed = self.order.preview_settlement_totals("CHK-FIXED", "Lakeside Till")
        self.assertEqual((p_fixed["grand_total"], p_fixed["rounded_total"]), (23.01, 23.01))

    def test_preserves_frappe_dict_style_payment_rows_without_callable_as_dict(self):
        class FrappeStyleDict(dict):
            def __getattr__(self, key):
                return self.get(key)

        check = Check(
            "LKG-DICT",
            grand_total=100.0,
            rounded_total=100.0,
            payments=[FrappeStyleDict({"mode_of_payment": "Cash", "amount": 40.0, "custom_tender_received": 1})],
        )
        self._settle(
            check,
            [
                {"mode_of_payment": "Cash", "amount": 40.0},
                {"mode_of_payment": "Mobile Money", "amount": 60.0},
            ],
        )
        self.assertEqual(
            [(r.mode_of_payment, r.amount) for r in check.payments],
            [("Cash", 40.0), ("Mobile Money", 60.0)],
        )


class TestSplitBillReceivedTenders(Loaded):
    """R4: split_bill never loses or duplicates a row marked custom_tender_received=1."""

    def setUp(self):
        super().setUp()
        self.frappe.has_permission.return_value = True
        self.frappe.generate_hash.return_value = "SPLITGRP99"
        self.enterContext(patch.object(self.order, "getBranch", return_value="Lakeside"))
        self.enterContext(patch.object(self.order, "_enforce_order_access"))
        self.enterContext(patch.object(self.order, "_split_kots_to_move", return_value=[]))

    def _split_check(self, name, items, payments=()):
        check = Check(name, items=items, payments=list(payments))
        check.set_missing_values = lambda *a, **k: None
        check.run_method = lambda *a, **k: None
        check.insert = lambda *a, **k: check.events.append(("insert",))
        check.reload = lambda *a, **k: None
        check.remove = lambda item: check.items.remove(item)

        def calc():
            total = sum(float(i.rate) * float(i.qty) for i in check.items)
            check.grand_total = check.rounded_total = total

        check.calculate_taxes_and_totals = calc
        calc()
        return check

    def test_split_bill_keeps_received_tender_on_source_without_duplicating_on_child(self):
        source = self._split_check(
            "CHK-SRC",
            items=[
                dish("i1", "fish", 1, 10.0, "k1"),
                dish("i2", "steak", 1, 10.0, "k2"),
            ],
            payments=[PaymentRow(mode_of_payment="Cash", amount=10.0, custom_tender_received=1)],
        )
        child = self._split_check("CHK-CHILD", items=[], payments=[])
        self.frappe.get_doc.return_value = source
        self.frappe.new_doc.return_value = child

        self.order.split_bill("CHK-SRC", [{"name": "i2", "qty": 1}])

        self.assertEqual(
            [
                (r.get("mode_of_payment"), round(float(r.get("amount")), 2), int(r.get("custom_tender_received") or 0))
                for r in source.payments
            ],
            [("Cash", 10.0, 1)],
        )
        self.assertEqual(
            [
                (r.get("mode_of_payment"), round(float(r.get("amount")), 2), int(r.get("custom_tender_received") or 0))
                for r in child.payments
            ],
            [("Cash", 0.0, 0)],
        )

    def test_split_bill_refuses_reducing_source_below_received_tenders(self):
        source = self._split_check(
            "CHK-SRC",
            items=[
                dish("i1", "fish", 1, 10.0, "k1"),
                dish("i2", "steak", 1, 10.0, "k2"),
            ],
            payments=[PaymentRow(mode_of_payment="Cash", amount=15.0, custom_tender_received=1)],
        )
        child = self._split_check("CHK-CHILD", items=[], payments=[])
        self.frappe.get_doc.return_value = source
        self.frappe.new_doc.return_value = child

        with self.assertRaisesRegex(Refused, "less than tenders already received"):
            self.order.split_bill("CHK-SRC", [{"name": "i2", "qty": 1}])
        self.assertFalse([e for e in child.events if e[0] == "insert"])
        self.assertFalse([e for e in source.events if e[0] == "save"])


if __name__ == "__main__":
    unittest.main()
