"""Site-less contract for POS-PR-1 R2: merged-bill submit atomicity, EFRIS print-wait config, and auto-reprint queue.

Covers:
- P0: submit_order_invoice sets doc.flags.ignore_payment_sync = True on legacy merged bills,
  and sync_merged_invoice skips already-posted partners and re-raises partner submit errors
  when doc.docstatus == 1 (all-or-nothing, compatible with #143).
- Print timing: POS Profile custom_efris_print_wait_seconds (Int, default 5, non_negative 1)
  exposed on getPosProfile / getPosProfileFull (clamped 0..15).
- Auto-reprint queue: queue_fiscal_reprint creates an idempotent AWAITING_FDN row in
  URY Bill Reprint Log, and process_pending_fiscal_reprints polls
  uganda_compliance.efris.efris_queue.get_efris_status and dispatches COPY #n via
  print_bill_copy once the FDN arrives.
"""

import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[3]


def _load_modules():
	try:
		import frappe  # noqa: F401
		from ury.ury.doctype.ury_order import ury_order
		from ury.ury.hooks import ury_pos_invoice
		from ury.ury_pos import api
		return ury_order, ury_pos_invoice, api
	except ImportError:
		pass

	class _Dict(dict):
		def __getattr__(self, key):
			try:
				return self[key]
			except KeyError:
				return None

		def __setattr__(self, key, value):
			self[key] = value

	frappe_mod = ModuleType("frappe")
	frappe_mod._dict = _Dict
	frappe_mod._ = lambda msg, *a, **kw: msg
	frappe_mod.whitelist = lambda *a, **kw: (lambda fn: fn)
	frappe_mod.cache = MagicMock()
	frappe_mod.PermissionError = PermissionError
	frappe_mod.ValidationError = ValueError
	frappe_mod.DoesNotExistError = LookupError
	frappe_mod.flags = _Dict(in_bill_merge_sync=False)
	frappe_mod.db = MagicMock()
	frappe_mod.session = SimpleNamespace(user="cashier@example.com")
	frappe_mod.utils = MagicMock()
	frappe_mod.utils.cint = lambda v: int(v or 0)
	frappe_mod.utils.flt = lambda v, *a, **kw: float(v or 0)
	frappe_mod.utils.now = lambda: "2026-11-23 12:00:00"
	frappe_mod.throw = MagicMock(side_effect=lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg)))
	frappe_mod.get_doc = MagicMock()
	frappe_mod.get_cached_doc = MagicMock()
	frappe_mod.get_roles = MagicMock(return_value=["URY Cashier"])
	frappe_mod.has_permission = MagicMock(return_value=True)
	frappe_mod.publish_realtime = MagicMock()
	frappe_mod.log_error = MagicMock()
	frappe_mod.get_traceback = MagicMock(return_value="traceback")

	utils_mod = ModuleType("frappe.utils")
	utils_mod.cint = lambda v: int(v or 0)
	utils_mod.flt = lambda v, *a, **kw: float(v or 0)
	utils_mod.now = lambda: "2026-11-23 12:00:00"
	utils_mod.now_datetime = MagicMock()
	utils_mod.nowdate = lambda: "2026-11-23"
	utils_mod.getdate = lambda v=None: v
	utils_mod.get_datetime = lambda v=None: v
	utils_mod.get_system_timezone = lambda: "UTC"
	utils_mod.get_first_day = lambda v=None: v
	utils_mod.get_last_day = lambda v=None: v
	utils_mod.add_days = lambda d, n: d
	utils_mod.add_to_date = lambda d, **kw: d
	utils_mod.get_time = lambda v=None: v
	utils_mod.time_diff_in_seconds = lambda a, b: 0
	utils_mod.nestedset = MagicMock()
	utils_mod.validate_phone_number = lambda v, *a, **kw: True

	doc_mod = ModuleType("frappe.model.document")
	doc_mod.Document = type("Document", (), {})
	model_mod = ModuleType("frappe.model")
	model_mod.document = doc_mod
	model_naming = ModuleType("frappe.model.naming")
	model_naming.make_autoname = lambda *a, **kw: "TEST-1"
	perms_mod = ModuleType("frappe.permissions")
	perms_mod.get_doctypes_with_read = lambda: []

	stubs = {
		"frappe": frappe_mod,
		"frappe.utils": utils_mod,
		"frappe.model": model_mod,
		"frappe.model.document": doc_mod,
		"frappe.model.naming": model_naming,
		"frappe.permissions": perms_mod,
		"erpnext": ModuleType("erpnext"),
		"erpnext.controllers": ModuleType("erpnext.controllers"),
		"erpnext.controllers.queries": MagicMock(),
		"erpnext.accounts": ModuleType("erpnext.accounts"),
		"erpnext.accounts.doctype": ModuleType("erpnext.accounts.doctype"),
		"erpnext.accounts.doctype.pos_invoice": ModuleType("erpnext.accounts.doctype.pos_invoice"),
		"erpnext.accounts.doctype.pos_invoice.pos_invoice": MagicMock(),
		"ury.ury.api.ury_kot_generate": MagicMock(),
		"ury.ury.api.ury_kot_routing": MagicMock(),
		"ury.ury.api.ury_print": MagicMock(),
		"ury.ury.api.ury_order_reservation_service": MagicMock(),
		"ury.ury.api.ury_stock_policy": MagicMock(),
		"ury.ury.api.ury_kot_notification": MagicMock(),
		"ury.ury.api.ury_stock_reservation": MagicMock(),
		"ury.ury.api.ury_pos_invoice_qty_reduction": MagicMock(),
		"ury.ury.api.ury_availability": MagicMock(),
		"ury.ury.api.item_price": MagicMock(),
		"ury.ury.api.ury_kot_order_number": MagicMock(),
		"ury.ury.doctype.alert_settings": ModuleType("ury.ury.doctype.alert_settings"),
		"ury.ury.doctype.alert_settings.alert_settings": MagicMock(),
		"ury.ury.doctype.staff_discount_policy": ModuleType("ury.ury.doctype.staff_discount_policy"),
		"ury.ury.doctype.staff_discount_policy.staff_discount_policy": MagicMock(),
		"ury.ury.utils": MagicMock(),
		"ury.ury.utils.stock_count_gate": MagicMock(),
		"ury.ury_pos.default_customer": MagicMock(get_default_customer_config=lambda _p: {}),
	}
	for name, mod in stubs.items():
		sys.modules.setdefault(name, mod)
	import ury.ury.api as _ury_api_pkg
	if not hasattr(_ury_api_pkg, "ury_print"):
		_ury_api_pkg.ury_print = sys.modules["ury.ury.api.ury_print"]
	from ury.ury.doctype.ury_order import ury_order
	from ury.ury.hooks import ury_pos_invoice
	from ury.ury_pos import api
	return ury_order, ury_pos_invoice, api


