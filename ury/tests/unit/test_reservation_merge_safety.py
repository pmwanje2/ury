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


class TestReservationsAreInertUntilEnabled(_Loader):
	"""An opted-out branch must remain unchanged."""

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
		# Branch A has switched reservations on.
		self.frappe.db.get_value.side_effect = lambda doctype, name=None, fields=None, *a, **k: (
			1 if fields == "custom_enable_reservation" else "Branch A")
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


SWITCHED_OFF = "Table reservations are switched off for this branch."


class TestReservationWritesRefusedWhenBranchDisabled(_Loader):
	"""A direct API call cannot reserve, edit or seat a table on a branch that has not enabled reservations."""

	def setUp(self):
		super().setUp()
		self.reservation_modules()
		self.doc = MagicMock(status="Confirmed", branch="Branch A", reserved_table="T1")
		self.frappe.get_doc.return_value = self.doc
		self.frappe.db.get_value.side_effect = lambda doctype, name=None, fields=None, *a, **k: (
			0 if fields == "custom_enable_reservation" else "Branch A")
		self.frappe.db.get_all.return_value = []

	def assert_nothing_written(self):
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.commit.assert_not_called()
		self.doc.insert.assert_not_called()
		self.doc.save.assert_not_called()

	def test_create_is_refused(self):
		with self.assertRaisesRegex(ValueError, SWITCHED_OFF):
			self.reservation.create_table_reservation("T1", "CUST-1", "2026-11-20 19:00:00",
				customer_name="Grace", customer_phone="+256772123456", branch="Branch A")
		self.frappe.get_doc.assert_not_called()
		self.assert_nothing_written()

	def test_create_without_branch_uses_the_table_branch_and_is_refused(self):
		with self.assertRaisesRegex(ValueError, SWITCHED_OFF):
			self.reservation.create_table_reservation("T1", "CUST-1", "2026-11-20 19:00:00")
		self.frappe.get_doc.assert_not_called()
		self.assert_nothing_written()

	def test_edit_is_refused(self):
		with self.assertRaisesRegex(ValueError, SWITCHED_OFF):
			self.reservation.update_table_reservation("TR-1", notes="Window seat")
		self.assert_nothing_written()

	def test_edit_cannot_move_a_reservation_onto_a_disabled_branch(self):
		self.frappe.db.get_value.side_effect = lambda doctype, name=None, fields=None, *a, **k: (
			int(name == "Branch A") if fields == "custom_enable_reservation" else "Branch B")
		with self.assertRaisesRegex(ValueError, SWITCHED_OFF):
			self.reservation.update_table_reservation("TR-1", table="T9")
		self.assert_nothing_written()

	def test_status_change_is_refused_and_the_table_is_not_marked_occupied(self):
		for status in ("Completed", "Cancelled", "No Show"):
			with self.subTest(status=status):
				self.doc.status = "Confirmed"
				with patch.object(self.reservation, "is_user_authorized_for_reservation_status_change",
						return_value=True):
					with self.assertRaisesRegex(ValueError, SWITCHED_OFF):
						self.reservation.update_reservation_status("TR-1", status)
				self.assertEqual(self.doc.status, "Confirmed")
				self.assert_nothing_written()


class MissingColumn(Exception):
	"""What the database raises for a Branch column that migrate has not added yet."""


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

	def test_branch_without_the_reservation_column_reads_as_disabled(self):
		"""New code served before migrate must not throw out of POS Invoice validate."""
		self.reservation_modules()

		def get_value(doctype, name=None, fields=None, *a, **k):
			if fields == "custom_enable_reservation":
				raise MissingColumn("(1054, \"Unknown column 'custom_enable_reservation' in 'field list'\")")
			return "Branch A"

		self.frappe.db.get_value.side_effect = get_value
		self.frappe.db.has_column.side_effect = lambda doctype, column: column != "custom_enable_reservation"
		self.assertFalse(self.reservation.reservations_enabled("Branch A"))
		self.assertFalse(self.reservation.reservations_enabled_for_table("T1"))
		self.assertIsNone(self.reservation.check_table_reservation("T1"))
		self.frappe.db.commit.assert_not_called()


class TestDisabledScheduler(_Loader):
	def test_disabled_confirmed_record_is_untouched(self):
		self.reservation_modules()
		self.frappe.db.get_all.return_value = [SimpleNamespace(name='R1', branch='Branch A', reserved_at=datetime(2020, 1, 1))]
		with patch.object(self.scheduler, 'get_branch_reservation_settings', return_value={'enable_reservation': 0}):
			self.scheduler.process_reservation_no_shows()
		self.frappe.get_doc.assert_not_called()
