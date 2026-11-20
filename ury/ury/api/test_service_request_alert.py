# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt
"""Call waiter: staff hear about a guest request at once, only staff of that
branch can acknowledge or resolve it, and a guest can only open one request
of each kind for its own table, at a bounded rate.

Site-less: frappe calls are patched, matching test_self_ordering.py.
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

DOCTYPE_MOD = "ury.ury.doctype.ury_service_request.ury_service_request"
GUEST_MOD = "ury.ury.api.self_ordering"
STAFF_MOD = "ury.ury.api.service_requests"


def _request(**values):
	from ury.ury.doctype.ury_service_request.ury_service_request import URYServiceRequest

	doc = URYServiceRequest.__new__(URYServiceRequest)
	defaults = {
		"doctype": "URY Service Request",
		"name": "SR-1",
		"request_type": "Assistance",
		"table": "T-2",
		"status": "Open",
		"requested_at": datetime.datetime(2026, 11, 20, 14, 56, 0),
	}
	defaults.update(values)
	doc.__dict__.update(defaults)
	return doc


class TestServiceRequestRealtime(unittest.TestCase):
	"""(b) Every create, acknowledge and resolve reaches open staff screens."""

	def setUp(self):
		self.publish = self.enterContext(patch(f"{DOCTYPE_MOD}.frappe.publish_realtime"))
		self.branch = self.enterContext(
			patch(f"{DOCTYPE_MOD}.frappe.db.get_value", return_value="Tankhill Muyenga")
		)

	def test_creating_a_request_publishes_to_staff_after_commit(self):
		doc = _request()
		hook = getattr(doc, "on_update", None)
		self.assertTrue(callable(hook), "URY Service Request does not publish anything when it is created")
		hook()
		self.publish.assert_called_once()
		args, kwargs = self.publish.call_args
		event = kwargs.get("event", args[0] if args else None)
		self.assertEqual(event, "ury_service_request")
		self.assertEqual(kwargs.get("doctype"), "URY Service Request")
		self.assertNotIn("user", kwargs)
		self.assertIs(kwargs.get("after_commit"), True)
		self.assertEqual(
			kwargs.get("room"),
			"doctype:URY Service Request",
			"The event must go to the permission-checked doctype room, never the site or website room",
		)
		message = kwargs.get("message", args[1] if len(args) > 1 else None)
		self.assertEqual(
			message,
			{
				"name": "SR-1",
				"table": "T-2",
				"branch": "Tankhill Muyenga",
				"request_type": "Assistance",
				"status": "Open",
				"requested_at": "2026-11-20 14:56:00",
			},
		)
		self.branch.assert_called_once_with("URY Table", "T-2", "branch")

	def test_acknowledge_and_resolve_also_publish_so_banners_clear_everywhere(self):
		for status in ("Acknowledged", "Resolved"):
			with self.subTest(status=status):
				self.publish.reset_mock()
				doc = _request(status=status)
				hook = getattr(doc, "on_update", None)
				self.assertTrue(callable(hook), "No realtime publish on status change")
				hook()
				message = self.publish.call_args.kwargs["message"]
				self.assertEqual(message["status"], status)


class TestStaffActionsAreBranchScoped(unittest.TestCase):
	"""Acknowledge/resolve: staff of the request's branch only."""

	def setUp(self):
		try:
			from ury.ury.api import service_requests
		except ImportError:
			self.fail("Staff service-request API (upstream ury/ury/api/service_requests.py) is missing")
		self.api = service_requests
		self.doc = MagicMock(table="T-2", status="Open")
		self.doc.name = "SR-1"
		self.enterContext(patch(f"{STAFF_MOD}.frappe.get_doc", return_value=self.doc))
		self.enterContext(patch(f"{STAFF_MOD}.frappe.has_permission", return_value=True))
		self.roles = self.enterContext(patch(f"{STAFF_MOD}.frappe.get_roles", return_value=["URY Captain"]))
		self.session = self.enterContext(patch(f"{STAFF_MOD}.frappe.session", SimpleNamespace(user="waiter@a.test")))
		self.table_branch = "Branch A"
		self.user_branches = ["Branch A"]
		self.enterContext(
			patch(f"{STAFF_MOD}.frappe.db.get_value", side_effect=lambda *a, **k: self.table_branch)
		)
		self.enterContext(
			patch(f"{STAFF_MOD}.frappe.get_all", side_effect=lambda doctype, **kw: list(self.user_branches)
				if doctype == "URY User" else [])
		)

	def test_staff_of_the_branch_may_acknowledge_and_resolve(self):
		self.assertEqual(self.api.acknowledge_service_request("SR-1")["status"], "Acknowledged")
		self.assertEqual(self.api.resolve_service_request("SR-1")["status"], "Resolved")

	def test_staff_of_another_branch_are_refused_with_a_stable_id(self):
		self.user_branches = ["Branch B"]
		for action in ("acknowledge_service_request", "resolve_service_request"):
			with self.subTest(action=action):
				self.doc.save.reset_mock()
				with self.assertRaises(frappe.PermissionError) as caught:
					getattr(self.api, action)("SR-1")
				self.assertIn("URY_SERVICE_REQUEST_OTHER_BRANCH", str(caught.exception))
				self.doc.save.assert_not_called()

	def test_staff_with_no_branch_are_refused(self):
		self.user_branches = []
		with self.assertRaises(frappe.PermissionError):
			self.api.acknowledge_service_request("SR-1")
		self.doc.save.assert_not_called()

	def test_system_manager_is_not_branch_bound(self):
		self.user_branches = []
		self.roles.return_value = ["System Manager"]
		self.assertEqual(self.api.acknowledge_service_request("SR-1")["status"], "Acknowledged")

	def test_guest_session_cannot_acknowledge(self):
		self.session.user = "Guest"
		self.roles.return_value = ["Guest"]
		self.user_branches = []
		with self.assertRaises(frappe.PermissionError):
			self.api.acknowledge_service_request("SR-1")
		self.doc.save.assert_not_called()

	def test_open_list_is_limited_to_the_users_branch(self):
		self.user_branches = ["Branch A"]
		with patch(f"{STAFF_MOD}.frappe.get_all") as get_all:
			get_all.side_effect = lambda doctype, **kw: (["Branch A"] if doctype == "URY User" else [])
			self.assertEqual(self.api.list_open_service_requests("Branch B"), [])

	def test_staff_endpoints_are_not_guest_methods(self):
		for name in ("list_open_service_requests", "acknowledge_service_request", "resolve_service_request",
				"get_service_request_alert_settings"):
			fn = getattr(self.api, name, None)
			self.assertTrue(callable(fn), name)
			self.assertNotIn(fn, frappe.guest_methods, name)