ury_order, ury_pos_invoice, api = _load_modules()


class TestMergedBillSubmitAtomicity(unittest.TestCase):
	def test_submit_order_invoice_sets_ignore_payment_sync_before_submitting_merged_bill(self):
		seen_flags = []

		class FakeInvoice:
			def __init__(self, name, partner=None):
				self.name = name
				self.docstatus = 0
				self.customer = "Walk In"
				self.company = "Demo Restaurant"
				self.pos_profile = "Main Till"
				self.restaurant_table = None
				self.custom_merged_tables = None
				self.custom_merged_pos_invoice = partner
				self.items = []
				self.payments = [SimpleNamespace(mode_of_payment="Cash", amount=1000)]
				self.rounded_total = 1000
				self.grand_total = 1000
				self.conversion_rate = 1
				self.flags = SimpleNamespace()

			def precision(self, _field):
				return 2

			def submit(self):
				seen_flags.append(bool(getattr(self.flags, "ignore_payment_sync", False)))
				self.docstatus = 1

		primary = FakeInvoice("INV-PRI", partner="INV-SEC")
		partner = FakeInvoice("INV-SEC", partner="INV-PRI")
		profile = SimpleNamespace(name="Main Till", customer="Walk In", allow_partial_payment=0)
		docs = {"INV-PRI": primary, "INV-SEC": partner}

		fake_frappe = MagicMock()
		fake_frappe.flags = SimpleNamespace(in_bill_merge_sync=False)
		fake_frappe.get_doc.side_effect = lambda dt, name, **kw: docs[name]
		fake_frappe.get_cached_doc.return_value = profile
		fake_frappe.throw.side_effect = lambda msg, *a, **kw: (_ for _ in ()).throw(RuntimeError(msg))

		with patch.object(ury_order, "frappe", fake_frappe), \
			 patch.object(ury_order, "_lock_settlement_customer", return_value=0), \
			 patch.object(ury_order, "_check_held_before_settlement"), \
			 patch.object(ury_order, "_enforce_on_account", return_value=None):
			result = ury_order.submit_order_invoice("INV-PRI")

		self.assertEqual(result, {"name": "INV-PRI", "status": "Submitted"})
		self.assertEqual(seen_flags, [True], "submit_order_invoice must set doc.flags.ignore_payment_sync = True")

	def test_sync_merged_invoice_reraises_partner_submit_error_when_primary_submits(self):
		primary = SimpleNamespace(
			name="INV-PRI",
			docstatus=1,
			invoice_printed=1,
			paid_amount=15000,
			custom_merged_pos_invoice="INV-SEC",
			payments=[SimpleNamespace(mode_of_payment="Cash", amount=15000)],
			flags=SimpleNamespace(ignore_payment_sync=True),
		)
		target = MagicMock()
		target.name = "INV-SEC"
		target.docstatus = 0
		target.rounded_total = 10000
		target.flags = SimpleNamespace()
		target.submit.side_effect = RuntimeError("Partner EFRIS / stock validation failed")

		fake_frappe = MagicMock()
		fake_frappe.flags = SimpleNamespace(in_bill_merge_sync=False)
		fake_frappe.db.exists.return_value = True
		fake_frappe.get_doc.return_value = target

		with patch.object(ury_pos_invoice, "frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Partner EFRIS"):
				ury_pos_invoice.sync_merged_invoice(primary)

		target.set.assert_not_called()
		self.assertFalse(fake_frappe.flags.in_bill_merge_sync)

	def test_sync_merged_invoice_does_not_resave_already_submitted_partner(self):
		primary = SimpleNamespace(
			name="INV-PRI",
			docstatus=1,
			invoice_printed=1,
			paid_amount=15000,
			custom_merged_pos_invoice="INV-SEC",
			payments=[],
			flags=SimpleNamespace(ignore_payment_sync=True),
		)
		target = MagicMock()
		target.name = "INV-SEC"
		target.docstatus = 1
		target.flags = SimpleNamespace()

		fake_frappe = MagicMock()
		fake_frappe.flags = SimpleNamespace(in_bill_merge_sync=False)
		fake_frappe.db.exists.return_value = True
		fake_frappe.get_doc.return_value = target

		with patch.object(ury_pos_invoice, "frappe", fake_frappe):
			ury_pos_invoice.sync_merged_invoice(primary)

		target.save.assert_not_called()
		target.submit.assert_not_called()

	def test_sync_merged_invoice_keeps_draft_on_update_non_blocking(self):
		primary = SimpleNamespace(
			name="INV-PRI",
			docstatus=0,
			invoice_printed=1,
			paid_amount=0,
			custom_merged_pos_invoice="INV-SEC",
			payments=[],
			flags=SimpleNamespace(),
		)
		target = MagicMock()
		target.name = "INV-SEC"
		target.docstatus = 0
		target.save.side_effect = RuntimeError("transient draft sync issue")

		fake_frappe = MagicMock()
		fake_frappe.flags = SimpleNamespace(in_bill_merge_sync=False)
		fake_frappe.db.exists.return_value = True
		fake_frappe.get_doc.return_value = target

		with patch.object(ury_pos_invoice, "frappe", fake_frappe):
			ury_pos_invoice.sync_merged_invoice(primary)

		fake_frappe.log_error.assert_called()


