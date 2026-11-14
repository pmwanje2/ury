"""Site-less contracts for carrying upstream table reservations (ury-erp/ury#358).

Reservations are opt-in per Branch (custom_enable_reservation, default 0).
A branch that has not enabled them must see no reservation side effects on
its ordinary till, order and closing paths. Reservation writes must honour
native permissions, and the cashier Draft/Unbilled queues keep their
meaning (Draft: being built or a takeaway; Unbilled: a table bill not yet
printed).
"""

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


class Refused(Exception):
	pass


def _framework():
	fr = MagicMock()
	fr.whitelist = lambda *a, **k: (lambda f: f)
	fr._ = lambda message, *a: message
	fr.PermissionError = Refused
	fr.ValidationError = ValueError
	fr.session = SimpleNamespace(user="waiter@example.test")
	fr.throw.side_effect = lambda message, exc=ValueError, *a, **k: (_ for _ in ()).throw(exc(message))
	return fr


def _utils():
	utils = ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.flt = lambda value, *a: float(value or 0)
	now = datetime(2026, 11, 16, 12, 0)
	utils.now_datetime = lambda: now
	utils.get_datetime = lambda value=None: value if isinstance(value, datetime) else now
	utils.get_system_timezone = lambda: "Africa/Kampala"
	utils.convert_utc_to_timezone = lambda value, tz: value
	for name in ("add_to_date", "get_time", "getdate", "nowdate", "add_days", "validate_phone_number"):
		setattr(utils, name, MagicMock(name=name))
	utils.nestedset = MagicMock()
	return utils


class _Loader(unittest.TestCase):
	def setUp(self):
		self.frappe = _framework()
		dashboard = ModuleType("ury.ury.api.ury_dashboard")
		dashboard._business_day_bounds = MagicMock()
		pytz = ModuleType("pytz")
		pytz.UTC = timezone.utc
		self.enterContext(patch.dict(sys.modules, {
			"frappe": self.frappe, "frappe.utils": _utils(),
			"ury.ury.api.ury_dashboard": dashboard, "pytz": pytz,
		}))
		# A site with no closed sessions and no reservations yet.
		self.frappe.db.sql.return_value = []

	def load(self, name, relative):
		spec = importlib.util.spec_from_file_location(name, ROOT / relative)
		module = importlib.util.module_from_spec(spec)
		sys.modules[name] = module
		spec.loader.exec_module(module)
		return module

	def reservation_modules(self):
		self.average = self.load("ury.ury.report.average_table_time.average_table_time",
			"ury/report/average_table_time/average_table_time.py")
		self.reservation = self.load("ury.ury.api.table_reservation", "ury/api/table_reservation.py")
		self.scheduler = self.load("ury.ury.api.reservation_scheduler", "ury/api/reservation_scheduler.py")


class TestCashierQueuesKeepTheirMeaning(_Loader):
	def setUp(self):
		super().setUp()
		self.api = self.load("ury.ury_pos.api", "ury_pos/api.py")
		self.enterContext(patch.object(self.api, "getBranch", return_value="Branch A"))
		self.enterContext(patch.object(self.api, "_enrich_split_group_meta", side_effect=lambda rows: rows))
		self.frappe.db.sql.return_value = []
		self.frappe.get_all.return_value = []

	def where(self):
		sql = " ".join(self.frappe.db.sql.call_args.args[0].lower().split())
		return sql.split(" where ", 1)[1]

	def test_draft_holds_printed_bills_and_untabled_orders(self):
		for read in (lambda: self.api.getPosInvoice("Draft", 10, 0),
				lambda: self.api.getInvoiceForCashier("Draft", "cashier", 10, 0)):
			read()
			self.assertIn("invoice_printed = 1 or (invoice_printed = 0 and coalesce(restaurant_table, '') = '')",
				self.where())

	def test_unbilled_holds_unprinted_table_bills(self):
		for read in (lambda: self.api.getPosInvoice("Unbilled", 10, 0),
				lambda: self.api.getInvoiceForCashier("Unbilled", "cashier", 10, 0)):
			read()
			self.assertIn("invoice_printed = 0 and restaurant_table is not null", self.where())

	def test_unbilled_search_matches_unprinted_table_bills_only(self):
		self.api.searchPosInvoice("tab", "Unbilled")
		filters = self.frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["invoice_printed"], 0)
		self.assertEqual(filters["restaurant_table"], ["not in", [None, ""]])

	def test_draft_search_is_not_narrowed_to_unprinted(self):
		self.api.searchPosInvoice("tab", "Draft")
		filters = self.frappe.get_all.call_args.kwargs["filters"]
		self.assertNotIn("invoice_printed", filters)