class TestAlertSettingsOnTheProfile(unittest.TestCase):
	"""(c) Sound on/off and escalation minutes live on the branch POS Profile."""

	def setUp(self):
		from ury.ury.api import service_requests

		self.api = service_requests
		self.enterContext(patch(f"{STAFF_MOD}.frappe.get_roles", return_value=["URY Cashier"]))
		self.enterContext(patch(f"{STAFF_MOD}.frappe.session", SimpleNamespace(user="cashier@a.test")))
		self.enterContext(patch(f"{STAFF_MOD}.frappe.get_all",
			side_effect=lambda doctype, **kw: ["Branch A"] if doctype == "URY User" else []))
		self.values = None
		self.enterContext(patch(f"{STAFF_MOD}.frappe.db.get_value", side_effect=lambda *a, **k: self.values))

	def reader(self):
		fn = getattr(self.api, "get_service_request_alert_settings", None)
		self.assertTrue(callable(fn), "No profile settings for the staff alert")
		return fn

	def test_defaults_are_sound_on_and_three_minutes(self):
		self.values = None
		self.assertEqual(self.reader()("Branch A"), {"sound": 1, "escalate_minutes": 3})

	def test_profile_values_are_used(self):
		self.values = {"custom_service_request_sound": 0, "custom_service_request_escalate_minutes": 5}
		self.assertEqual(self.reader()("Branch A"), {"sound": 0, "escalate_minutes": 5})

	def test_zero_or_negative_minutes_fall_back_to_three(self):
		for value in (0, -2, None):
			with self.subTest(value=value):
				self.values = {"custom_service_request_sound": 1, "custom_service_request_escalate_minutes": value}
				self.assertEqual(self.reader()("Branch A")["escalate_minutes"], 3)

	def test_other_branch_settings_are_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self.reader()("Branch B")

	def test_fields_ship_in_the_fixture_and_its_filter(self):
		import json
		from pathlib import Path

		import ury
		import ury.hooks as hooks

		root = Path(ury.__file__).parent
		fields = {row["name"]: row for row in json.loads((root / "fixtures" / "custom_field.json").read_text())}
		for name, fieldtype, default in (
			("POS Profile-custom_service_request_sound", "Check", "1"),
			("POS Profile-custom_service_request_escalate_minutes", "Int", "3"),
		):
			with self.subTest(name=name):
				self.assertIn(name, fields)
				self.assertEqual(fields[name]["fieldtype"], fieldtype)
				self.assertEqual(str(fields[name]["default"]), default)
				named = [f for entry in hooks.fixtures if entry.get("doctype") == "Custom Field"
					for flt in entry.get("filters", []) for f in flt[2]]
				self.assertIn(name, named, "Fixture row is not named in hooks.fixtures, so migrate drops it")


