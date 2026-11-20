# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt
"""Call waiter on version-16: staff of the request's branch hear about a guest
request at once (per-user realtime), only they can list, acknowledge or resolve
it, the lifecycle is Open -> Acknowledged -> Resolved with who and when
recorded, history is never deleted, and the alert's sound and escalation
minutes come from the branch POS Profile. Upstream's ServiceRequestPanel stays.

Site-less: frappe calls are patched.
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
		# A loaded Frappe document carries every field, empty ones as None.
		"acknowledged_by": None,
		"acknowledged_at": None,
		"resolved_by": None,
		"resolved_at": None,
	}
	defaults.update(values)
	doc.__dict__.update(defaults)
	return doc


def _match(value, condition):
	"""One Frappe filter condition: a plain value, or ["in" | "not in" | "!=", operand]."""
	if isinstance(condition, (list, tuple)) and len(condition) == 2:
		operator, operand = condition
		if operator == "in":
			return value in operand
		if operator == "not in":
			return value not in operand
		if operator == "!=":
			return value != operand
	return value == condition


class _Rows:
	"""frappe.get_all over in-memory rows: equality and ["in", [...]] filters, pluck."""

	def __init__(self, tables):
		self.tables = tables

	def __call__(self, doctype, filters=None, pluck=None, **kwargs):
		rows = [row for row in self.tables.get(doctype, [])
			if all(_match(row.get(key), condition) for key, condition in (filters or {}).items())]
		return [row[pluck] for row in rows] if pluck else rows


def _branch_people():
	"""Two branches. Only enabled floor/till staff of a branch may hear its guests."""
	members = (
		("Tankhill Muyenga", "captain@a.test", "URY Captain", 1),
		("Tankhill Muyenga", "cashier@a.test", "URY Cashier", 1),
		("Tankhill Muyenga", "manager@a.test", "URY Manager", 1),
		("Tankhill Muyenga", "cook@a.test", "Kitchen Display", 1),
		("Tankhill Muyenga", "former@a.test", "URY Captain", 0),
		("Salama Kampala", "captain@b.test", "URY Captain", 1),
	)
	return _Rows({
		"URY User": [{"parent": b, "parenttype": "Branch", "user": u} for b, u, _, _ in members],
		"Has Role": [{"parent": u, "parenttype": "User", "role": r} for _, u, r, _ in members]
			+ [{"parent": "cashier@a.test", "parenttype": "User", "role": "URY Captain"}],
		"User": [{"name": u, "enabled": e} for _, u, _, e in members],
	})


class TestServiceRequestRealtime(unittest.TestCase):
	"""(b) Every create, acknowledge and resolve reaches the branch's own staff, one user room each."""

	def setUp(self):
		self.publish = self.enterContext(patch(f"{DOCTYPE_MOD}.frappe.publish_realtime"))
		self.table_branch = "Tankhill Muyenga"
		self.branch = self.enterContext(
			patch(f"{DOCTYPE_MOD}.frappe.db.get_value", side_effect=lambda *a, **k: self.table_branch)
		)
		self.enterContext(patch(f"{DOCTYPE_MOD}.frappe.get_all", _branch_people()))

	def recipients(self):
		return sorted(c.kwargs.get("user") for c in self.publish.call_args_list)

	def publish_for(self, **values):
		doc = _request(**values)
		hook = getattr(doc, "on_update", None)
		self.assertTrue(callable(hook), "URY Service Request does not publish anything when it is created")
		hook()

	def test_creating_a_request_publishes_to_each_branch_staff_user_after_commit(self):
		self.publish_for()
		self.assertEqual(self.recipients(), ["captain@a.test", "cashier@a.test", "manager@a.test"],
			"Only enabled Captain/Cashier/Manager users of the table's branch may hear it, once each")
		for call in self.publish.call_args_list:
			args, kwargs = call
			self.assertEqual(kwargs.get("event", args[0] if args else None), "ury_service_request")
			self.assertIsNone(kwargs.get("room"),
				"Never a shared room: doctype:URY Service Request admits every reader of every branch")
			self.assertIs(kwargs.get("after_commit"), True)
			self.assertEqual(
				kwargs.get("message"),
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

	def test_a_user_of_another_branch_receives_nothing(self):
		self.publish_for()
		self.assertNotIn("captain@b.test", self.recipients())
		self.publish.reset_mock()
		self.table_branch = "Salama Kampala"
		self.publish_for(table="S-1")
		self.assertEqual(self.recipients(), ["captain@b.test"])

	def test_no_branch_means_no_one_is_told(self):
		self.table_branch = None
		self.publish_for()
		self.publish.assert_not_called()

	def test_acknowledge_and_resolve_also_publish_so_banners_clear_everywhere(self):
		for status in ("Acknowledged", "Resolved"):
			with self.subTest(status=status):
				self.publish.reset_mock()
				self.publish_for(status=status)
				self.assertTrue(self.publish.call_args_list, "No realtime publish on status change")
				for call in self.publish.call_args_list:
					self.assertEqual(call.kwargs["message"]["status"], status)


class TestStaffActionsAreBranchScoped(unittest.TestCase):
	"""B2: list, acknowledge and resolve: staff of the request's branch only, like the settings endpoint."""

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
		self.enterContext(patch(f"{STAFF_MOD}.now_datetime", return_value=datetime.datetime(2026, 11, 20, 15, 0)))
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

	def test_open_list_of_another_branch_is_refused_loudly(self):
		self.user_branches = ["Branch A"]
		with self.assertRaises(frappe.PermissionError) as caught:
			self.api.list_open_service_requests("Branch B")
		self.assertIn("URY_SERVICE_REQUEST_OTHER_BRANCH", str(caught.exception))

	def test_acknowledge_never_reopens_a_resolved_request(self):
		doc = _request(status="Resolved", resolved_by="captain@a.test",
			resolved_at=datetime.datetime(2026, 11, 20, 15, 1))

		def save():
			# Frappe loads the stored row before validate; the stored row is Resolved.
			doc._doc_before_save = {"status": "Resolved", "resolved_by": "captain@a.test",
				"resolved_at": datetime.datetime(2026, 11, 20, 15, 1)}
			doc.validate()

		doc.save = save
		with patch(f"{STAFF_MOD}.frappe.get_doc", return_value=doc), \
				patch(f"{DOCTYPE_MOD}.frappe.session", SimpleNamespace(user="waiter@a.test")):
			with self.assertRaises(frappe.ValidationError) as caught:
				self.api.acknowledge_service_request("SR-1")
		self.assertIn("URY_SERVICE_REQUEST_ALREADY_RESOLVED", str(caught.exception))
		self.assertNotIsInstance(caught.exception, frappe.PermissionError)

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
				self.assertTrue(name in fields, f"{name} is not in fixtures/custom_field.json")
				self.assertEqual(fields[name]["fieldtype"], fieldtype)
				self.assertEqual(str(fields[name]["default"]), default)
				named = [f for entry in hooks.fixtures if isinstance(entry, dict) and entry.get("doctype") == "Custom Field"
					for flt in entry.get("filters", []) for f in flt[2]]
				self.assertIn(name, named, "Fixture row is not named in hooks.fixtures, so migrate drops it")


class TestLifecycleAndAudit(unittest.TestCase):
	"""Open -> Acknowledged -> Resolved, forward only, with who and when recorded server-side."""

	def setUp(self):
		self.when = datetime.datetime(2026, 11, 20, 15, 0)
		self.enterContext(patch(f"{DOCTYPE_MOD}.frappe.session", SimpleNamespace(user="waiter@a.test")))
		self.enterContext(patch(f"{DOCTYPE_MOD}.frappe.utils.now_datetime", return_value=self.when))

	def save(self, previous, **values):
		doc = _request(**values)
		doc._doc_before_save = previous
		doc.validate()
		return doc

	def test_acknowledging_records_who_and_when(self):
		doc = self.save({"status": "Open"}, status="Acknowledged")
		self.assertEqual(getattr(doc, "acknowledged_by", None), "waiter@a.test")
		self.assertEqual(getattr(doc, "acknowledged_at", None), self.when)
		self.assertIsNone(getattr(doc, "resolved_by", None))

	def test_resolving_records_who_and_when(self):
		doc = self.save({"status": "Acknowledged", "acknowledged_by": "captain@a.test"}, status="Resolved",
			acknowledged_by="captain@a.test")
		self.assertEqual(doc.resolved_by, "waiter@a.test")
		self.assertEqual(doc.resolved_at, self.when)
		self.assertEqual(doc.acknowledged_by, "captain@a.test")

	def test_audit_values_cannot_be_rewritten_once_set(self):
		earlier = datetime.datetime(2026, 11, 20, 14, 57)
		previous = {"status": "Acknowledged", "acknowledged_by": "captain@a.test", "acknowledged_at": earlier}
		doc = self.save(previous, status="Resolved", acknowledged_by="someone@else.test",
			acknowledged_at=self.when)
		self.assertEqual(doc.acknowledged_by, "captain@a.test")
		self.assertEqual(doc.acknowledged_at, earlier)

	def test_a_resolved_request_cannot_be_acknowledged_or_reopened(self):
		for status in ("Acknowledged", "Open"):
			with self.subTest(status=status):
				with self.assertRaises(frappe.ValidationError) as caught:
					self.save({"status": "Resolved"}, status=status)
				self.assertIn("URY_SERVICE_REQUEST_ALREADY_RESOLVED", str(caught.exception))
				self.assertNotIsInstance(caught.exception, frappe.PermissionError)

	def test_an_acknowledged_request_cannot_go_back_to_open(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.save({"status": "Acknowledged"}, status="Open")
		self.assertIn("URY_SERVICE_REQUEST_STATUS_BACKWARD", str(caught.exception))

	def test_audit_fields_exist_read_only_on_the_doctype(self):
		import json
		from pathlib import Path

		import ury

		meta = json.loads((Path(ury.__file__).parent / "ury" / "doctype" / "ury_service_request"
			/ "ury_service_request.json").read_text())
		fields = {f["fieldname"]: f for f in meta["fields"]}
		for name, fieldtype in (("acknowledged_by", "Link"), ("acknowledged_at", "Datetime"),
				("resolved_by", "Link"), ("resolved_at", "Datetime")):
			with self.subTest(name=name):
				self.assertIn(name, fields)
				self.assertIn(name, meta["field_order"])
				self.assertEqual(fields[name]["fieldtype"], fieldtype)
				self.assertEqual(fields[name].get("read_only"), 1)
		self.assertEqual(fields["acknowledged_by"].get("options"), "User")


class _MemoryCache:
	"""The four frappe.cache calls frappe.rate_limiter makes, kept in memory."""

	def __init__(self):
		self.store = {}

	def make_key(self, key, user=None, shared=False):
		return f"unit|{key}".encode()

	def get(self, key):
		return self.store.get(key)

	def setex(self, key, seconds, value):
		self.store[key] = value

	def incrby(self, key, amount):
		self.store[key] = int(self.store.get(key) or 0) + amount
		return self.store[key]


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

	def guest(self, name):
		method = getattr(self.api, name, None)
		self.assertTrue(callable(method), f"Guests have no {name} endpoint")
		return method

	def test_one_open_assistance_per_table_whatever_the_check(self):
		self.exists.return_value = "REQ-OPEN"
		self.assertEqual(self.guest("request_assistance")("opaque"), {"status": "Already Requested", "request": "REQ-OPEN"})
		self.exists.assert_called_once_with(
			"URY Service Request",
			{"table": "T-2", "request_type": "Assistance", "status": "Open"},
		)
		self.req.insert.assert_not_called()

	def test_a_later_call_after_an_acknowledged_one_alerts_again(self):
		# Only an Open call suppresses a new one: once staff acknowledged the first
		# call, the guest calling again is a new request that must ring again.
		stored = {"REQ-ACK": {"table": "T-2", "request_type": "Assistance", "status": "Acknowledged"}}
		self.exists.side_effect = lambda doctype, filters: next(
			(name for name, row in stored.items() if all(_match(row.get(k), v) for k, v in filters.items())), None)
		self.assertEqual(self.guest("request_assistance")("opaque"), {"status": "Requested", "request": "REQ-NEW"})
		self.req.insert.assert_called_once_with(ignore_permissions=True)

	def test_bill_requests_also_dedupe_against_open_only(self):
		self.exists.return_value = "REQ-OPEN"
		self.assertEqual(self.api.request_bill("opaque")["status"], "Already Requested")
		self.exists.assert_called_once_with(
			"URY Service Request",
			{"table": "T-2", "invoice": "POS-1", "request_type": "Bill", "status": "Open"},
		)

	def test_request_is_created_for_the_sessions_own_table_only(self):
		self.guest("request_assistance")("opaque")
		payload = self.get_doc.call_args.args[0]
		self.assertEqual(payload["table"], "T-2")
		self.assertEqual(payload["session"], "SESSION-1")
		self.req.insert.assert_called_once_with(ignore_permissions=True)

	def test_guest_endpoints_accept_only_the_session_token(self):
		import inspect

		for name in ("request_assistance", "request_bill"):
			with self.subTest(name=name):
				self.assertEqual(list(inspect.signature(self.guest(name)).parameters), ["session"])

	def test_guest_requests_are_rate_limited(self):
		self.enterContext(patch.object(frappe, "cache", _MemoryCache()))
		frappe.local.request = SimpleNamespace(method="POST")
		self.addCleanup(setattr, frappe.local, "request", None)
		frappe.local.request_ip = "203.0.113.9"
		for name in ("request_assistance", "request_bill"):
			with self.subTest(name=name):
				frappe.local.form_dict = frappe._dict(cmd=f"{GUEST_MOD}.{name}", session="opaque")
				fn = self.guest(name)
				for _ in range(5):
					fn("opaque")
				with self.assertRaises(frappe.RateLimitExceededError):
					fn("opaque")


class TestServiceRequestHistoryIsKept(unittest.TestCase):
	"""Simphony keeps service-request history: no deletion, and no delete grant beyond System Manager."""

	def test_deleting_any_request_is_refused_with_a_stable_id(self):
		for status in ("Open", "Acknowledged", "Resolved"):
			with self.subTest(status=status):
				doc = _request(status=status)
				hook = getattr(doc, "on_trash", None)
				self.assertTrue(callable(hook), "Nothing stops a service request from being deleted")
				with self.assertRaises(frappe.ValidationError) as caught:
					hook()
				self.assertIn("URY_SERVICE_REQUEST_DELETE_REFUSED", str(caught.exception))

	def test_only_system_manager_holds_delete(self):
		import json
		from pathlib import Path

		import ury

		meta = json.loads((Path(ury.__file__).parent / "ury" / "doctype" / "ury_service_request"
			/ "ury_service_request.json").read_text())
		self.assertEqual(sorted(p["role"] for p in meta["permissions"] if p.get("delete")), ["System Manager"])


if __name__ == "__main__":
	unittest.main()
