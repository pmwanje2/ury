# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt
"""Call waiter on version-16: staff hear about a guest request at once
(realtime), and the alert's sound and escalation minutes come from the
branch POS Profile. Upstream's ServiceRequestPanel and staff API stay as
they are.

Site-less: frappe calls are patched.
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

DOCTYPE_MOD = "ury.ury.doctype.ury_service_request.ury_service_request"
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


if __name__ == "__main__":
	unittest.main()