class TestGuestRequestLimits(unittest.TestCase):
	"""A guest session can only create, for its own table, once per kind, at a bounded rate."""

	def setUp(self):
		from ury.ury.api import self_ordering

		self.api = self_ordering
		self.session = MagicMock(table="T-2", invoice="POS-1", ordering_profile="Profile A")
		self.session.name = "SESSION-1"
		self.profile = MagicMock(enable_request_bill=1)
		self.enterContext(patch(f"{GUEST_MOD}._resolve_session", return_value=self.session))
		self.get_doc = self.enterContext(patch(f"{GUEST_MOD}.frappe.get_doc"))
		self.exists = self.enterContext(patch(f"{GUEST_MOD}.frappe.db.exists", return_value=None))
		self.enterContext(patch(f"{GUEST_MOD}.frappe.set_user"))
		self.enterContext(patch(f"{GUEST_MOD}.now_datetime", return_value=datetime.datetime(2026, 11, 20, 14, 56)))
		self.req = MagicMock()
		self.req.name = "REQ-NEW"
		self.get_doc.side_effect = lambda doctype, name=None: self.req if isinstance(doctype, dict) else self.profile

	def test_one_open_assistance_per_table_whatever_the_check(self):
		self.exists.return_value = "REQ-OPEN"
		self.assertEqual(self.api.request_assistance("opaque"), {"status": "Already Requested", "request": "REQ-OPEN"})
		self.exists.assert_called_once_with(
			"URY Service Request",
			{"table": "T-2", "request_type": "Assistance", "status": ["!=", "Resolved"]},
		)
		self.req.insert.assert_not_called()

	def test_request_is_created_for_the_sessions_own_table_only(self):
		self.api.request_assistance("opaque")
		payload = self.get_doc.call_args.args[0]
		self.assertEqual(payload["table"], "T-2")
		self.assertEqual(payload["session"], "SESSION-1")
		self.req.insert.assert_called_once_with(ignore_permissions=True)

	def test_guest_endpoints_accept_only_the_session_token(self):
		import inspect

		for name in ("request_assistance", "request_bill"):
			with self.subTest(name=name):
				fn = getattr(self.api, name)
				self.assertEqual(list(inspect.signature(fn).parameters), ["session"])

	def test_guest_requests_are_rate_limited(self):
		from ury.ury.api.test_guest_feedback import _MemoryCache

		self.enterContext(patch.object(frappe, "cache", _MemoryCache()))
		frappe.local.request = SimpleNamespace(method="POST")
		self.addCleanup(setattr, frappe.local, "request", None)
		frappe.local.request_ip = "203.0.113.9"
		for name in ("request_assistance", "request_bill"):
			with self.subTest(name=name):
				frappe.local.form_dict = frappe._dict(cmd=f"{GUEST_MOD}.{name}", session="opaque")
				fn = getattr(self.api, name)
				for _ in range(5):
					fn("opaque")
				with self.assertRaises(frappe.RateLimitExceededError):
					fn("opaque")


if __name__ == "__main__":
	unittest.main()
