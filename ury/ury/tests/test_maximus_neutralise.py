"""Theme N (Maximus fork, PLAN-ury-route-b section 3): upstream behaviours neutralised on version-16.

N1  Insights auto-grant runs only when site config `ury_analytics_access` is set (default off).
N2  No global `Report` class override; each URY report supplies its own optional-filter defaults.
N3  `bench migrate` does not hide the Setup sidebar (Role Permissions, Data Import, ...).
N4  One-draft-per-branch Stock Reconciliation guard runs only when site config
    `ury_stock_reconciliation_single_draft` is set (default off).
N5  `ury/role_permissions.py` is carried, so a fresh install gets the same role DocPerms as the
    version-2 line (including URY Admin), and the `v2_0.admin_permissions` Patch Log line is kept.

Run: bench --site <site> run-tests --app ury --module ury.ury.tests.test_maximus_neutralise
"""

import importlib
import importlib.util
import os
from unittest.mock import patch

import frappe
from frappe.core.doctype.report.report import Report
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

ANALYTICS_GATE = "ury_analytics_access"
SR_GATE = "ury_stock_reconciliation_single_draft"
SIDEBAR_ITEMS = ("Role", "Role Permissions", "User Permissions", "Email Account", "Data Import")

# Upstream defects unrelated to theme N. Each must still fail, so a fix upstream is noticed here.
# Stock Correction Report: PARTITION BY b.warehouse names a column the parent table lacks (1054).
KNOWN_BROKEN_REPORTS = {"Stock Correction Report"}

# Reports that were URY Query Reports on version-16 @100c43fe and relied on the global override.
REPORT_LOCAL_DEFAULTS = {"Wastage and Damage Report"}


def _ury_modules():
	return frappe.get_all("Module Def", filters={"app_name": "ury"}, pluck="name")


def _mandatory_only_filters(report):
	values = {}
	for f in report.filters:
		if not f.mandatory:
			continue
		values[f.fieldname] = today() if f.fieldtype == "Date" else "_ury_n_probe"
	return values


def _run_natively(report, filters):
	"""Run a report through frappe's own Report class, whatever class the hooks resolve."""
	if report.report_type == "Query Report":
		return Report.execute_query_report(report, dict(filters))
	return Report.execute_script_report(report, frappe._dict(filters))


class TestN1AnalyticsAccessGate(FrappeTestCase):
	def setUp(self):
		from ury.ury import analytics_access

		self.aa = analytics_access
		if not frappe.db.exists("Role", "Insights User"):
			frappe.get_doc({"doctype": "Role", "role_name": "Insights User", "desk_access": 1}).insert(
				ignore_permissions=True
			)
		self.installed = patch.object(analytics_access, "insights_installed", return_value=True)
		self.installed.start()
		self.addCleanup(self.installed.stop)
		frappe.local.conf.pop(ANALYTICS_GATE, None)

	def _new_manager(self, email):
		frappe.db.delete("User", {"name": email})
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "N1",
				"send_welcome_email": 0,
				"user_type": "System User",
				"roles": [{"role": "URY Manager"}],
			}
		)
		user.insert(ignore_permissions=True)
		return user

	def _roles(self, email):
		return set(frappe.get_all("Has Role", filters={"parent": email, "parenttype": "User"}, pluck="role"))

	def test_user_save_adds_no_insights_role_when_gate_absent(self):
		user = self._new_manager("n1-off@example.invalid")
		user.save(ignore_permissions=True)
		self.assertNotIn("Insights User", self._roles(user.name))

	def test_user_save_adds_insights_role_when_gate_set(self):
		with patch.dict(frappe.local.conf, {ANALYTICS_GATE: 1}):
			user = self._new_manager("n1-on@example.invalid")
		self.assertIn("Insights User", self._roles(user.name))

	def test_apply_grants_nothing_when_gate_absent(self):
		self._new_manager("n1-apply@example.invalid")
		before = frappe.db.count("Has Role", {"role": "Insights User"})
		marker = frappe.db.get_default(self.aa.MARKER_KEY)
		result = self.aa.apply()
		self.assertTrue(result.get("skipped"), result)
		self.assertEqual(frappe.db.count("Has Role", {"role": "Insights User"}), before)
		self.assertEqual(frappe.db.get_default(self.aa.MARKER_KEY), marker)


