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
        get_system_timezone=lambda: "Africa/Kampala",
        now_datetime=lambda: None,
    )
    return framework, {
        "frappe": framework,
        "frappe.utils": utils,
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
    with patch.dict(sys.modules, modules):
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
                           custom_enable_discount=1, allow_discount_change=0)
        self.frappe.get_cached_doc.return_value = self.profile
        self.frappe.get_value.return_value = "Dine In"
        self.frappe.db.get_value.return_value = 0
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.primary = Check("CHK-1", custom_merged_pos_invoice="CHK-2", rounded_total=100, grand_total=100)
        self.partner = Check("CHK-2", custom_merged_pos_invoice="CHK-1", rounded_total=80, grand_total=80)
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.partner
        for helper in ("_validate_client_ids", "_lock_settlement_customer", "_client_order_invoice",
                       "_check_held_before_settlement", "_refuse_client_order_rebind", "_set_settlement_customer",
                       "_enforce_on_account"):
            self.enterContext(patch.object(self.order, helper, return_value=None))
        self.enterContext(patch.object(self.order, "_validate_additional_discount", return_value=10))
        self.enterContext(patch.object(self.order, "get_order_invoice", return_value=self.primary))

    def test_the_bill_discount_applies_to_the_linked_partner_before_it_is_totalled(self):
        result = self.order.make_invoice(
            customer="Walk-in", payments=[{"mode_of_payment": "Cash", "amount": 162}], cashier="cashier",
            pos_profile="Lakeside Till", owner="cashier", additionalDiscount=10, invoice="CHK-1", submit_invoice=0,
        )
        self.assertEqual(result["status"], "Draft")
        self.assertEqual(self.partner.additional_discount_percentage, 10)
        self.assertEqual([e for e in self.partner.events if e[0] == "totals"][0], ("totals", 10, 0))


class TestSettlementTotalsPreview(Loaded):
    """G17: the till shows the totals the native controller will charge, not its own rounding."""

    def setUp(self):
        super().setUp()
        self.check = Check("CHK-1")
        self.partner = Check("CHK-2")
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: {"CHK-1": self.check, "CHK-2": self.partner}[name]
        self.discount = self.enterContext(patch.object(self.order, "_validate_additional_discount", return_value=10))
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
        return preview(invoice="CHK-1", pos_profile="Lakeside Till", additionalDiscount=10, **kwargs)

    def test_returns_the_native_totals_at_the_validated_discount_without_saving(self):
        self.assertEqual(self.preview(), {"grand_total": 1110.5, "rounded_total": 1110})
        self.discount.assert_called_once_with(10, "Lakeside Till")
        self.assertEqual(self.check.events[-1], ("totals", 10, 0))
        self.assertFalse([e for e in self.check.events if e[0] == "save"])
        self.assertIn(("permission", "read"), self.check.events)
        self.access.assert_called_once_with(self.check)

    def test_a_linked_bill_previews_both_halves_at_the_one_discount(self):
        self.check.custom_merged_pos_invoice = "CHK-2"
        self.assertEqual(self.preview(), {"grand_total": 1560.75, "rounded_total": 1560})
        self.assertEqual(self.partner.events[-1], ("totals", 10, 0))

    def test_another_tills_check_is_refused(self):
        self.check.pos_profile = "Pool Bar Till"
        with self.assertRaises(NotPermitted):
            self.preview()


if __name__ == "__main__":
    unittest.main()