class TestEfrisPrintWaitConfiguration(unittest.TestCase):
	def test_pos_profile_custom_field_declared_with_5s_default_and_15s_max(self):
		from ury import hooks

		setup = (APP / "setup_customizations.py").read_text(encoding="utf-8")
		self.assertIn('"fieldname": "custom_efris_print_wait_seconds"', setup)
		declared = setup[setup.index("custom_efris_print_wait_seconds"):][:500]
		self.assertIn('"default": "5"', declared)
		self.assertIn("15", declared)
		self.assertNotIn("180", declared)

		name = "POS Profile-custom_efris_print_wait_seconds"
		rows = json.loads((APP / "fixtures" / "custom_field.json").read_text(encoding="utf-8"))
		fields = {row["name"]: row for row in rows}
		self.assertIn(name, fields)
		self.assertEqual(fields[name]["fieldtype"], "Int")
		self.assertEqual(str(fields[name]["default"]), "5")
		self.assertEqual(fields[name].get("non_negative"), 1)
		self.assertIn("15", fields[name].get("description") or "")
		self.assertNotIn("180", fields[name].get("description") or "")

		named = [
			f for entry in hooks.fixtures
			if isinstance(entry, dict) and entry.get("doctype") == "Custom Field"
			for flt in entry.get("filters", []) for f in flt[2]
		]
		self.assertIn(name, named)

	def test_get_pos_profile_exposes_wait_seconds_default_5_and_clamps_0_to_15(self):
		for raw, expected in ((None, 5), ("", 5), (0, 0), (10, 10), (15, 15), (16, 15), (45, 15), (500, 15), (-5, 0)):
			with self.subTest(raw=raw):
				profile = MagicMock()
				profile.name = "Main Till"
				profile.branch = "Main Branch"
				profile.company = "Demo Co"
				profile.country = "Uganda"
				profile.warehouse = "Stores - DC"
				profile.print_format = "Restaurant Receipt 80mm"
				profile.table_attention_time = 10
				profile.paid_limit = 30
				profile.custom_enable_discount = 0
				profile.custom_enable_multiple_cashier = 0
				profile.custom_edit_order_type = 0
				profile.custom_enable_kot_reprint = 0
				profile.custom_invoice_warning_time = 0
				profile.custom_disable_print_status_tracking = 0
				profile.qz_print = 0
				profile.qz_host = None
				profile.printer_settings = []
				profile.applicable_for_users = [SimpleNamespace(user="cashier@example.com")]
				profile.role_allowed_for_billing = []
				profile.role_restricted_for_table_order = []
				profile.transfer_role_permissions = []
				profile.owner = "Administrator"
				profile.creation = "2026-11-23"
				profile.modified = "2026-11-23"
				profile.modified_by = "Administrator"
				profile.docstatus = 0
				profile.idx = 0
				profile.customer = "Walk In"
				profile.disabled = 0
				profile.company_address = None
				profile.restaurant = "Main"
				profile.currency = "UGX"

				values = {"custom_efris_print_wait_seconds": raw, "payments": []}
				profile.get.side_effect = lambda key, default=None, _v=values: _v.get(key, default)

				fake_frappe = MagicMock()
				fake_frappe.session = SimpleNamespace(user="cashier@example.com")
				fake_frappe.db.exists.return_value = "Main Till"
				fake_frappe.db.get_value.return_value = "Uganda"
				fake_frappe.get_doc.return_value = profile
				fake_frappe.get_single.return_value = SimpleNamespace(disable_rounded_total=0)

				with patch.object(api, "frappe", fake_frappe), \
					 patch.object(api, "getBranch", return_value="Main Branch"), \
					 patch.object(api, "get_default_customer_config", return_value={}):
					limited = api.getPosProfile()
					full = api.getPosProfileFull("Main Till")

				self.assertEqual(limited.get("efris_print_wait_seconds"), expected)
				self.assertEqual(full.get("efris_print_wait_seconds"), expected)


