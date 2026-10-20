"""Site-less regressions for waiter split authorization and POS table release."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch


class Row(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class Doc:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, field, value):
        getattr(self, field).append(Row(**value))

    def remove(self, row):
        self.items.remove(row)

    def set_missing_values(self):
        return None

    def run_method(self, method):
        return None

    def calculate_taxes_and_totals(self):
        return None

    def insert(self):
        self.name = getattr(self, "name", None) or "POS-INV-SPLIT"

    def reload(self):
        return self

    def save(self):
        return None

    def as_dict(self):
        return {
            key: value
            for key, value in vars(self).items()
            if not key.startswith("_")
        }


class FakePdfWriter:
    def write(self, stream):
        stream.write(b"pdf")


class TestSplitBillAndPrintRelease(unittest.TestCase):
    def setUp(self):
        self.framework = MagicMock()
        self.framework.PermissionError = PermissionError
        self.framework.ValidationError = ValueError
        self.framework._ = lambda *messages: messages[0]
        self.framework.whitelist = lambda *args, **kwargs: lambda function: function
        self.framework.session = SimpleNamespace(user="captain_a@example.com")
        self.framework.flags = SimpleNamespace(ury_bill_split=False)
        self.framework.db = MagicMock()
        self.framework.cache = MagicMock()
        self.framework.STANDARD_USERS = {"Administrator", "Guest"}
        self.framework.has_permission = MagicMock(return_value=True)
        self.framework.get_roles = MagicMock(return_value=["URY Captain"])
        self.framework.get_doc = MagicMock()
        self.framework.get_all = MagicMock()
        self.framework.get_value = MagicMock()
        self.framework.new_doc = MagicMock()
        self.framework.generate_hash = MagicMock(return_value="split-hash")
        self.framework.get_print = MagicMock()
        self.framework.publish_realtime = MagicMock()
        self.framework.log_error = MagicMock()

        def throw(message, exception=ValueError):
            raise exception(message)

        self.framework.throw = throw

        utils = ModuleType("frappe.utils")
        utils.cint = lambda value: int(value or 0)
        utils.flt = lambda value: float(value or 0)
        utils.get_datetime = lambda value: value
        utils.get_system_timezone = lambda: "Africa/Kampala"
        utils.now_datetime = lambda: None
        document = ModuleType("frappe.model.document")
        document.Document = object
        queries = ModuleType("erpnext.controllers.queries")
        queries.item_query = MagicMock()
        pos_api = ModuleType("ury.ury_pos.api")
        pos_api.getBranch = MagicMock(return_value="Branch A")
        pos_api.getBranchRoom = MagicMock(return_value="Main Hall")
        pos_api.getRoom = MagicMock(return_value=[{"name": "Main Hall"}])
        pos_api.posOpening = MagicMock(return_value=0)
        kot = ModuleType("ury.ury.api.ury_kot_generate")
        kot.kot_execute = MagicMock()
        kot.process_items_for_cancel_kot = MagicMock()
        printview = ModuleType("frappe.www.printview")
        printview.validate_print_permission = MagicMock()
        pypdf = ModuleType("pypdf")
        pypdf.PdfWriter = FakePdfWriter
        cryptography = ModuleType("cryptography")
        cryptography_hazmat = ModuleType("cryptography.hazmat")
        cryptography_primitives = ModuleType("cryptography.hazmat.primitives")
        cryptography_primitives.hashes = MagicMock()
        cryptography_primitives.serialization = MagicMock()
        cryptography_asymmetric = ModuleType("cryptography.hazmat.primitives.asymmetric")
        cryptography_asymmetric.padding = MagicMock()
        modules = {
            "frappe": self.framework,
            "frappe.utils": utils,
            "frappe.model.document": document,
            "erpnext.controllers.queries": queries,
            "ury.ury_pos.api": pos_api,
            "ury.ury.api.ury_kot_generate": kot,
            "frappe.www.printview": printview,
            "pypdf": pypdf,
            "cryptography": cryptography,
            "cryptography.hazmat": cryptography_hazmat,
            "cryptography.hazmat.primitives": cryptography_primitives,
            "cryptography.hazmat.primitives.asymmetric": cryptography_asymmetric,
        }
        self.enterContext(patch.dict(sys.modules, modules))

        root = Path(__file__).resolve().parents[2]
        self.order = self._load(
            "ury.ury.doctype.ury_order.ury_order",
            root / "doctype/ury_order/ury_order.py",
        )
        self.print_api = self._load(
            "ury.ury.api.ury_print",
            root / "api/ury_print.py",
        )

    def enterContext(self, context):
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    @staticmethod
    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _profile():
        return SimpleNamespace(
            name="POS Profile",
            transfer_role_permissions=[],
            role_allowed_for_billing=[],
            remove_items=1,
            custom_enable_kot_reprint=0,
        )

    @staticmethod
    def _invoice(waiter="captain_a@example.com", items=None):
        rows = items or [Row(name="line-1", item_code="FOOD", item_name="Food", qty=2)]
        for row in rows:
            for field, value in {
                "rate": 100,
                "price_list_rate": 100,
                "base_price_list_rate": 100,
                "comment": None,
                "custom_course": None,
                "ury_held": 0,
                "cost_center": None,
                "uom": None,
                "conversion_factor": 1,
                "warehouse": None,
            }.items():
                if not hasattr(row, field):
                    setattr(row, field, value)
        return Doc(
            name="POS-INV-1",
            waiter=waiter,
            branch="Branch A",
            restaurant_table="T1",
            pos_profile="POS Profile",
            docstatus=0,
            invoice_printed=0,
            custom_merged_tables=None,
            custom_split_group=None,
            items=rows,
            payments=[],
        )

    def _wire_invoice(self, invoice, room="Main Hall"):
        profile = self._profile()
        self.framework.get_doc.side_effect = lambda doctype, name=None: (
            invoice if doctype == "POS Invoice" else profile
        )
        self.framework.db.get_value.side_effect = (
            lambda doctype, name, field: room
            if doctype == "URY Table" and field == "restaurant_room"
            else None
        )
        self.order.getBranch.return_value = "Branch A"
        self.order.getRoom.return_value = [{"name": "Main Hall"}]
        new_invoice = self._invoice(waiter=invoice.waiter, items=[])
        new_invoice.name = None
        self.framework.new_doc.return_value = new_invoice

    def test_split_bill_refuses_other_waiters_order(self):
        invoice = self._invoice(waiter="captain_b@example.com")
        self._wire_invoice(invoice)

        with self.assertRaisesRegex(PermissionError, r"\[URY-SPLIT-NOT-PERMITTED\]"):
            self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}])

    def test_split_bill_refuses_room_outside_assignment(self):
        invoice = self._invoice(waiter="captain_a@example.com")
        self._wire_invoice(invoice, room="Back Room")

        with self.assertRaisesRegex(PermissionError, "outside your assigned room"):
            self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}])

    def test_split_bill_allows_owning_captain(self):
        invoice = self._invoice(waiter="captain_a@example.com")
        self._wire_invoice(invoice)

        result = self.order.split_bill(invoice.name, [{"name": "line-1", "qty": 1}])

        self.assertEqual(result, {"source_invoice": "POS-INV-1", "new_invoice": "POS-INV-SPLIT"})

    def _wire_context(self, invoice, user="captain_a@example.com"):
        table = Doc(
            name="T1",
            branch="Branch A",
            restaurant_room="Main Hall",
            as_dict=lambda: {"name": "T1", "branch": "Branch A"},
        )
        profile = self._profile()
        self.framework.session.user = user
        self.framework.get_doc.side_effect = lambda doctype, name=None: (
            table if doctype == "URY Table" else invoice if doctype == "POS Invoice" else profile
        )
        self.framework.get_all.side_effect = (
            lambda doctype, **kwargs: [Row(name=invoice.name)] if doctype == "POS Invoice" and invoice else []
        )
        self.framework.db.exists.return_value = "POS Profile"
        self.order.getBranch.return_value = "Branch A"
        self.order.getRoom.return_value = [{"name": "Main Hall"}]
        self.framework.get_roles.return_value = ["URY Captain"]

    def test_context_split_bill_flag(self):
        cases = (
            ("owner with two lines", self._invoice(items=[Row(name="a", qty=1), Row(name="b", qty=1)]), "captain_a@example.com", True),
            ("owner with quantity two", self._invoice(items=[Row(name="a", qty=2)]), "captain_a@example.com", True),
            ("other waiter", self._invoice(waiter="captain_b@example.com"), "captain_a@example.com", False),
            ("owner with one unit line", self._invoice(items=[Row(name="a", qty=1)]), "captain_a@example.com", False),
            ("no order", None, "captain_a@example.com", False),
        )
        for label, invoice, user, expected in cases:
            with self.subTest(label=label):
                self._wire_context(invoice, user)
                result = self.order.get_table_order_context("T1")
                self.assertEqual(result["permissions"]["split_bill"], expected)

    def _wire_print(self, sibling_open):
        self.invoice_printed = 0
        self.table_occupied = 1
        self.table_releases = []
        invoice = self._invoice()
        settings = SimpleNamespace(server_ip="127.0.0.1", port=631, printer_name="receipt")
        self.framework.get_doc.side_effect = lambda doctype, name=None: (
            settings if doctype == "Network Printer Settings" else invoice
        )
        self.framework.get_print.return_value = FakePdfWriter()
        self.framework.db.get_value.side_effect = self._print_get_value
        self.framework.db.set_value.side_effect = self._print_set_value
        self.framework.get_all.side_effect = lambda doctype, **kwargs: self._print_get_all(
            doctype, kwargs, sibling_open
        )

        cups = ModuleType("cups")
        cups.setServer = MagicMock()
        cups.setPort = MagicMock()
        cups.Connection = MagicMock(return_value=SimpleNamespace(printFile=MagicMock()))
        self.enterContext(patch.dict(sys.modules, {"cups": cups}))
        fulfilment = ModuleType("ury.ury.api.ury_fulfilment_posting_service")
        fulfilment.trigger_manufacture_on_bill_print = MagicMock()
        self.enterContext(patch.dict(sys.modules, {fulfilment.__name__: fulfilment}))

    def _print_get_value(self, doctype, name, field):
        if doctype == "POS Invoice" and isinstance(field, list):
            return ("T1", self.invoice_printed, "POS-INV-1")
        if doctype == "POS Invoice" and field == "restaurant_table":
            return "T1"
        if doctype == "POS Invoice" and field == "invoice_printed":
            return self.invoice_printed
        if doctype == "URY Table" and field == "restaurant_room":
            return "Main Hall"
        if doctype == "URY Table" and field == "occupied":
            return self.table_occupied
        if doctype == "POS Profile" and field == "print_format":
            return "Receipt"
        return None

    def _print_set_value(self, doctype, name, field, value=None, **kwargs):
        if doctype == "POS Invoice" and field == "invoice_printed":
            self.invoice_printed = value
        if doctype == "URY Table" and isinstance(field, dict) and field.get("occupied") == 0:
            self.table_occupied = 0
            self.table_releases.append(name)

    @staticmethod
    def _print_get_all(doctype, kwargs, sibling_open):
        if doctype == "URY Table":
            return [Row(name="T1", merged_with=None, occupied=1)]
        if doctype != "POS Invoice":
            return []
        filters = kwargs.get("filters", {})
        if "restaurant_table" in filters:
            rows = [Row(name="POS-INV-1", invoice_printed=1)]
            if sibling_open:
                rows.append(Row(name="POS-INV-2", invoice_printed=0))
            return rows
        return []

    def _print_one(self, endpoint):
        if endpoint == "network":
            with patch("builtins.open", mock_open()):
                return self.print_api.network_printing(
                    "POS Invoice", "POS-INV-1", "Printer", "Receipt"
                )
        if endpoint == "qz":
            return self.print_api.qz_print_update("POS-INV-1")
        return self.print_api.print_pos_page("POS Invoice", "POS-INV-1", "Receipt")

    def test_print_keeps_table_occupied_while_sibling_open(self):
        for endpoint in ("network", "qz", "browser"):
            with self.subTest(endpoint=endpoint):
                self._wire_print(sibling_open=True)
                result = self._print_one(endpoint)
                self.assertEqual(result, "Success" if endpoint == "network" else ({"status": "Success"} if endpoint == "qz" else None))
                self.assertEqual(self.table_releases, [])
                self.assertEqual(self.table_occupied, 1)

    def test_print_releases_table_when_last_sibling_printed(self):
        for endpoint in ("network", "qz", "browser"):
            with self.subTest(endpoint=endpoint):
                self._wire_print(sibling_open=False)
                self._print_one(endpoint)
                self.assertEqual(self.table_releases, ["T1"])
                self.assertEqual(self.table_occupied, 0)

    def test_desk_print_keeps_table_occupied_while_sibling_open(self):
        self._wire_print(sibling_open=True)
        self.assertTrue(self.order.release_tables_after_print("POS-INV-1"))
        self.assertEqual(self.table_releases, [])
        self.assertEqual(self.table_occupied, 1)

    def test_desk_print_releases_table_when_last_sibling_printed(self):
        self._wire_print(sibling_open=False)
        self.assertTrue(self.order.release_tables_after_print("POS-INV-1"))
        self.assertEqual(self.table_releases, ["T1"])
        self.assertEqual(self.table_occupied, 0)


if __name__ == "__main__":
    unittest.main()