class TestN2NoReportOverride(FrappeTestCase):
	def test_report_hook_is_not_overridden(self):
		self.assertNotIn("Report", frappe.get_hooks("override_doctype_class"))

	def test_report_resolves_to_native_class(self):
		name = frappe.get_all("Report", filters={"module": ["in", _ury_modules()]}, pluck="name", limit=1)[0]
		self.assertIs(type(frappe.get_doc("Report", name)), Report)

	def test_each_ury_report_runs_with_blank_optional_filters(self):
		names = set(
			frappe.get_all(
				"Report",
				filters={"module": ["in", _ury_modules()], "report_type": "Query Report", "is_standard": "Yes"},
				pluck="name",
			)
		)
		names |= REPORT_LOCAL_DEFAULTS
		self.assertGreater(len(names), 40)
		failures = []
		for name in sorted(names - KNOWN_BROKEN_REPORTS):
			report = frappe.get_doc("Report", name)
			try:
				_run_natively(report, _mandatory_only_filters(report))
			except Exception as exc:
				failures.append(f"{name}: {exc!r}"[:200])
		self.assertEqual(failures, [])

	def test_known_broken_reports_still_fail(self):
		for name in KNOWN_BROKEN_REPORTS:
			report = frappe.get_doc("Report", name)
			with self.assertRaises(Exception, msg=f"{name} now runs: remove it from KNOWN_BROKEN_REPORTS"):
				_run_natively(report, _mandatory_only_filters(report))


class TestN3MigrateKeepsSetupSidebar(FrappeTestCase):
	def test_after_migrate_does_not_hide_setup_sidebar(self):
		if not frappe.db.table_exists("Sidebar Item"):
			self.fail("CANNOT-VERIFY: this Frappe has no Sidebar Item table")
		present = frappe.get_all(
			"Sidebar Item", filters={"parent": "Setup", "label": ["in", SIDEBAR_ITEMS]}, pluck="label"
		)
		self.assertEqual(sorted(present), sorted(SIDEBAR_ITEMS))
		frappe.db.sql(
			"UPDATE `tabSidebar Item` SET hidden = 0 WHERE parent = 'Setup' AND label IN %s", (SIDEBAR_ITEMS,)
		)
		for method in frappe.get_hooks("after_migrate", app_name="ury"):
			frappe.get_attr(method)()
		hidden = frappe.get_all(
			"Sidebar Item",
			filters={"parent": "Setup", "label": ["in", SIDEBAR_ITEMS], "hidden": 1},
			pluck="label",
		)
		self.assertEqual(hidden, [])


class TestN4StockReconciliationDraftGuard(FrappeTestCase):
	def setUp(self):
		from ury.ury.utils import stock_reconciliation_guards

		self.guard = stock_reconciliation_guards
		frappe.local.conf.pop(SR_GATE, None)
		self.doc = frappe._dict(name="MAT-RECO-NEW", branch="N4 Branch", doctype="Stock Reconciliation")
		self.other_draft = patch.object(frappe.db, "exists", return_value="MAT-RECO-OTHER")
		self.other_draft.start()
		self.addCleanup(self.other_draft.stop)

	def test_second_branch_draft_allowed_when_gate_absent(self):
		self.guard.validate(self.doc, "validate")

	def test_second_branch_draft_refused_when_gate_set(self):
		with patch.dict(frappe.local.conf, {SR_GATE: 1}):
			with self.assertRaises(frappe.ValidationError):
				self.guard.validate(self.doc, "validate")


class TestN5RolePermissionsCarried(FrappeTestCase):
	def test_role_permissions_module_is_carried(self):
		self.assertIsNotNone(
			importlib.util.find_spec("ury.role_permissions"),
			"ury/role_permissions.py is cited by the siteops DocPerm composer and must exist",
		)

	def test_fresh_install_grants_ury_admin_rows(self):
		from ury import install

		install.ensure_role_permissions()
		row = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": "Purchase Order", "role": "URY Admin", "permlevel": 0},
			["read", "write", "submit", "cancel"],
			as_dict=True,
		)
		self.assertIsNotNone(row, "URY Admin has no Purchase Order Custom DocPerm after install")
		self.assertEqual((row.read, row.write, row.submit, row.cancel), (1, 1, 1, 1))

	def test_every_role_permissions_row_exists_after_install(self):
		self.assertIsNotNone(importlib.util.find_spec("ury.role_permissions"), "ury.role_permissions missing")
		from ury import install, role_permissions

		install.ensure_role_permissions()
		missing = []
		for role, rows in role_permissions.permissions_map.items():
			for doctype, perms in rows:
				if not frappe.db.exists("DocType", doctype):
					continue
				got = frappe.db.get_value(
					"Custom DocPerm",
					{"parent": doctype, "role": role, "permlevel": perms.get("permlevel", 0)},
					"*",
					as_dict=True,
				)
				if not got:
					missing.append((role, doctype, "row"))
					continue
				for ptype, value in perms.items():
					if ptype in role_permissions.PERMISSION_KEYS and int(got.get(ptype) or 0) != value:
						missing.append((role, doctype, ptype))
		self.assertEqual(missing, [])

	def test_admin_permissions_patch_line_kept(self):
		path = os.path.join(frappe.get_app_path("ury"), "patches.txt")
		with open(path) as fh:
			lines = [line.split("#")[0].strip() for line in fh]
		self.assertIn("ury.patches.v2_0.admin_permissions", lines)
		module = importlib.import_module("ury.patches.v2_0.admin_permissions")
		self.assertTrue(callable(getattr(module, "execute", None)))
