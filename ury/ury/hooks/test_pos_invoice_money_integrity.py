"""POS Invoice validate/submit hooks that decide what the guest is charged; site-less (frappe double, no bench).

Pinned here:

1. Price list (G41). The check keeps the price list its lines were priced from. Resolution mirrors
   how sync_order prices a line: an aggregator check uses its Aggregator Settings price list; a
   table check uses its room's menu when the restaurant prices by room, else the active menu; a
   counter check uses its order type's menu when the restaurant prices by order type, else the
   active menu. The menu's enabled Price List is the check's price list. Simphony prices each
   revenue center or order type from its own menu level; one later branch must never overwrite it.
2. Printed-check lines (G8). Lines are compared by line identity (the stable reservation_line_key,
   else the row), never by item code, so two lines of one item are two lines. A new document
   (including a native POS return) has no stored copy to compare against.
3. Legacy linked bills (G12). Bills linked before Add Check moved lines onto one check still
   settle as two invoices. Submitting the primary must not rewrite the partner's own tenders, a
   partner that cannot post must stop the primary posting, and a partner that already posted is
   not saved again.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[1]  # ury/ury
HOOKS = APP / "hooks/ury_pos_invoice.py"


class Row(dict):
    __getattr__ = dict.get


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Refused(Exception):
    pass


class DoesNotExist(Exception):
    pass


def _framework():
    framework = MagicMock()
    framework.ValidationError = Refused
    framework.DoesNotExistError = DoesNotExist
    framework._ = lambda message, *args: message
    framework.flags = SimpleNamespace()

    def throw(message, exception=None, **kwargs):
        raise (exception or Refused)(message)

    framework.throw = throw
    framework.get_traceback = lambda *args, **kwargs: "traceback"
    utils = _stub(
        "frappe.utils",
        flt=lambda value, precision=None: float(value or 0),
        now_datetime=MagicMock(), get_time=MagicMock(), now=MagicMock(), getdate=MagicMock(),
        get_first_day=MagicMock(), get_last_day=MagicMock(), get_datetime=MagicMock(),
    )
    return framework, {
        "frappe": framework,
        "frappe.utils": utils,
        "ury.ury.doctype.ury_order.ury_order": _stub(
            "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()),
        "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy", get_applicable_policy=MagicMock()),
    }


class Loaded(unittest.TestCase):
    def setUp(self):
        self.frappe, modules = _framework()
        self.enterContext(patch.dict(sys.modules, modules))
        spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_pos_invoice", HOOKS)
        self.hooks = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_pos_invoice": self.hooks}))
        spec.loader.exec_module(self.hooks)


class Records:
    """Configuration records the price-list resolution reads, keyed as frappe.db.get_value is called."""

    def __init__(self):
        self.restaurant = {"active_menu": "Main Menu", "room_wise_menu": 0, "order_type_wise_menu": 0}
        self.table_rooms = {"Pool 1": "Pool Bar", "Hall 1": "Hall"}
        self.room_menus = {}
        self.order_type_menus = {}
        self.price_lists = {"Main Menu": "Main Prices", "Pool Menu": "Pool Prices", "Pickup Menu": "Pickup Prices"}
        self.aggregator_price_lists = {}

    def get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "URY Restaurant":
            return self.restaurant.get(fieldname)
        if doctype == "URY Table":
            assert fieldname == "restaurant_room", fieldname
            return self.table_rooms.get(filters)
        if doctype == "Menu for Room":
            return self.room_menus.get(filters["room"])
        if doctype == "Order Type Menu":
            return self.order_type_menus.get(filters["order_type"])
        if doctype == "Price List":
            assert filters.get("enabled") == 1, filters
            return self.price_lists.get(filters.get("restaurant_menu"))
        if doctype == "Aggregator Settings":
            assert fieldname == "price_list", fieldname
            return self.aggregator_price_lists.get(filters["customer"])
        raise AssertionError(f"unexpected read {doctype} {filters} {fieldname}")


class TestPriceListResolution(Loaded):
    """G41: the check keeps the price list of the menu it was rung on."""

    def setUp(self):
        super().setUp()
        self.records = Records()
        self.frappe.db.get_value.side_effect = self.records.get_value

    def check(self, order_type="Dine In", table=None, customer="Walk-in"):
        return SimpleNamespace(
            restaurant="Lakeside", restaurant_table=table, order_type=order_type, customer=customer,
            branch="Lakeside Branch", selling_price_list="Rung Prices",
        )

    def resolve(self, doc):
        self.hooks.validate_price_list(doc, "validate")
        return doc.selling_price_list

    def test_a_table_in_a_room_priced_restaurant_keeps_its_rooms_price_list(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.assertEqual(self.resolve(self.check(table="Pool 1")), "Pool Prices")

    def test_a_counter_check_in_an_order_type_priced_restaurant_keeps_its_order_types_price_list(self):
        self.records.restaurant["order_type_wise_menu"] = 1
        self.records.order_type_menus["Take Away"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(order_type="Take Away")), "Pickup Prices")

    def test_a_table_check_ignores_the_order_type_menu(self):
        """sync_order prices a table check from its room or the active menu, never the order-type menu."""
        self.records.restaurant["order_type_wise_menu"] = 1
        self.records.order_type_menus["Dine In"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(table="Hall 1")), "Main Prices")

    def test_a_table_without_room_pricing_uses_the_active_menu(self):
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.assertEqual(self.resolve(self.check(table="Pool 1")), "Main Prices")

    def test_a_room_without_its_own_menu_uses_the_active_menu(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.assertEqual(self.resolve(self.check(table="Hall 1")), "Main Prices")

    def test_a_counter_check_without_order_type_pricing_uses_the_active_menu(self):
        self.records.order_type_menus["Take Away"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(order_type="Take Away")), "Main Prices")

    def test_an_aggregator_check_uses_the_aggregators_price_list(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.records.aggregator_price_lists["Delivery Partner"] = "Partner Prices"
        doc = self.check(order_type="Aggregators", customer="Delivery Partner")
        self.assertEqual(self.resolve(doc), "Partner Prices")

    def test_an_aggregator_without_a_price_list_is_refused(self):
        with self.assertRaises(Refused):
            self.resolve(self.check(order_type="Aggregators", customer="Unknown Partner"))

    def test_a_check_without_a_restaurant_keeps_its_price_list(self):
        doc = self.check()
        doc.restaurant = None
        self.assertEqual(self.resolve(doc), "Rung Prices")


class Check:
    """A POS Invoice double: header fields as attributes, child rows as Row dicts."""

    def __init__(self, name="CHK-1", items=(), new=False, **fields):
        self.name = name
        self.items = list(items)
        self.payments = []
        self.flags = SimpleNamespace()
        self.waiter = "waiter@example.com"
        self.pos_profile = "Lakeside Till"
        self.invoice_printed = 1
        self.is_return = 0
        self.docstatus = 0
        self._new = new
        self.saved = 0
        self.submitted = 0
        self.__dict__.update(fields)

    def is_new(self):
        return self._new

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, row):
        getattr(self, key).append(Row(row))

    def save(self, **kwargs):
        self.saved += 1

    def submit(self):
        self.submitted += 1
        self.docstatus = 1


def line(name, item_code, qty, key=None):
    return Row(name=name, item_code=item_code, item_name=item_code.title(), qty=qty, reservation_line_key=key)


class TestPrintedCheckLineIdentity(Loaded):
    """G8: a printed check refuses a removed or reduced line, judged line by line."""

    def setUp(self):
        super().setUp()
        self.frappe.db.get_value.side_effect = lambda doctype, name, field, *a, **k: (
            0 if (doctype, field) == ("POS Profile", "remove_items") else None)
        self.stored = None
        self.frappe.get_doc.side_effect = self.load

    def load(self, doctype, name, *args, **kwargs):
        if self.stored is None:
            raise DoesNotExist(f"{doctype} {name} not found")
        return self.stored

    def validate(self, doc):
        self.hooks.validate_invoice(doc, "validate")

    def test_removing_one_of_two_lines_of_one_item_is_refused(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "soda", 1, "k2")])
        with self.assertRaisesRegex(Refused, "Removed items"):
            self.validate(Check(items=[line("r1", "soda", 2, "k1")]))

    def test_reducing_one_of_two_lines_of_one_item_is_refused(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "soda", 1, "k2")])
        with self.assertRaisesRegex(Refused, "qty reduced from 2"):
            self.validate(Check(items=[line("r1", "soda", 1, "k1"), line("r2", "soda", 1, "k2")]))

    def test_adding_a_line_to_a_printed_check_is_allowed(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1")])
        self.validate(Check(items=[line("r1", "soda", 2, "k1"), line("r3", "soda", 1, "k3")]))

    def test_a_line_resaved_under_a_new_row_keeps_its_identity(self):
        """sync_order rebuilds rows; the stable line key, not the row name, is the line."""
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "fish", 1, "k2")])
        self.validate(Check(items=[line("r8", "soda", 2, "k1"), line("r9", "fish", 1, "k2")]))

    def test_a_line_without_a_stable_key_is_matched_by_its_row(self):
        self.stored = Check(items=[line("r1", "soda", 1), line("r2", "soda", 1)])
        with self.assertRaisesRegex(Refused, "Removed items"):
            self.validate(Check(items=[line("r1", "soda", 1)]))

    def test_a_new_native_return_is_not_compared_with_a_stored_copy(self):
        """A return copies invoice_printed from the paid check but is a new document with no stored copy."""
        self.validate(Check(name="RET-1", new=True, is_return=1, items=[line("r1", "soda", -1, "k1")]))
        self.frappe.get_doc.assert_not_called()

    def test_a_new_document_is_not_compared_with_a_stored_copy(self):
        self.validate(Check(name="CHK-NEW", new=True, items=[line("r1", "soda", 1, "k1")]))
        self.frappe.get_doc.assert_not_called()

    def test_a_saved_draft_return_is_not_held_to_the_printed_check_rule(self):
        """A return refunds a paid check; correcting its draft quantity is not editing a printed bill."""
        self.stored = Check(name="RET-1", is_return=1, items=[line("r1", "soda", -1, "k1")])
        self.validate(Check(name="RET-1", is_return=1, items=[line("r1", "soda", -2, "k1")]))


class TestLegacyLinkedBillSync(Loaded):
    """G12 for bills linked before Add Check consolidated lines: both post exactly once with their own tenders."""

    def setUp(self):
        super().setUp()
        self.frappe.db.exists.return_value = True
        self.partner = Check(name="CHK-2", invoice_printed=0, rounded_total=80, paid_amount=80)
        self.partner.payments = [Row(mode_of_payment="Card", amount=80)]
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.partner
        self.primary = Check(name="CHK-1", custom_merged_pos_invoice="CHK-2", docstatus=1, paid_amount=150)
        self.primary.payments = [Row(mode_of_payment="Cash", amount=100), Row(mode_of_payment="Card", amount=50)]

    def test_submitting_the_primary_keeps_the_partners_own_tenders(self):
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.submitted, 1)
        self.assertEqual(
            [(row.mode_of_payment, row.amount) for row in self.partner.payments], [("Card", 80)])
        self.assertEqual(self.partner.paid_amount, 80)

    def test_a_partner_that_cannot_post_stops_the_primary_posting(self):
        def refuse():
            raise Refused("Partner refused")

        self.partner.submit = refuse
        with self.assertRaisesRegex(Refused, "Partner refused"):
            self.hooks.sync_merged_invoice(self.primary)

    def test_a_partner_that_already_posted_is_not_saved_again(self):
        self.partner.docstatus = 1
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.saved, 0)
        self.assertEqual(self.partner.submitted, 0)

    def test_a_draft_save_still_mirrors_the_printed_flag(self):
        self.primary.docstatus = 0
        self.primary.invoice_printed = 1
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.invoice_printed, 1)
        self.assertEqual(self.partner.saved, 1)


class TestReturnCopySafety(unittest.TestCase):
    """P1: native returns of consolidated checks never auto-populate merge/split links."""

    REPO_URY = Path(__file__).resolve().parents[2]  # ury/
    EXPECTED_NO_COPY = (
        "POS Invoice-custom_merged_pos_invoice",
        "POS Invoice-custom_split_from",
        "POS Invoice-custom_merged_total",
    )

    def _custom_fields(self):
        import json

        path = self.REPO_URY / "fixtures" / "custom_field.json"
        return {row["name"]: row for row in json.loads(path.read_text(encoding="utf-8"))}

    def test_merge_and_split_custom_fields_mark_no_copy_so_native_returns_stay_clean(self):
        fields = self._custom_fields()
        for name in self.EXPECTED_NO_COPY:
            self.assertIn(name, fields, f"{name} missing from custom_field.json")
            self.assertEqual(
                fields[name].get("no_copy"),
                1,
                f"{name} has no_copy={fields[name].get('no_copy')!r}; Frappe map_fields copies it onto returns",
            )

    def test_native_return_mapper_leaves_merge_and_split_links_empty(self):
        """Frappe's make_return_doc -> map_fields auto-populates any POS Invoice Link field
        pointing to POS Invoice when no_copy is 0, corrupting Return tab totals and tripping
        [HOSP-POS-RETURN-MERGED]."""
        fields = self._custom_fields()
        auto_populated = sorted(
            row["fieldname"]
            for row in fields.values()
            if row.get("dt") == "POS Invoice"
            and row.get("fieldtype") == "Link"
            and row.get("options") == "POS Invoice"
            and not row.get("no_copy")
        )
        self.assertEqual(auto_populated, [])

    def test_existing_site_patch_sets_no_copy_and_is_registered(self):
        patch_entry = "ury.patches.v2_0.no_copy_merge_split_fields"
        patches_txt = (self.REPO_URY / "patches.txt").read_text(encoding="utf-8").splitlines()
        registered = [line.split("#", 1)[0].strip() for line in patches_txt]
        self.assertIn(patch_entry, registered, "no_copy patch must be registered in ury/patches.txt")

        patch_file = self.REPO_URY / "patches" / "v2_0" / "no_copy_merge_split_fields.py"
        self.assertTrue(patch_file.exists(), f"missing patch file {patch_file}")

        framework, modules = _framework()
        state = {name: 0 for name in self.EXPECTED_NO_COPY}
        framework.db.exists.side_effect = lambda dt, name: dt == "Custom Field" and name in state
        framework.db.get_value.side_effect = lambda dt, name, field, *a, **k: (
            state[name] if dt == "Custom Field" and field == "no_copy" else None
        )

        def set_val(dt, name, field, value, *args, **kwargs):
            if dt == "Custom Field" and field == "no_copy":
                state[name] = value

        framework.db.set_value.side_effect = set_val

        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(patch_entry, patch_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.execute()

        self.assertEqual(state, {name: 1 for name in self.EXPECTED_NO_COPY})
        framework.clear_cache.assert_called_with(doctype="POS Invoice")


class TestTenderReceivedFieldAndPatch(unittest.TestCase):
    """R4: explicit server-only custom_tender_received field on Sales Invoice Payment."""

    REPO_URY = Path(__file__).resolve().parents[2]  # ury/
    FIELD_NAME = "Sales Invoice Payment-custom_tender_received"

    def _custom_fields(self):
        import json

        path = self.REPO_URY / "fixtures" / "custom_field.json"
        return {row["name"]: row for row in json.loads(path.read_text(encoding="utf-8"))}

    def test_custom_field_fixture_hooks_and_setup_define_server_only_no_copy_tender_received(self):
        fields = self._custom_fields()
        self.assertIn(self.FIELD_NAME, fields, f"{self.FIELD_NAME} missing from custom_field.json")
        row = fields[self.FIELD_NAME]
        self.assertEqual(row.get("dt"), "Sales Invoice Payment")
        self.assertEqual(row.get("fieldname"), "custom_tender_received")
        self.assertEqual(row.get("fieldtype"), "Check")
        self.assertEqual(str(row.get("default")), "0")
        self.assertEqual(row.get("no_copy"), 1)
        self.assertEqual(row.get("read_only"), 1)
        self.assertEqual(row.get("print_hide"), 1)

        hooks_text = (self.REPO_URY / "hooks.py").read_text(encoding="utf-8")
        self.assertIn(f'"{self.FIELD_NAME}"', hooks_text)
        self.assertIn("mark_phone_payment_received", hooks_text)

        setup_text = (self.REPO_URY / "setup_customizations.py").read_text(encoding="utf-8")
        self.assertIn('"Sales Invoice Payment"', setup_text)
        self.assertIn('"custom_tender_received"', setup_text)

    def test_existing_site_patch_installs_field_idempotently_without_backfilling_existing_rows(self):
        patch_entry = "ury.patches.v2_0.add_sales_invoice_payment_tender_received"
        patches_txt = (self.REPO_URY / "patches.txt").read_text(encoding="utf-8").splitlines()
        registered = [line.split("#", 1)[0].strip() for line in patches_txt]
        self.assertIn(patch_entry, registered, "tender_received patch must be registered in ury/patches.txt")

        patch_file = self.REPO_URY / "patches" / "v2_0" / "add_sales_invoice_payment_tender_received.py"
        self.assertTrue(patch_file.exists(), f"missing patch file {patch_file}")

        framework, modules = _framework()
        created = []
        stored = {}

        def fake_create(definitions, **kw):
            created.append(definitions)
            for dt, field_defs in definitions.items():
                for fd in field_defs:
                    stored[f"{dt}-{fd['fieldname']}"] = dict(fd)

        framework.db.exists.side_effect = lambda dt, name: dt == "Custom Field" and name in stored
        framework.db.get_value.side_effect = lambda dt, name, fields, as_dict=False, *a, **k: (
            Row(stored[name]) if dt == "Custom Field" and name in stored else None
        )
        modules["frappe.custom.doctype.custom_field.custom_field"] = _stub(
            "frappe.custom.doctype.custom_field.custom_field",
            create_custom_fields=fake_create,
        )
        modules["ury.setup_customizations"] = _stub(
            "ury.setup_customizations",
            get_custom_fields=lambda: {
                "Sales Invoice Payment": [
                    {
                        "fieldname": "custom_tender_received",
                        "fieldtype": "Check",
                        "label": "Tender Received",
                        "default": "0",
                        "no_copy": 1,
                        "read_only": 1,
                        "print_hide": 1,
                        "insert_after": "reference_no",
                    }
                ]
            },
        )

        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(patch_entry, patch_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.execute()
            self.assertEqual(len(created), 1)
            self.assertIn(self.FIELD_NAME, stored)
            self.assertEqual(stored[self.FIELD_NAME]["no_copy"], 1)
            self.assertEqual(stored[self.FIELD_NAME]["read_only"], 1)
            self.assertEqual(str(stored[self.FIELD_NAME]["default"]), "0")
            # Second run must be a no-op (idempotent) and never backfill existing rows via SQL.
            mod.execute()
            self.assertEqual(len(created), 1)
            framework.db.sql.assert_not_called()


class TestConfirmedPhonePaymentsAndServerOnlyFlag(Loaded):
    """R4: unconfirmed Phone Payment Requests never submit as paid, and clients cannot
    forge custom_tender_received=1 on draft payment rows."""

    def setUp(self):
        super().setUp()
        self.frappe.session = SimpleNamespace(user="cashier@example.com")
        self.frappe.utils = _stub(
            "frappe.utils",
            cint=lambda value: int(value or 0),
            flt=lambda value, precision=None: round(float(value or 0), precision) if precision is not None else float(value or 0),
        )
        self.phone_modes = {"Online Mobile Money": "Phone", "Cash": "Cash", "Mobile Money": "Bank"}
        self.paid_requests = []
        self.db_received_rows = []
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            self.phone_modes.get(name) if dt == "Mode of Payment" and field == "type" else None
        )
        self.frappe.get_all.side_effect = self._get_all

    def _get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        if doctype == "Payment Request":
            mode = (filters or {}).get("mode_of_payment")
            return [
                pr for pr in self.paid_requests
                if (not mode or pr.get("mode_of_payment") == mode)
            ]
        if doctype == "Sales Invoice Payment":
            return list(self.db_received_rows)
        return []

    def test_refuses_submitting_unconfirmed_phone_payment_even_when_split_across_rows(self):
        fn = getattr(self.hooks, "validate_confirmed_phone_payments", None)
        self.assertTrue(callable(fn), "validate_confirmed_phone_payments missing from ury_pos_invoice.py")
        doc = Check(
            name="LKG-00122",
            payments=[
                Row(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72),
                Row(mode_of_payment="Online Mobile Money", type="Phone", amount=1.18),
            ],
        )
        with self.assertRaisesRegex(Refused, "not been confirmed"):
            fn(doc, "before_submit")

    def test_refuses_submitting_phone_payment_when_amount_mismatches_confirmed_request(self):
        fn = getattr(self.hooks, "validate_confirmed_phone_payments", None)
        self.assertTrue(callable(fn), "validate_confirmed_phone_payments missing from ury_pos_invoice.py")
        self.paid_requests = [
            Row(name="PR-1", mode_of_payment="Online Mobile Money", grand_total=4.72, outstanding_amount=0.0)
        ]
        doc = Check(
            name="LKG-00122",
            payments=[Row(mode_of_payment="Online Mobile Money", type="Phone", amount=5.90)],
        )
        with self.assertRaisesRegex(Refused, "Confirmed payment"):
            fn(doc, "before_submit")

    def test_allows_submitting_confirmed_phone_payment_or_counter_cash_fallback(self):
        fn = getattr(self.hooks, "validate_confirmed_phone_payments", None)
        self.assertTrue(callable(fn), "validate_confirmed_phone_payments missing from ury_pos_invoice.py")
        # Counter Cash on an invoice with no Paid Payment Request passes cleanly.
        cash_doc = Check(name="LKG-00122", payments=[Row(mode_of_payment="Cash", type="Cash", amount=5.90)])
        fn(cash_doc, "before_submit")

        # Confirmed Phone payment + Cash balance passes cleanly.
        self.paid_requests = [
            Row(name="PR-1", mode_of_payment="Online Mobile Money", grand_total=4.72, outstanding_amount=0.0)
        ]
        confirmed_doc = Check(
            name="LKG-00122",
            payments=[
                Row(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72),
                Row(mode_of_payment="Cash", type="Cash", amount=1.18),
            ],
        )
        fn(confirmed_doc, "before_submit")

    def test_server_only_flag_blocks_client_forging_custom_tender_received(self):
        fn = getattr(self.hooks, "validate_server_only_tender_received", None)
        self.assertTrue(callable(fn), "validate_server_only_tender_received missing from ury_pos_invoice.py")
        forged = Check(
            name="LKG-FORGE",
            payments=[Row(name="sip-1", mode_of_payment="Cash", amount=5.90, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "confirmed server payment"):
            fn(forged, "validate")

        # Unchanged already-received DB row is allowed on ordinary saves (e.g. adding items).
        self.db_received_rows = [
            Row(name="sip-1", mode_of_payment="Cash", amount=5.90, reference_no=None)
        ]
        fn(forged, "validate")

        # Altering an existing received row's amount via client save is refused.
        altered = Check(
            name="LKG-FORGE",
            payments=[Row(name="sip-1", mode_of_payment="Cash", amount=50.0, custom_tender_received=1)],
        )
        with self.assertRaisesRegex(Refused, "confirmed server payment"):
            fn(altered, "validate")

        # Trusted server context may write custom_tender_received=1.
        self.db_received_rows = []
        self.frappe.flags.ury_server_tender_received = True
        try:
            fn(forged, "validate")
        finally:
            self.frappe.flags.ury_server_tender_received = False

    def test_server_only_guard_blocks_client_dropping_reducing_remoding_or_replacing_received_row(self):
        """P1 #2: validate_server_only_tender_received must compare DB rows first so a raw
        REST PUT with custom_tender_received=0 (or empty payments) cannot drop, reduce,
        re-mode, or replace an already-received tender."""
        fn = self.hooks.validate_server_only_tender_received
        self.db_received_rows = [
            Row(name="sip-1", mode_of_payment="Cash", amount=5.90, reference_no=None, type="Cash")
        ]

        for label, payments in (
            ("drop_all", []),
            ("unflag_same_row", [Row(name="sip-1", mode_of_payment="Cash", amount=5.90, custom_tender_received=0)]),
            ("reduce_amount", [Row(name="sip-1", mode_of_payment="Cash", amount=2.00, custom_tender_received=0)]),
            ("remode", [Row(name="sip-1", mode_of_payment="Card", amount=5.90, custom_tender_received=0)]),
            ("replace_row", [Row(name="sip-2", mode_of_payment="Cash", amount=5.90, custom_tender_received=0)]),
        ):
            with self.subTest(case=label):
                doc = Check(name="LKG-00088", payments=payments)
                with self.assertRaisesRegex(Refused, "received"):
                    fn(doc, "validate")

        # Even Administrator over an HTTP request cannot drop a received row without the server flag.
        self.frappe.session = SimpleNamespace(user="Administrator")
        self.frappe.request = SimpleNamespace(method="PUT")
        try:
            with self.assertRaisesRegex(Refused, "received"):
                fn(Check(name="LKG-00088", payments=[]), "validate")
        finally:
            self.frappe.request = None
            self.frappe.session = SimpleNamespace(user="cashier@example.com")

        # Server return / cancel / explicit server flag flows remain allowed.
        fn(Check(name="LKG-RET", is_return=1, payments=[]), "validate")
        fn(Check(name="LKG-CAN", docstatus=2, payments=[]), "validate")
        self.frappe.flags.ury_server_tender_received = True
        try:
            fn(Check(name="LKG-00088", payments=[]), "validate")
        finally:
            self.frappe.flags.ury_server_tender_received = False

        # A Phone row whose Payment Request is no longer live Paid/Partially Paid may be replaced.
        self.db_received_rows = [
            Row(
                name="sip-phone",
                mode_of_payment="Online Mobile Money",
                amount=5.90,
                reference_no="PR-CANCELLED",
                type="Phone",
            )
        ]
        self.paid_requests = []
        fn(
            Check(
                name="LKG-00089",
                payments=[Row(name="sip-cash", mode_of_payment="Cash", amount=5.90, custom_tender_received=0)],
            ),
            "validate",
        )

    def test_server_only_guard_blocks_administrator_background_and_data_import_writes(self):
        """R6 P1 #3: Administrator with frappe.request=None (e.g. background Data Import)
        must NEVER bypass validate_server_only_tender_received. Only the server's own
        ury_server_tender_received flag (outside Data Import) may modify received rows."""
        fn = self.hooks.validate_server_only_tender_received
        self.db_received_rows = [
            Row(name="sip-1", mode_of_payment="Cash", amount=5.90, reference_no=None, type="Cash")
        ]
        self.frappe.session = SimpleNamespace(user="Administrator")
        self.frappe.request = None
        try:
            for label, in_import, server_flag in (
                ("admin_bg_no_flag", False, False),
                ("admin_data_import_no_flag", True, False),
                ("admin_data_import_even_with_flag", True, True),
            ):
                with self.subTest(case=label):
                    self.frappe.flags.in_import = in_import
                    self.frappe.flags.ury_server_tender_received = server_flag
                    # Attempting to unflag or drop an existing received tender must fail.
                    with self.assertRaisesRegex(Refused, "received"):
                        fn(
                            Check(
                                name="LKG-ADMIN-DI",
                                payments=[
                                    Row(name="sip-1", mode_of_payment="Cash", amount=5.90, custom_tender_received=0)
                                ],
                            ),
                            "validate",
                        )
                    # Attempting to forge custom_tender_received=1 on a new row must also fail.
                    with self.assertRaisesRegex(Refused, "confirmed server payment"):
                        fn(
                            Check(
                                name="LKG-ADMIN-DI",
                                payments=[
                                    Row(name="sip-1", mode_of_payment="Cash", amount=5.90, custom_tender_received=1),
                                    Row(name="sip-2", mode_of_payment="Cash", amount=10.0, custom_tender_received=1),
                                ],
                            ),
                            "validate",
                        )

            # Server-only internal flow (not Data Import) passes cleanly with the internal flag.
            self.frappe.flags.in_import = False
            self.frappe.flags.ury_server_tender_received = True
            fn(Check(name="LKG-ADMIN-SERVER", payments=[]), "validate")
        finally:
            self.frappe.flags.in_import = False
            self.frappe.flags.ury_server_tender_received = False
            self.frappe.request = None
            self.frappe.session = SimpleNamespace(user="cashier@example.com")

    def test_partially_refunded_phone_request_validates_against_live_paid_amount(self):
        """R6 P1 #2: Partially Refunded Phone Payment Requests must validate against live
        paid amount (grand_total - outstanding_amount) so cash balance settles cleanly."""
        fn = self.hooks.validate_confirmed_phone_payments

        def status_aware_get_all(doctype, filters=None, fields=None, **kw):
            if doctype == "Payment Request":
                f = filters or {}
                allowed = set(f["status"][1]) if isinstance(f.get("status"), list) else None
                mode = f.get("mode_of_payment")
                return [
                    pr
                    for pr in self.paid_requests
                    if (not mode or pr.get("mode_of_payment") == mode)
                    and (allowed is None or pr.get("status") in allowed)
                ]
            if doctype == "Sales Invoice Payment":
                return list(self.db_received_rows)
            return []

        self.frappe.get_all.side_effect = status_aware_get_all
        self.paid_requests = [
            Row(
                name="PR-PART-REF",
                mode_of_payment="Online Mobile Money",
                grand_total=5.90,
                outstanding_amount=2.00,
                status="Partially Refunded",
            )
        ]

        # Live paid 3.90 + Cash 2.00 must submit cleanly.
        fn(
            Check(
                name="LKG-PART-REF",
                payments=[
                    Row(mode_of_payment="Online Mobile Money", type="Phone", amount=3.90),
                    Row(mode_of_payment="Cash", type="Cash", amount=2.00),
                ],
            ),
            "before_submit",
        )

        # Stale full 5.90 must be refused because confirmed live paid is 3.90.
        with self.assertRaisesRegex(Refused, "Confirmed payment"):
            fn(
                Check(
                    name="LKG-PART-REF",
                    payments=[Row(mode_of_payment="Online Mobile Money", type="Phone", amount=5.90)],
                ),
                "before_submit",
            )

        # Fully refunded (outstanding == grand_total -> live paid 0.0) refuses Phone and allows Cash.
        self.paid_requests[0]["outstanding_amount"] = 5.90
        with self.assertRaisesRegex(Refused, "not been confirmed"):
            fn(
                Check(
                    name="LKG-PART-REF",
                    payments=[Row(mode_of_payment="Online Mobile Money", type="Phone", amount=5.90)],
                ),
                "before_submit",
            )
        fn(
            Check(
                name="LKG-PART-REF",
                payments=[Row(mode_of_payment="Cash", type="Cash", amount=5.90)],
            ),
            "before_submit",
        )


class TestOfflinePosUtilsMultiCompanyFiscalYear(unittest.TestCase):
    """P2: offline_pos_utils must ensure company URY belongs to an active Fiscal Year even
    when another company already owns the active Fiscal Year on a multi-company site."""

    def test_links_ury_company_to_existing_active_fiscal_year_on_multi_company_site(self):
        from datetime import date

        framework = _stub(
            "frappe",
            db=_stub("frappe.db"),
            get_all=MagicMock(),
            get_doc=MagicMock(),
        )
        utils_mod = _stub("frappe.utils", now_datetime=lambda: SimpleNamespace(date=lambda: date(2026, 11, 24)))
        saved = []

        class FakeFY:
            def __init__(self, name, companies):
                self.name = name
                self.companies = [SimpleNamespace(company=c) for c in companies]

            def append(self, field, row):
                self.companies.append(SimpleNamespace(**row))

            def save(self, **kw):
                saved.append((self.name, [c.company for c in self.companies]))

        fy_doc = FakeFY("2026", ["Lakeside Hospitality"])
        framework.get_all.side_effect = lambda dt, filters=None, pluck=None, **kw: (
            ["2026"]
            if dt == "Fiscal Year"
            else (["Lakeside Hospitality"] if dt == "Fiscal Year Company" else [])
        )
        framework.get_doc.return_value = fy_doc

        spec = importlib.util.spec_from_file_location(
            "offline_pos_utils_under_test",
            APP.parent / "tests" / "offline_pos_utils.py",
        )
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": framework, "frappe.utils": utils_mod}):
            spec.loader.exec_module(mod)
            ensure_fn = getattr(mod, "_ensure_company_fiscal_year", None)
            self.assertTrue(callable(ensure_fn), "_ensure_company_fiscal_year missing from offline_pos_utils.py")
            ensure_fn("URY", date(2026, 11, 24))
        self.assertEqual(saved, [("2026", ["Lakeside Hospitality", "URY"])])


if __name__ == "__main__":
    unittest.main()