class TestCustomerDefaultsStayNative(_Loader):
	def setUp(self):
		super().setUp()
		self.api = self.load("ury.ury_pos.api", "ury_pos/api.py")
		self.frappe.has_permission.return_value = True
		self.frappe.db.get_single_value.side_effect = lambda doctype, field: (
			{"customer_group": "Retail", "territory": "Uganda"}[field] if doctype == "Selling Settings" else None)
		self.frappe.db.exists.return_value = True
		self.frappe.db.get_default.return_value = None
		self.frappe.db.get_value.return_value = "Some Leaf Group"

	def test_omitted_group_and_territory_use_selling_settings(self):
		result = self.api.create_customer("Guest Name", "+256772123456")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual((payload["customer_group"], payload["territory"]), ("Retail", "Uganda"))
		self.assertEqual(result["status"], "success")

	def test_unset_selling_settings_group_is_a_configuration_error(self):
		self.frappe.db.get_single_value.side_effect = lambda doctype, field: None
		with self.assertRaisesRegex(ValueError, "Default Customer Group"):
			self.api.create_customer("Guest Name", "+256772123456")
		self.frappe.get_doc.assert_not_called()


class TestReservationsAreInertUntilEnabled(_Loader):
	"""Tankhill and Salama have not enabled reservations; nothing may write."""

	def setUp(self):
		super().setUp()
		self.reservation_modules()
		self.branch = {"custom_enable_reservation": 0, "custom_buffer_time": 30, "custom_grace_period": 15,
			"custom_avg_table_time_last_day": 0, "custom_avg_table_time_last_week": 0}
		self.frappe.db.get_value.side_effect = self.get_value
		self.frappe.db.get_all.return_value = []
		self.frappe.get_all.return_value = []

	def get_value(self, doctype, name=None, fields=None, as_dict=False, **kwargs):
		if doctype == "Branch":
			if isinstance(fields, (list, tuple)):
				return SimpleNamespace(**{f: self.branch.get(f) for f in fields}, get=lambda k, d=None: self.branch.get(k, d)) if not as_dict else {f: self.branch.get(f) for f in fields}
			return self.branch.get(fields)
		if doctype == "URY Table":
			return "Branch A" if fields == "branch" else 1
		return None

	def assert_no_writes(self):
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.commit.assert_not_called()
		self.frappe.get_doc.assert_not_called()

	def test_settings_read_does_not_write_branch_averages(self):
		settings = self.reservation.get_branch_reservation_settings("Branch A")
		self.assertEqual(settings["enable_reservation"], 0)
		self.assert_no_writes()

	def test_table_lock_check_is_a_pure_read_when_disabled(self):
		self.assertIsNone(self.reservation.check_table_reservation("T1"))
		self.assert_no_writes()
		self.frappe.db.get_all.assert_not_called()

	def test_floor_poll_does_not_release_tables_when_disabled(self):
		self.frappe.db.get_all.return_value = [SimpleNamespace(name="T1", branch="Branch A")]
		self.assertEqual(self.reservation.get_active_reservations("Branch A"), [])
		self.assert_no_writes()

	def test_completed_reservation_lookup_is_skipped_when_disabled(self):
		self.assertIsNone(self.reservation.get_completed_reservation_for_table("T1", "Branch A"))
		self.frappe.db.get_all.assert_not_called()

	def test_closing_does_not_recompute_branch_averages_when_disabled(self):
		closing = self.load("ury.ury.hooks.ury_pos_closing_entry", "ury/hooks/ury_pos_closing_entry.py")
		self.frappe.db.get_value.side_effect = lambda doctype, name, field=None, *a, **k: (
			"Branch A" if field == "branch" else self.branch.get(field) if doctype == "Branch" else None)
		with patch.object(self.average, "update_branch_avg_table_times") as update:
			closing.update_branch_avg_times(SimpleNamespace(get=lambda k: {"pos_opening_entry": "OPEN-1"}.get(k),
				pos_opening_entry="OPEN-1", pos_profile="Till"))
		update.assert_not_called()


