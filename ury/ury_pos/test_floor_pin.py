"""Site-less contracts for the paired floor tablet PIN boundary."""

import hashlib
import hmac
import importlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


class Refused(Exception):
	pass


class UniqueCollision(Exception):
	pass


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


class TestFloorPin(unittest.TestCase):
	def setUp(self):
		self.clock = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
		self.counts = {}
		self.tracker_options = []
		case = self

		class Tracker:
			def __init__(self, key, max_consecutive_login_attempts=3, lock_interval=300):
				self.key, self.maximum, self.interval = key, max_consecutive_login_attempts, lock_interval
				case.tracker_options.append((key, self.maximum, self.interval))

			def is_user_allowed(self):
				start, count = case.counts.get(self.key, (case.clock, 0))
				return not (start + timedelta(seconds=self.interval) > case.clock and count > self.maximum)

			def add_failure_attempt(self):
				start, count = case.counts.get(self.key, (case.clock, 0))
				if start + timedelta(seconds=self.interval) <= case.clock:
					start, count = case.clock, 0
				case.counts[self.key] = (start, count + 1)

			def add_success_attempt(self):
				case.counts.pop(self.key, None)

		self.frappe = MagicMock()
		self.frappe.whitelist.side_effect = lambda **kw: lambda fn: fn
		self.frappe.throw.side_effect = lambda message, *a, **kw: (_ for _ in ()).throw(Refused(message))
		self.frappe.PermissionError = Refused
		self.frappe.AuthenticationError = Refused
		self.frappe.UniqueValidationError = UniqueCollision
		self.frappe.session.user = "manager@example.test"
		self.frappe.form_dict = {}
		self.frappe.request.cookies = {}
		self.profile = Doc(name="Salama POS", branch="Salama", disabled=0, custom_floor_pin_login=1,
			custom_floor_device_epoch=0, custom_floor_pin_roles=[Doc(role="URY Captain")],
			custom_floor_stay_signed_in_roles=[Doc(role="URY Manager")])
		self.profile.add_comment = MagicMock()
		self.frappe.get_doc.return_value = self.profile
		self.frappe.get_all.return_value = [Doc(name="Salama POS")]
		self.frappe.has_permission.return_value = True
		self.frappe.get_roles.return_value = ["URY Captain"]
		self.frappe.db.exists.return_value = True
		self.user = "waiter@example.test"
		self.frappe.db.get_value.side_effect = lambda dt, filters, field=None, **kw: (
			self.user if isinstance(filters, dict) and filters.get("enabled") == 1 else 1)
		self.frappe.sessions.get_csrf_token.return_value = "fresh-csrf"
		self.frappe.utils.get_fullname.return_value = "Waiter"
		self.rate_limit = MagicMock(side_effect=lambda **kw: lambda fn: fn)
		modules = {"frappe": self.frappe, "frappe.sessions": self.frappe.sessions,
			"frappe.auth": SimpleNamespace(LoginAttemptTracker=Tracker),
			"frappe.rate_limiter": SimpleNamespace(rate_limit=self.rate_limit),
			"frappe.utils": self.frappe.utils,
			"frappe.utils.password": SimpleNamespace(get_encryption_key=lambda: "site-key"),
			"frappe.core.doctype.activity_log.activity_log": SimpleNamespace(add_authentication_log=MagicMock())}
		self.audit = modules["frappe.core.doctype.activity_log.activity_log"].add_authentication_log
		self.enterContext(patch.dict(sys.modules, modules))
		self.module = None

	def api(self):
		path = ROOT / "ury/ury_pos/floor_pin.py"
		self.assertTrue(path.exists(), "Missing paired-tablet floor PIN behavior")
		if self.module is None:
			spec = importlib.util.spec_from_file_location("floor_pin_under_test", path)
			self.module = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(self.module)
		return self.module

	def pair(self):
		api = self.api()
		api.pair_device("Salama POS", "Floor tablet A")
		args, _ = self.frappe.local.cookie_manager.set_cookie.call_args
		self.frappe.request.cookies["ury_floor_device"] = args[1]
		return api

	def sign_in(self, pin="0527"):
		self.frappe.form_dict["pin"] = pin
		return self.api().sign_in(pin)

	def refused(self, code, fn):
		with self.assertRaisesRegex(Refused, code):
			fn()

	def fields(self):
		return {f["name"]: f for f in json.loads((ROOT / "ury/fixtures/custom_field.json").read_text())}

	def test_fixture_declares_user_floor_pin_hash_unique_hidden_no_copy(self):
		field = self.fields().get("User-custom_floor_pin_hash", {})
		self.assertEqual(field.get("fieldtype"), "Data")
		for flag in ("unique", "hidden", "read_only", "no_copy", "print_hide", "report_hide"):
			self.assertEqual(field.get(flag), 1, flag)
		self.assertEqual(field.get("permlevel"), 0)

	def test_fixture_declares_pos_profile_floor_pin_fields(self):
		for name, kind in (("login", "Check"), ("roles", "Table MultiSelect")):
			field = self.fields().get(f"POS Profile-custom_floor_pin_{name}", {})
			self.assertEqual(field.get("fieldtype"), kind)
		field = self.fields().get("POS Profile-custom_floor_device_epoch", {})
		self.assertEqual(field.get("fieldtype"), "Int")
		self.assertEqual(field.get("default"), "0")
		self.assertEqual(self.fields().get("POS Profile-custom_floor_pin_login", {}).get("default"), "0")
		self.assertEqual(self.fields().get("POS Profile-custom_floor_pin_roles", {}).get("options"), "Role Permitted")

	def test_hooks_ship_floor_pin_fields(self):
		from ury import hooks
		fixture = next(row for row in hooks.fixtures if row.get("doctype", row.get("dt")) == "Custom Field")
		names = next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])
		for name in ("User-custom_floor_pin_hash", "POS Profile-custom_floor_pin_login",
			"POS Profile-custom_floor_pin_roles", "POS Profile-custom_floor_device_epoch"):
			self.assertIn(name, names)

	def test_pin_hash_is_keyed_hmac_not_plain_sha256(self):
		api = self.api()
		with patch.object(api.secrets, "randbelow", return_value=527):
			api.issue_pin(self.user)
		stored = self.frappe.db.set_value.call_args.args[3]
		key = hmac.digest(b"site-key", b"ury-floor-pin-v1", "sha256")
		self.assertEqual(stored, hmac.new(key, b"0527", hashlib.sha256).hexdigest())
		self.assertNotEqual(stored, hashlib.sha256(b"0527").hexdigest())

	def test_pair_device_requires_pos_profile_write_and_pin_login_enabled(self):
		api = self.api()
		self.frappe.has_permission.return_value = False
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: api.pair_device("Salama POS", "A"))
		self.frappe.has_permission.return_value = True
		self.profile.custom_floor_pin_login = 0
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: api.pair_device("Salama POS", "A"))
		self.frappe.local.cookie_manager.set_cookie.assert_not_called()

	def test_pair_device_sets_httponly_secure_strict_signed_cookie(self):
		self.pair()
		args, opts = self.frappe.local.cookie_manager.set_cookie.call_args
		self.assertEqual(args[0], "ury_floor_device")
		self.assertTrue(opts["httponly"])
		self.assertTrue(opts["secure"])
		self.assertEqual(opts["samesite"], "Strict")
		self.assertEqual(opts["max_age"], 400 * 86400)
		payload, signature = args[1].rsplit("|", 1)
		key = hmac.digest(b"site-key", b"ury-floor-device-v1", "sha256")
		self.assertEqual(signature, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest())
		self.assertEqual(payload.split("|")[:2], ["v1", "Salama POS"])
		self.assertEqual(len(payload.split("|")[2]), 16)
		self.frappe.has_permission.assert_called_with("POS Profile", "write", self.profile)
		self.assertIn("manager@example.test", str(self.profile.add_comment.call_args))

	def test_issue_pin_requires_pos_profile_write(self):
		self.frappe.has_permission.return_value = False
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: self.api().issue_pin(self.user))
		self.frappe.db.set_value.assert_not_called()

	def test_pin_staff_requires_pos_profile_write(self):
		self.frappe.has_permission.return_value = False
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: self.api().pin_staff())
		self.frappe.db.get_value.assert_not_called()

	def test_pin_staff_lists_only_enabled_eligible_branch_users_with_full_name(self):
		users = [
			"waiter@example.test", "second-waiter@example.test", "disabled@example.test",
			"off-branch@example.test", "manager@example.test", "system@example.test",
		]
		self.frappe.get_all.side_effect = [[Doc(name="Salama POS")], users]
		roles = {
			"waiter@example.test": ["URY Captain"],
			"second-waiter@example.test": ["URY Captain"],
			"disabled@example.test": ["URY Captain"],
			"off-branch@example.test": ["URY Captain"],
			"manager@example.test": ["URY Captain", "URY Manager"],
			"system@example.test": ["URY Captain", "System Manager"],
		}
		full_names = {
			"waiter@example.test": "Amina Waiter",
			"second-waiter@example.test": "Brian Waiter",
		}
		self.frappe.get_roles.side_effect = roles.__getitem__
		self.frappe.utils.get_fullname.side_effect = full_names.__getitem__

		def get_value(doctype, user, field=None, **_kwargs):
			if field == "enabled":
				return user != "disabled@example.test"
			if field == "custom_floor_pin_hash":
				return "digest-for-amina" if user == "waiter@example.test" else None
			return 1

		self.frappe.db.get_value.side_effect = get_value
		self.frappe.db.exists.side_effect = lambda doctype, filters: (
			doctype != "URY User" or filters.get("user") in {"waiter@example.test", "second-waiter@example.test"}
		)

		result = self.api().pin_staff()

		self.assertEqual(result, [
			{"user": "waiter@example.test", "full_name": "Amina Waiter", "has_pin": True},
			{"user": "second-waiter@example.test", "full_name": "Brian Waiter", "has_pin": False},
		])

	def test_pin_staff_has_pin_flag_never_returns_hash(self):
		digest = "digest-for-amina"
		self.frappe.get_all.side_effect = [[Doc(name="Salama POS")], [self.user]]
		self.frappe.utils.get_fullname.return_value = "Amina Waiter"
		self.frappe.db.get_value.side_effect = lambda _doctype, _user, field=None, **_kwargs: (
			digest if field == "custom_floor_pin_hash" else 1
		)
		self.frappe.db.exists.return_value = True

		result = self.api().pin_staff()

		self.assertTrue(result[0]["has_pin"])
		self.assertNotIn(digest, json.dumps(result))

	def test_issue_pin_refuses_target_outside_branch_or_without_floor_role(self):
		api = self.api()
		self.frappe.db.exists.return_value = False
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: api.issue_pin(self.user))
		self.frappe.db.exists.return_value = True
		self.frappe.get_roles.return_value = ["Kitchen User"]
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: api.issue_pin(self.user))
		self.frappe.db.set_value.assert_not_called()

	def test_issue_pin_refuses_stay_signed_in_role_and_system_manager(self):
		for roles in (["URY Captain", "URY Manager"], ["URY Captain", "System Manager"]):
			self.frappe.get_roles.return_value = roles
			self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: self.api().issue_pin(self.user))
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: self.api().issue_pin("Administrator"))
		self.frappe.db.set_value.assert_not_called()

	def test_issue_pin_returns_four_digits_and_stores_only_hmac(self):
		api = self.api()
		with patch.object(api.secrets, "randbelow", return_value=7):
			result = api.issue_pin(self.user)
		self.assertEqual(result, {"pin": "0007"})
		args, kwargs = self.frappe.db.set_value.call_args
		self.assertEqual(args[:3], ("User", self.user, "custom_floor_pin_hash"))
		self.assertEqual(len(args[3]), 64)
		self.assertEqual(kwargs, {"update_modified": False})

	def test_issue_pin_retries_on_unique_collision(self):
		api = self.api()
		self.frappe.db.set_value.side_effect = [UniqueCollision(), None]
		with patch.object(api.secrets, "randbelow", side_effect=[7, 8]):
			self.assertEqual(api.issue_pin(self.user), {"pin": "0008"})
		self.assertEqual(self.frappe.db.set_value.call_count, 2)
		self.frappe.db.set_value.side_effect = UniqueCollision()
		self.frappe.db.set_value.reset_mock()
		self.refused("URY_FLOOR_PIN_NOT_PERMITTED", lambda: api.issue_pin(self.user))
		self.assertEqual(self.frappe.db.set_value.call_count, 20)

	def test_issue_pin_replaces_previous_pin(self):
		api = self.api()
		with patch.object(api.secrets, "randbelow", side_effect=[7, 8]):
			api.issue_pin(self.user)
			api.issue_pin(self.user)
		writes = self.frappe.db.set_value.call_args_list
		self.assertEqual(writes[0].args[:3], writes[1].args[:3])
		self.assertNotEqual(writes[0].args[3], writes[1].args[3])

	def test_issue_pin_does_not_reissue_the_previous_pin(self):
		api = self.api()
		key = hmac.digest(b"site-key", b"ury-floor-pin-v1", "sha256")
		previous = hmac.new(key, b"0007", hashlib.sha256).hexdigest()
		self.frappe.db.get_value.side_effect = lambda dt, user, field=None, **kw: (
			previous if field == "custom_floor_pin_hash" else 1)
		with patch.object(api.secrets, "randbelow", side_effect=[7, 8]):
			self.assertEqual(api.issue_pin(self.user), {"pin": "0008"})
		self.assertEqual(self.frappe.db.set_value.call_count, 1)

	def test_issue_pin_retries_native_database_unique_violation(self):
		# db.set_value bypasses Document's UniqueValidationError translation.
		api = self.api()
		collision = RuntimeError(1062, "Duplicate unique hash")
		self.frappe.db.is_unique_key_violation.side_effect = lambda error: error is collision
		self.frappe.db.set_value.side_effect = [collision, None]
		with patch.object(api.secrets, "randbelow", side_effect=[7, 8]):
			try:
				result = api.issue_pin(self.user)
			except RuntimeError:
				self.fail("Native db.set_value unique collision was not retried")
		self.assertEqual(result, {"pin": "0008"})
		self.frappe.db.set_value.side_effect = RuntimeError("database unavailable")
		with self.assertRaisesRegex(RuntimeError, "database unavailable"):
			api.issue_pin(self.user)

	def test_failed_pin_audit_survives_request_rollback(self):
		self.pair()
		pending, committed = [], []
		self.audit.side_effect = lambda *args, **kwargs: pending.append((args, kwargs))
		self.frappe.db.commit.side_effect = lambda: committed.extend(pending)
		self.refused("URY_FLOOR_PIN_REFUSED", lambda: self.sign_in("bad"))
		pending.clear()  # Frappe rolls back when the authentication exception escapes.
		self.assertEqual(len(committed), 1, "Failed PIN audit must survive request rollback")
		self.assertNotIn("bad", str(committed))

	def test_issue_pin_activity_log_never_contains_pin(self):
		api = self.api()
		with patch.object(api.secrets, "randbelow", return_value=527):
			api.issue_pin(self.user)
		self.audit.assert_called_once()
		self.assertIn("Floor PIN issued", str(self.audit.call_args))
		self.assertIn(self.user, str(self.audit.call_args))
		self.assertNotIn("0527", str(self.audit.call_args))

	def test_sign_in_refuses_without_device_cookie_before_lookup(self):
		self.refused("URY_FLOOR_DEVICE_NOT_PAIRED", self.sign_in)
		self.frappe.db.get_value.assert_not_called()
		self.assertNotIn("pin", self.frappe.form_dict)

	def test_sign_in_refuses_tampered_device_token(self):
		self.pair()
		self.frappe.request.cookies["ury_floor_device"] += "x"
		self.refused("URY_FLOOR_DEVICE_NOT_PAIRED", self.sign_in)
		self.frappe.db.get_value.assert_not_called()

	def test_sign_in_refuses_after_device_epoch_bump(self):
		self.pair()
		self.profile.custom_floor_device_epoch = 1
		self.refused("URY_FLOOR_DEVICE_NOT_PAIRED", self.sign_in)
		self.frappe.db.get_value.assert_not_called()

	def test_sign_in_refuses_when_pin_login_off(self):
		self.pair()
		self.profile.custom_floor_pin_login = 0
		self.refused("URY_FLOOR_DEVICE_NOT_PAIRED", self.sign_in)

	def test_sign_in_refuses_when_no_open_pos_opening_entry(self):
		self.pair()
		self.frappe.db.exists.side_effect = lambda dt, filters: dt != "POS Opening Entry"
		self.refused("URY_FLOOR_TILL_CLOSED", self.sign_in)
		self.frappe.db.exists.assert_called_with("POS Opening Entry", {
			"pos_profile": "Salama POS", "status": "Open", "docstatus": 1})
		self.frappe.db.get_value.assert_not_called()

	def test_sign_in_locks_tablet_after_five_failures_for_five_minutes(self):
		self.pair()
		self.frappe.db.get_value.return_value = None
		self.frappe.db.get_value.side_effect = None
		for _ in range(5):
			self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)
		self.frappe.db.get_value.reset_mock()
		self.refused("URY_FLOOR_PIN_LOCKED", self.sign_in)
		self.frappe.db.get_value.assert_not_called()
		self.clock += timedelta(seconds=299)
		self.refused("URY_FLOOR_PIN_LOCKED", self.sign_in)
		self.clock += timedelta(seconds=1)
		self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)
		self.assertTrue(any(maximum == 4 and seconds == 300 for _, maximum, seconds in self.tracker_options))
		self.pair()
		self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)

	def test_sign_in_day_cap_survives_successes_until_repair(self):
		self.pair()
		for _ in range(20):
			self.refused("URY_FLOOR_PIN_REFUSED", lambda: self.sign_in("bad"))
			if _ < 19:
				self.sign_in()
		self.refused("URY_FLOOR_PIN_LOCKED", self.sign_in)
		self.assertTrue(any(maximum == 19 and seconds == 86400 for _, maximum, seconds in self.tracker_options))
		self.pair()
		self.sign_in()

	def test_sign_in_wrong_pin_is_generic_and_counts_failure(self):
		self.pair()
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.refused("URY_FLOOR_PIN_REFUSED: That PIN didn't work.", self.sign_in)
		self.assertEqual(sorted(count for _, count in self.counts.values()), [1, 1])
		self.assertIn("Floor tablet A", str(self.audit.call_args))
		self.assertIn("Failed", str(self.audit.call_args))
		self.assertNotIn("0527", str(self.audit.call_args))

	def test_sign_in_refuses_disabled_user(self):
		self.pair()
		self.frappe.db.get_value.side_effect = lambda dt, filters, *a, **kw: (
			None if isinstance(filters, dict) and filters.get("enabled") == 1 else self.user)
		self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)
		self.frappe.local.login_manager.login_as.assert_not_called()

	def test_sign_in_refuses_user_outside_branch_or_without_floor_role(self):
		self.pair()
		self.frappe.db.exists.side_effect = lambda dt, filters: dt != "URY User"
		self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)
		self.frappe.db.exists.side_effect = None
		self.frappe.get_roles.return_value = ["Kitchen User"]
		self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)

	def test_sign_in_refuses_stay_signed_in_role_and_system_manager(self):
		self.pair()
		for roles in (["URY Captain", "URY Manager"], ["URY Captain", "System Manager"]):
			self.frappe.get_roles.return_value = roles
			self.refused("URY_FLOOR_PIN_REFUSED", self.sign_in)

	def test_sign_in_calls_login_as_with_twenty_minute_session_end(self):
		self.pair()
		before = datetime.now(timezone.utc)
		self.sign_in()
		args, kwargs = self.frappe.local.login_manager.login_as.call_args
		self.assertEqual(args, (self.user,))
		end = datetime.fromisoformat(kwargs["session_end"])
		self.assertLess(abs((end - before).total_seconds() - 1200), 2)
		self.frappe.sessions.clear_sessions.assert_called_once_with(self.user, keep_current=True)

	def test_sign_in_returns_csrf_token_and_pops_pin_from_form_dict(self):
		self.pair()
		result = self.sign_in()
		self.assertEqual(result, {"full_name": "Waiter", "csrf_token": "fresh-csrf"})
		self.assertNotIn("pin", self.frappe.form_dict)
		self.assertNotIn("0527", str(result))
		self.rate_limit.assert_called_once_with(limit=60, seconds=60)

	def test_urypos_context_pad_only_when_paired_and_till_open(self):
		self.pair()
		brand = ModuleType("ury.brand")
		brand.get_brand_context = lambda **kw: {}
		with patch.dict(sys.modules, {"ury.brand": brand, "ury.ury_pos.floor_pin": self.api()}):
			spec = importlib.util.spec_from_file_location("urypos_page", ROOT / "ury/www/urypos.py")
			page = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(page)
			self.assertEqual(page.get_context({}).get("floor_pad"), {"paired": True, "till_open": True})
			self.frappe.db.exists.side_effect = lambda dt, filters: dt != "POS Opening Entry"
			self.assertEqual(page.get_context({}).get("floor_pad"), {"paired": True, "till_open": False})
			self.frappe.request.cookies.clear()
			self.assertEqual(page.get_context({}).get("floor_pad"), {"paired": False, "till_open": False})


if __name__ == "__main__":
	unittest.main()