class TestPendingFiscalReprintQueue(unittest.TestCase):
	def test_queue_fiscal_reprint_creates_idempotent_awaiting_fdn_log_row(self):
		fn = getattr(api, "queue_fiscal_reprint", None)
		self.assertTrue(callable(fn), "Missing api.queue_fiscal_reprint")
		inserted = []
		doc = SimpleNamespace(
			name="INV-001", branch="Main Branch", docstatus=1, status="Paid",
			pos_profile="Main Till", restaurant_table="T1",
		)

		def get_doc(arg, *a, **kw):
			if arg == "POS Invoice":
				return doc
			rec = dict(arg)
			return type("Row", (), {"insert": lambda _, **ikw: inserted.append(rec) or rec})()

		fake_frappe = MagicMock()
		fake_frappe.session = SimpleNamespace(user="cashier@example.com")
		fake_frappe.get_roles.return_value = ["URY Cashier"]
		fake_frappe.has_permission.return_value = True
		fake_frappe.get_doc.side_effect = get_doc
		fake_frappe.db.exists.side_effect = lambda dt, name: any(r["name"] == name for r in inserted)

		with patch.object(api, "frappe", fake_frappe), \
			 patch.object(api, "getBranch", return_value="Main Branch"), \
			 patch("ury.ury.doctype.ury_order.ury_order._enforce_order_access"):
			first = fn("INV-001")
			second = fn("INV-001")

		self.assertEqual(first["status"], "AWAITING_FDN")
		self.assertEqual(second["status"], "AWAITING_FDN")
		self.assertEqual(len(inserted), 1)
		self.assertEqual(inserted[0]["doctype"], "URY Bill Reprint Log")
		self.assertEqual(inserted[0]["invoice"], "INV-001")
		self.assertEqual(inserted[0]["attempt"], 0)
		self.assertEqual(inserted[0]["copy_number"], 0)

	def test_process_pending_fiscal_reprints_waits_for_fdn_then_dispatches_copy(self):
		from ury import hooks

		fn = getattr(api, "process_pending_fiscal_reprints", None)
		self.assertTrue(callable(fn), "Missing api.process_pending_fiscal_reprints")
		self.assertIn(
			"ury.ury_pos.api.process_pending_fiscal_reprints",
			hooks.scheduler_events["cron"]["* * * * *"],
		)

		row = SimpleNamespace(name="PENDING-EFRIS-INV-001", invoice="INV-001", job_owner="cashier@example.com")
		inv = SimpleNamespace(
			name="INV-001", docstatus=1, status="Paid", branch="Main Branch",
			pos_profile="Main Till", restaurant_table="T1", custom_merged_pos_invoice=None,
		)
		fake_frappe = MagicMock()
		fake_frappe.get_all.return_value = [row]
		fake_frappe.get_doc.return_value = inv
		fake_frappe.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
			0 if (dt, field) == ("POS Profile", "qz_print") else None
		)
		fake_efris = ModuleType("uganda_compliance.efris.efris_queue")
		fake_efris.get_efris_status = MagicMock(side_effect=[
			{"fdn": None, "efris_status": "Pending"},
			{"fdn": "324013579864", "efris_status": "Submitted"},
		])

		with patch.dict(sys.modules, {
			"uganda_compliance": ModuleType("uganda_compliance"),
			"uganda_compliance.efris": ModuleType("uganda_compliance.efris"),
			"uganda_compliance.efris.efris_queue": fake_efris,
		}), patch.object(api, "frappe", fake_frappe), \
			patch.object(api, "print_bill_copy", return_value={"status": "SUBMITTED", "copy_number": 1}) as pbc, \
			patch("ury.ury.api.ury_print._bill_printers", return_value=["Bar Printer"]):
			# Poll 1: FDN not ready yet -> do not print
			fn()
			pbc.assert_not_called()
			# Poll 2: FDN arrived -> dispatch COPY #n and mark DISPATCHED
			fn()
			pbc.assert_called_once_with(invoice="INV-001", idempotency_key="efris-auto-INV-001", delivery="network")
			fake_frappe.db.set_value.assert_called_with(
				"URY Bill Reprint Log", "PENDING-EFRIS-INV-001", {"status": "DISPATCHED"}
			)

	def test_process_pending_fiscal_reprints_switches_to_job_owner_and_restores_user(self):
		"""P1: Scheduler must impersonate row.job_owner during print_bill_copy so multi-branch sites resolve branch."""
		row = SimpleNamespace(name="PENDING-EFRIS-INV-MB1", invoice="INV-MB1", job_owner="cashier@example.com")
		inv = SimpleNamespace(
			name="INV-MB1", docstatus=1, status="Paid", branch="Branch A",
			pos_profile="Main Till", restaurant_table="T1", custom_merged_pos_invoice=None,
		)
		fake_frappe = MagicMock()
		fake_frappe.session = SimpleNamespace(user="Administrator")
		user_transitions = []

		def set_user(user):
			user_transitions.append(user)
			fake_frappe.session.user = user

		fake_frappe.set_user.side_effect = set_user
		fake_frappe.get_all.return_value = [row]
		fake_frappe.get_doc.return_value = inv
		fake_frappe.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
			0 if (dt, field) == ("POS Profile", "qz_print") else None
		)
		fake_efris = ModuleType("uganda_compliance.efris.efris_queue")
		fake_efris.get_efris_status = MagicMock(return_value={"fdn": "324013579864", "efris_status": "Submitted"})

		seen_user_in_pbc = []

		def fake_pbc(**kwargs):
			seen_user_in_pbc.append(fake_frappe.session.user)
			return {"status": "SUBMITTED", "copy_number": 1}

		with patch.dict(sys.modules, {
			"uganda_compliance": ModuleType("uganda_compliance"),
			"uganda_compliance.efris": ModuleType("uganda_compliance.efris"),
			"uganda_compliance.efris.efris_queue": fake_efris,
		}), patch.object(api, "frappe", fake_frappe), \
			patch.object(api, "print_bill_copy", side_effect=fake_pbc), \
			patch("ury.ury.api.ury_print._bill_printers", return_value=["Bar Printer"]):
			api.process_pending_fiscal_reprints()

		self.assertEqual(seen_user_in_pbc, ["cashier@example.com"])
		self.assertEqual(user_transitions, ["cashier@example.com", "Administrator"])
		self.assertEqual(fake_frappe.session.user, "Administrator")


if __name__ == "__main__":
	unittest.main()