class TestReservationWritesHonourNativePermissions(_Loader):
	def setUp(self):
		super().setUp()
		self.reservation_modules()
		self.doc = MagicMock(status="Confirmed", branch="Branch A", reserved_table="T1")
		self.frappe.get_doc.return_value = self.doc
		self.frappe.db.get_value.return_value = "Branch A"
		self.frappe.db.get_all.return_value = []

	def test_create_uses_native_insert_permission(self):
		self.reservation.create_table_reservation("T1", "CUST-1", "2026-11-20 19:00:00",
			customer_name="Grace", customer_phone="+256772123456", branch="Branch A")
		self.doc.insert.assert_called_once_with()

	def test_edit_checks_write_permission_and_saves_natively(self):
		self.reservation.update_table_reservation("TR-1", notes="Window seat")
		self.doc.check_permission.assert_called_with("write")
		self.doc.save.assert_called_once_with()

	def test_status_change_checks_write_permission_and_saves_natively(self):
		with patch.object(self.reservation, "is_user_authorized_for_reservation_status_change", return_value=True):
			self.reservation.update_reservation_status("TR-1", "Cancelled")
		self.doc.check_permission.assert_called_with("write")
		self.doc.save.assert_called_once_with()

	def test_occupancy_sync_is_not_a_public_endpoint(self):
		whitelisted = []
		self.frappe.whitelist = lambda *a, **k: (lambda f: whitelisted.append(f.__name__) or f)
		self.reservation_modules()
		self.assertIn("get_active_reservations", whitelisted)
		self.assertNotIn("sync_branch_table_occupancy", whitelisted)


class TestOrdersSkipReservationsWhenDisabled(_Loader):
	def test_pos_invoice_validate_does_not_load_reservations_when_disabled(self):
		self.reservation_modules()
		self.frappe.db.get_value.side_effect = lambda doctype, name=None, fields=None, *a, **k: (
			0 if fields == "custom_enable_reservation" else "Branch A")
		self.assertFalse(self.reservation.reservations_enabled_for_table("T1"))
		self.assertFalse(self.reservation.reservations_enabled("Branch A"))
		self.frappe.db.get_value.side_effect = lambda doctype, name=None, fields=None, *a, **k: (
			1 if fields == "custom_enable_reservation" else "Branch A")
		self.assertTrue(self.reservation.reservations_enabled_for_table("T1"))


class TestStaleMosaicBundleIsNotShipped(unittest.TestCase):
	def test_no_prebuilt_mosaic_bundle_or_route(self):
		self.assertFalse((ROOT / "www" / "URYMosaic.html").exists())
		self.assertFalse((ROOT / "public" / "URYMosaic").exists())


class TestCustomFieldFixtureHasOneRowPerField(unittest.TestCase):
	"""ury-erp/ury#358 appended a second copy of 112 Custom Field rows (version-16 has none)."""

	def test_each_custom_field_name_appears_once(self):
		import json
		import collections

		rows = json.loads((ROOT / "fixtures" / "custom_field.json").read_text(encoding="utf-8"))
		repeated = sorted(name for name, count in collections.Counter(row["name"] for row in rows).items() if count > 1)
		self.assertEqual(repeated, [])
		names = {row["name"] for row in rows}
		for field in ("custom_reservation_settings_section", "custom_enable_reservation", "custom_buffer_time",
				"custom_grace_period", "custom_avg_table_time_last_day", "custom_avg_table_time_last_week"):
			self.assertIn(f"Branch-{field}", names)


def _split_bill_harness():
	from ury.ury.doctype.ury_order import test_split_bill_release as harness
	return harness.TestSplitBillAndPrintRelease


class TestCaptainContextCarriesTheBranchReservationFlag(unittest.TestCase):
	"""The waiter floor reads the flag from its existing context call."""

	setUp = _split_bill_harness().setUp
	enterContext = _split_bill_harness().enterContext
	_load = staticmethod(_split_bill_harness()._load)

	def context(self, enabled):
		self.order.getBranch.return_value = "Branch A"
		self.order.getRoom.return_value = []
		self.order.posOpening.return_value = 0
		self.framework.db.exists.return_value = None
		self.framework.db.get_value.side_effect = lambda doctype, name, field=None, *a, **k: (
			enabled if (doctype, name, field) == ("Branch", "Branch A", "custom_enable_reservation") else None)
		return self.order.get_captain_context()

	def test_flag_is_true_only_when_the_branch_enabled_reservations(self):
		self.assertIs(self.context(1)["reservation_enabled"], True)
		self.assertIs(self.context(0)["reservation_enabled"], False)
		self.assertIs(self.context(None)["reservation_enabled"], False)


if __name__ == "__main__":
	unittest.main()
