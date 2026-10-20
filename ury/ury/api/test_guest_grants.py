"""G1-B behavioral contracts; native Frappe, controlled DB/pricing/printer boundaries.

No installed-site or transaction-contention claim is made by this unit suite.
"""

import hashlib
import inspect
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api import self_ordering as api

MOD = "ury.ury.api.self_ordering"
NOW = datetime(2026, 10, 16, 12)


class Record(SimpleNamespace):
    def get(self, name, default=None):
        return getattr(self, name, default)

    def update(self, values=None, **kwargs):
        values = values or {}
        self.__dict__.update(values)
        self.__dict__.update(kwargs)

    def __getitem__(self, name):
        return getattr(self, name)

    def __setitem__(self, name, value):
        setattr(self, name, value)


class TestGuestGrants(unittest.TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(mute_messages=True, in_test=True)
        frappe.local.session = frappe._dict(user="captain@example.test", data=frappe._dict())
        frappe.local.message_log = []
        frappe.local.error_log = []
        frappe.local.conf = frappe._dict(db_name="unit")
        frappe.local.form_dict = frappe._dict(cmd="guest-test")
        frappe.local.db = MagicMock()
        self.events = []
        self.sessions = {}
        self.credentials = {}
        self.rows = {}
        self.cache_data = {}
        self.commits = []
        self.rollbacks = []
        self.profile = frappe._dict(
            name="Guest Profile", enabled=1, branch="Branch A", pos_profile="Till A",
            restaurant="Restaurant A", enable_qr_table_ordering=1, enable_guest_assistant=0,
            require_guest_invoice_grant=0, allow_add_to_running_table=1, default_customer="Walk In",
            session_idle_timeout_minutes=30,
            enable_request_bill=1, enable_customer_payment=1, enable_payment_link=1,
        )
        self.invoice = self.make_invoice("BILL-1")
        self.make_invoice("SIBLING-SECRET", customer="OTHER-CUSTOMER")
        self.get_doc = self.enterContext(patch(f"{MOD}.frappe.get_doc", side_effect=self.load))
        self.enterContext(patch(f"{MOD}.frappe.get_all", side_effect=self.get_all))
        self.enterContext(patch(f"{MOD}.frappe.db.get_value", side_effect=self.get_value))
        self.enterContext(patch(f"{MOD}.frappe.db.get_values", side_effect=self.get_values))
        self.enterContext(patch(f"{MOD}.frappe.db.exists", return_value=None))
        self.enterContext(patch(f"{MOD}.frappe.db.set_value", side_effect=self.set_value))
        self.enterContext(patch(f"{MOD}.frappe.db.commit", side_effect=lambda: self.events.append("COMMIT")))
        self.enterContext(patch(f"{MOD}.frappe.db.after_commit.add", side_effect=self.commits.append))
        self.enterContext(patch(f"{MOD}.frappe.db.after_rollback.add", side_effect=self.rollbacks.append))
        self.enterContext(patch(f"{MOD}.frappe.set_user", side_effect=self.set_user))
        self.enterContext(patch(f"{MOD}.now_datetime", return_value=NOW))
        self.enterContext(patch(f"{MOD}._", side_effect=lambda value: value))
        self.enterContext(patch(f"{MOD}._get_profile_secret", return_value="synthetic-unit-secret"))
        self.enterContext(patch(f"{MOD}._ensure_admin_branch_mapping"))
        self.menu = self.enterContext(patch(f"{MOD}.resolve_restaurant_menu", return_value={"items": [{"item": "Tea"}]}))
        self.pricing = self.enterContext(patch(f"{MOD}.price_items_for_invoice", return_value=[{
            "item_code": "Tea", "item_name": "Tea", "qty": 1, "comment": "", "rate": 20, "amount": 20,
        }]))
        self.kot = self.enterContext(patch(f"{MOD}.kot_execute"))
        self.resolver = self.enterContext(patch(f"{MOD}._resolve_or_create_pos_invoice", return_value=(self.invoice, self.invoice.name)))
        self.cache = MagicMock()
        self.cache.get_value.side_effect = lambda key, **kw: self.cache_data.get(key)
        self.cache.set_value.side_effect = lambda key, value, **kw: self.cache_data.__setitem__(key, value)
        self.cache.delete_value.side_effect = lambda key: self.cache_data.pop(key, None)
        self.enterContext(patch.object(frappe, "cache", self.cache))
        self.enterContext(patch("ury.ury_pos.api.getBranch", return_value="Branch A"))
        self.enterContext(patch("ury.ury.doctype.ury_order.ury_order.getRoom", return_value=[{"name": "Dining"}]))
        self.enterContext(patch("ury.ury.doctype.ury_order.ury_order.frappe.get_roles", return_value=["URY Captain"]))

    def make_invoice(self, name, **changes):
        row = Record(
            name=name, branch="Branch A", pos_profile="Till A", restaurant_table="Table 7",
            custom_merged_tables="", custom_merged_pos_invoice=None, custom_split_from=None,
            custom_split_group=None, waiter="captain@example.test", customer="Walk In", docstatus=0,
            modified="unchanged", invoice_printed=0, invoice_created=1, creation=NOW,
            selling_price_list="Standard Selling", grand_total=20, is_return=0, items=[],
            order_type="Dine In",
        )
        row.update(changes)
        row.check_permission = MagicMock()
        row.save = lambda **kw: self.events.append(("save-invoice", name))
        row.append = lambda field, value: row[field].append(frappe._dict(value))
        self.rows[name] = row
        return row

    def set_user(self, user):
        frappe.local.session.user = user

    def load(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            if doctype["doctype"] == "URY Ordering Session":
                doctype = {"invoice": None, "is_granted": 0, "grant_fingerprint": None, "granted_by": None, **doctype}
            row = Record(**doctype)
            row.name = f"SESSION-{len(self.sessions) + 1}" if row.doctype == "URY Ordering Session" else "REQUEST-1"
            row.save = lambda **kw: self.events.append(("save-session", row.name))
            def insert(**kw):
                self.events.append(("insert", row.doctype))
                if row.doctype == "URY Ordering Session":
                    self.sessions[row.name] = row
            row.insert = insert
            return row
        self.events.append(("load", doctype, name, kwargs.get("for_update", False)))
        if doctype == "URY Self Ordering Profile":
            return self.profile
        if doctype == "POS Profile":
            return frappe._dict(transfer_role_permissions=[], role_allowed_for_billing=[], payments=[])
        if doctype == "URY Ordering Session":
            return self.sessions[name]
        if doctype == "POS Invoice":
            if name not in self.rows:
                raise frappe.DoesNotExistError("missing")
            return self.rows[name]
        raise AssertionError((doctype, name))

    def get_all(self, doctype, **kwargs):
        if doctype == "URY Self Ordering Profile":
            self.assertEqual(kwargs["filters"]["branch"], self.invoice.branch)
            return [self.profile.name]
        if doctype == "POS Invoice":
            self.events.append(("discovery", kwargs))
            return [self.rows["SIBLING-SECRET"]]
        if doctype == "URY Ordering Session":
            return [row.name for row in self.sessions.values() if row.invoice == kwargs["filters"]["invoice"]]
        if doctype == "URY Service Request":
            return []
        raise AssertionError((doctype, kwargs))

    def get_value(self, doctype, filters, fieldname="name", **kwargs):
        if doctype == "URY Ordering Session":
            for row in self.sessions.values():
                if all(row.get(key) == value for key, value in filters.items()):
                    return row.name
            return None
        if doctype == "URY Table":
            values = {"branch": "Branch A", "restaurant_room": "Dining", "merged_with": ""}
            return frappe._dict({key: values[key] for key in fieldname}) if isinstance(fieldname, list) else values.get(fieldname)
        if doctype == "URY Self Ordering Profile":
            return self.profile.get(fieldname)
        if doctype == "URY Menu":
            return "Menu A"
        if doctype in ("Payment Request", "URY Service Request"):
            self.events.append(("payment-read" if doctype == "Payment Request" else "service-read", filters))
            return None
        if doctype == "POS Invoice":
            row = self.rows.get(filters)
            return frappe._dict({key: row.get(key) for key in fieldname}) if isinstance(fieldname, list) else row.get(fieldname)
        raise AssertionError((doctype, filters, fieldname))

    def get_values(self, doctype, filters, fieldname="name", **kwargs):
        self.assertTrue(kwargs.get("for_update"), "lifecycle membership must use a native current read")
        self.events.append(("members", filters))
        rows = []
        self.assertIn(doctype, ("POS Invoice", "URY Ordering Session"))
        source = self.rows if doctype == "POS Invoice" else self.sessions
        for row in source.values():
            matches = True
            for key, value in filters.items():
                if isinstance(value, (list, tuple)):
                    matches &= value[0] == "<" and row.get(key) < value[1]
                else:
                    matches &= row.get(key) == value
            if matches:
                rows.append(frappe._dict({key: row.get(key) for key in fieldname}))
        return rows

    def set_value(self, doctype, name, field, value=None, **kw):
        if doctype == "URY Ordering Session":
            row = self.sessions[name]
            row.update(field if isinstance(field, dict) else {field: value})
        self.events.append(("set", doctype, name))

    def method(self, name):
        method = getattr(api, name, None)
        self.assertTrue(callable(method), f"Missing approved G1 behavior: {name}")
        return method

    def grant(self):
        self.profile.require_guest_invoice_grant = 1
        self.set_user("captain@example.test")
        result = self.method("issue_table_grant")("BILL-1")
        self.set_user("Guest")
        self.assertIn("session", result)
        row = next(row for row in self.sessions.values() if row.token_hash == hashlib.sha256(result["session"].encode()).hexdigest())
        self.credentials[row.name] = result["session"]
        return result["session"], row

    def assert_denied(self, call):
        self.events.clear()
        self.cache.set_value.reset_mock()
        self.kot.reset_mock()
        with self.assertRaises((frappe.PermissionError, frappe.ValidationError)) as caught:
            call()
        self.assertNotIn("SIBLING-SECRET", str(caught.exception))
        self.assertFalse(any(event[0] in ("insert", "set", "save-session", "save-invoice") for event in self.events))
        self.cache.set_value.assert_not_called()
        self.kot.assert_not_called()

    def test_static_qr_is_menu_only_and_never_discovers_or_appends_existing_bills(self):
        self.profile.require_guest_invoice_grant = 1
        raw, session = api._open_session(self.profile, "QR Table", "Table 7")
        self.set_user("Guest")
        self.events.clear()
        self.assertEqual(api.get_customer_order(raw)["items"], [])
        self.assert_denied(lambda: api.add_customer_items(raw, [{"item": "Tea", "qty": 1}]))
        self.resolver.assert_not_called()
        self.assertFalse(any(event[0] == "discovery" for event in self.events))
        self.assertIsNone(session.get("invoice"))

    def test_existing_menu_bootstrap_still_commits_its_unprivileged_session(self):
        api._open_session(self.profile, "QR Table", "Table 7")
        self.assertIn("COMMIT", self.events, "Existing GET bootstrap must remain usable on its next request")

    def test_staff_issue_exact_grant_revokes_previous_without_committing(self):
        first, old = self.grant()
        second, current = self.grant()
        self.assertNotEqual(first, second)
        self.assertEqual(current.invoice, "BILL-1")
        self.assertEqual(current.granted_by, "captain@example.test")
        self.assertEqual(current.is_granted, 1)
        self.assertEqual(len(current.grant_fingerprint), 64)
        self.assertEqual(old.status, "Closed")
        self.assertNotIn(first, json.dumps(current.__dict__, default=str))
        self.assertNotIn("COMMIT", self.events)
        self.assert_denied(lambda: api.get_customer_order(first))
        self.assertEqual(api.get_customer_order(second)["invoice"], "BILL-1")

    def test_granted_context_reuses_exact_session_without_opening_or_extending(self):
        token, session = self.grant()
        original_expiry = session.expires_at
        session.last_activity = NOW - timedelta(minutes=4)
        self.events.clear()

        result = self.method("get_granted_ordering_context")(token)

        self.assertEqual(result["session"], token)
        self.assertEqual(result["source"], "QR Table")
        self.assertEqual(result["table"], "Table 7")
        self.assertTrue(result["capabilities"]["add_to_running_table_enabled"])
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(session.expires_at, original_expiry)
        self.assertEqual(session.last_activity, NOW - timedelta(minutes=4))
        self.assertFalse(any(event[0] in ("insert", "save-session") for event in self.events))

    def test_granted_context_rejects_menu_only_session(self):
        self.profile.require_guest_invoice_grant = 1
        token, _session = api._open_session(self.profile, "QR Table", "Table 7")
        self.set_user("Guest")

        self.assert_denied(lambda: self.method("get_granted_ordering_context")(token))

    def test_granted_context_refuses_once_profile_flag_is_turned_off(self):
        token, session = self.grant()
        original_expiry = session.expires_at
        self.profile.require_guest_invoice_grant = 0
        self.assert_denied(lambda: self.method("get_granted_ordering_context")(token))
        self.assertEqual(session.expires_at, original_expiry, "a refused grant must not be extended")

    def test_granted_context_refuses_a_session_from_another_profile(self):
        token, session = self.grant()
        session.ordering_profile = "Other Profile"
        self.assert_denied(lambda: self.method("get_granted_ordering_context")(token))

    def test_granted_context_refuses_a_revoked_grant_after_reissue(self):
        first, old = self.grant()
        second, _current = self.grant()
        self.assertEqual(old.status, "Closed")
        self.assert_denied(lambda: self.method("get_granted_ordering_context")(first))
        self.assertEqual(self.method("get_granted_ordering_context")(second)["session"], second)

    def test_guest_bill_read_does_not_disclose_private_staff_line_notes(self):
        self.invoice.items.append(frappe._dict(
            item_code="Tea", item_name="Tea", qty=1,
            comment="PRIVATE STAFF kitchen note", rate=20, amount=20,
        ))
        token, _session = self.grant()
        result = api.get_customer_order(token)
        self.assertIsNone(result["items"][0].get("comment"))
        self.assertNotIn("PRIVATE STAFF", json.dumps(result))
        self.assertEqual(result["items"][0]["item_name"], "Tea")
        self.assertEqual(result["items"][0]["amount"], 20)
        self.assertEqual(self.invoice.items[0].comment, "PRIVATE STAFF kitchen note")

    def test_guest_append_response_hides_notes_without_removing_them_from_kitchen(self):
        self.invoice.items.append(frappe._dict(
            item_code="Tea", item_name="Tea", qty=1,
            comment="PRIVATE STAFF kitchen note", rate=20, amount=20,
        ))
        self.pricing.return_value = [{
            "item_code": "Tea", "item_name": "Tea", "qty": 1,
            "comment": "guest no sugar", "rate": 20, "amount": 20,
        }]
        token, _session = self.grant()
        result = api.add_customer_items(token, [{"item": "Tea", "qty": 1, "comment": "guest no sugar"}])
        self.assertNotIn("PRIVATE STAFF", json.dumps(result))
        self.assertTrue(all(row.get("comment") is None for row in result["items"]))
        self.assertEqual([row.comment for row in self.invoice.items], ["PRIVATE STAFF kitchen note", "guest no sugar"])
        self.assertEqual(self.kot.call_args.args[3], [
            {"item_code": "Tea", "item_name": "Tea", "qty": 1, "comments": "PRIVATE STAFF kitchen note"},
            {"item_code": "Tea", "item_name": "Tea", "qty": 1, "comments": "guest no sugar"},
        ])

    def test_staff_authority_requires_native_write_branch_room_and_waiter(self):
        # Grant mode ON: each refusal below must come from native authority,
        # not from the default-OFF issuance refusal.
        self.profile.require_guest_invoice_grant = 1
        cases = ["guest", "write", "branch", "room", "waiter", "submitted", "return", "disabled"]
        for case in cases:
            with self.subTest(case=case):
                self.set_user("captain@example.test")
                self.invoice.update(branch="Branch A", waiter="captain@example.test", docstatus=0, is_return=0)
                self.profile.enabled = 1
                self.invoice.check_permission.side_effect = None
                if case == "guest":
                    self.set_user("Guest")
                if case == "write":
                    self.invoice.check_permission.side_effect = frappe.PermissionError("write denied")
                if case == "branch":
                    self.invoice.branch = "Branch B"
                if case == "room":
                    room = self.enterContext(patch("ury.ury.doctype.ury_order.ury_order.getRoom", return_value=[{"name": "Other Room"}]))
                if case == "waiter":
                    self.invoice.waiter = "other@example.test"
                if case == "submitted":
                    self.invoice.docstatus = 1
                if case == "return":
                    self.invoice.is_return = 1
                if case == "disabled":
                    self.profile.enabled = 0
                self.assert_denied(lambda: self.method("issue_table_grant")("BILL-1"))
                if case == "room":
                    room.return_value = [{"name": "Dining"}]

    def test_native_write_permission_is_checked_before_session_insert(self):
        self.grant()
        self.invoice.check_permission.assert_called_once_with("write")
        lock = self.events.index(("load", "POS Invoice", "BILL-1", True))
        insert = self.events.index(("insert", "URY Ordering Session"))
        self.assertLess(lock, insert)

    def test_forged_expired_ungranted_or_foreign_session_never_exposes_a_bill(self):
        token, session = self.grant()
        self.assert_denied(lambda: api.get_customer_order("forged-session"))
        for field, value in [("status", "Closed"), ("expires_at", NOW - timedelta(seconds=1)),
                             ("is_granted", 0), ("grant_fingerprint", "forged"),
                             ("invoice", "SIBLING-SECRET"), ("table", "Table 8"),
                             ("ordering_profile", "Other Profile")]:
            with self.subTest(field=field):
                original = session[field]
                session[field] = value
                self.assert_denied(lambda: api.get_customer_order(token))
                session[field] = original

    def test_every_invoice_lifecycle_change_invalidates_without_modified_dependency(self):
        token, session = self.grant()
        cases = [("branch", "Branch B"), ("pos_profile", "Till B"), ("restaurant_table", "Table 8"),
                 ("custom_merged_tables", "Table 8"), ("custom_merged_pos_invoice", "SIBLING-SECRET"),
                 ("custom_split_from", "SIBLING-SECRET"), ("custom_split_group", "GROUP"),
                 ("waiter", "other@example.test"), ("customer", "Next Party"), ("docstatus", 1), ("docstatus", 2)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                old = self.invoice[field]
                self.invoice[field] = value
                self.assert_denied(lambda: api.get_customer_order(token))
                self.invoice[field] = old
                self.assertEqual(self.invoice.modified, "unchanged")
        del self.rows["BILL-1"]
        self.assert_denied(lambda: api.get_customer_order(token))
        self.assertEqual(session.status, "Active")  # Refusal does not require an unsafe commit.

    def test_resplit_and_group_member_removal_or_submission_revoke_exact_sibling(self):
        self.invoice.custom_split_group = "GROUP"
        sibling = self.rows["SIBLING-SECRET"]
        sibling.custom_split_group = "GROUP"
        sibling.custom_split_from = "BILL-1"
        token, _session = self.grant()
        nested = self.make_invoice("NESTED-SECRET", custom_split_group="GROUP", custom_split_from="SIBLING-SECRET")
        self.assert_denied(lambda: api.get_customer_order(token))
        del self.rows[nested.name]
        sibling.docstatus = 1
        self.assert_denied(lambda: api.get_customer_order(token))
        sibling.docstatus = 2
        self.assert_denied(lambda: api.get_customer_order(token))
        del self.rows[sibling.name]
        self.assert_denied(lambda: api.get_customer_order(token))

    def test_legacy_split_child_inherits_native_parent_group_for_resplit_revocation(self):
        self.invoice.custom_split_from = "ROOT-SECRET"
        self.make_invoice("ROOT-SECRET", custom_split_group="GROUP")
        self.rows["SIBLING-SECRET"].custom_split_group = "GROUP"
        token, _session = self.grant()
        self.make_invoice("NESTED-SECRET", custom_split_group="GROUP", custom_split_from="SIBLING-SECRET")
        self.assert_denied(lambda: api.get_customer_order(token))

    def test_rounds_print_and_sorted_table_order_do_not_invalidate_grant(self):
        self.invoice.custom_merged_tables = "Table 9, Table 8"
        token, _session = self.grant()
        self.invoice.custom_merged_tables = "Table 8,Table 9"
        self.invoice.invoice_printed = 1
        self.invoice.modified = "normal-round"
        self.invoice.items.append(frappe._dict(item_code="Tea", item_name="Tea", qty=1, comment="", rate=20, amount=20))
        result = api.get_customer_order(token)
        self.assertEqual(result["invoice"], "BILL-1")
        self.assertEqual(result["items"][0]["item_code"], "Tea")
        self.assertNotIn("customer", result)

    def test_all_bill_service_payment_and_status_methods_deny_old_party_before_side_effects(self):
        token, _session = self.grant()
        self.invoice.customer = "Next Party"
        calls = [lambda: api.get_customer_order(token), lambda: api.get_order_status(token),
                 lambda: api.add_customer_items(token, [{"item": "Tea", "qty": 1}]),
                 lambda: api.request_bill(token), lambda: api.request_assistance(token),
                 lambda: api.create_payment_request(token), lambda: api.get_payment_status(token),
                 lambda: api.share_payment_link(token, "nobody@example.test")]
        for call in calls:
            with self.subTest(call=call):
                self.assert_denied(call)

    def test_native_lock_conflicts_are_safe_retry_and_do_not_write(self):
        token, _session = self.grant()
        original = self.load
        for error in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            with self.subTest(error=error):
                def conflict(doctype, name=None, **kw):
                    if doctype == "POS Invoice" and kw.get("for_update"):
                        raise error("database details must not escape")
                    return original(doctype, name, **kw)
                self.get_doc.side_effect = conflict
                self.assert_denied(lambda: api.add_customer_items(token, [{"item": "Tea", "qty": 1}]))

    def test_server_pricing_and_request_id_replay_append_once_after_commit(self):
        self.assertIn("request_id", inspect.signature(api.add_customer_items).parameters,
                      "Guest item additions cannot safely replay a request yet")
        token, _session = self.grant()
        items = [{"item": "Tea", "qty": 1, "rate": 0.01, "invoice": "SIBLING-SECRET"}]
        result = api.add_customer_items(token, items, request_id="round-1")
        self.assertEqual(result["items"][0]["rate"], 20)
        self.assertEqual(self.invoice.items[0].rate, 20)
        self.assertTrue(all("response" not in value for value in self.cache_data.values()),
                        "cache success must not precede native transaction commit")
        for callback in self.commits:
            callback()
        self.assertEqual(api.add_customer_items(token, items, request_id="round-1"), result)
        self.assertEqual(len(self.invoice.items), 1)
        self.kot.assert_called_once()
        self.resolver.assert_not_called()  # Exact granted invoice, never a discovery helper.

    def test_request_id_cannot_be_rebound_and_replay_never_bypasses_revocation(self):
        self.assertIn("request_id", inspect.signature(api.add_customer_items).parameters,
                      "Guest item additions cannot safely replay a request yet")
        token, _session = self.grant()
        api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1")
        for callback in self.commits:
            callback()
        self.assert_denied(lambda: api.add_customer_items(token, [{"item": "Tea", "qty": 2}], request_id="round-1"))
        self.invoice.customer = "Next Party"
        self.assert_denied(lambda: api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1"))

    def test_pending_round_refuses_in_native_commit_to_cache_callback_window(self):
        token, _session = self.grant()
        api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1")
        # A waiting native request can acquire the invoice after SQL COMMIT,
        # but before after_commit callbacks run. It must not append twice.
        self.assert_denied(lambda: api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1"))
        self.assertEqual(len(self.invoice.items), 1)

    def test_native_rollback_clears_pending_round_without_recording_success(self):
        token, _session = self.grant()
        api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1")
        self.assertTrue(self.rollbacks, "No native rollback cleanup for the in-flight request ID")
        self.invoice.items.clear()  # Controlled DB boundary; actual rollback is a native-site gate.
        for callback in self.rollbacks:
            callback()
        self.commits.clear()
        result = api.add_customer_items(token, [{"item": "Tea", "qty": 1}], request_id="round-1")
        self.assertEqual(len(result["items"]), 1)

    def test_table_grant_expires_within_thirty_minutes_even_with_long_profile_timeout(self):
        self.profile.session_idle_timeout_minutes = 600
        _token, session = self.grant()
        self.assertLessEqual(session.expires_at, NOW + timedelta(minutes=30))

    def test_staff_grant_expiry_does_not_slide_on_activity(self):
        _token, session = self.grant()
        session.expires_at = NOW + timedelta(minutes=5)
        session.last_activity = NOW - timedelta(minutes=4)
        api._checked_guest_session(session.name)
        self.assertEqual(session.expires_at, NOW + timedelta(minutes=5))
        self.assertEqual(session.last_activity, NOW - timedelta(minutes=4))

    def test_non_grant_session_slides_expiry_on_activity(self):
        _token, session = api._open_session(self.profile, "Kiosk", None)
        session.expires_at = NOW + timedelta(minutes=1)
        session.last_activity = NOW - timedelta(minutes=4)
        api._checked_guest_session(session.name)
        self.assertEqual(session.expires_at, NOW + timedelta(minutes=30))
        self.assertEqual(session.last_activity, NOW)

    def test_flag_off_table_qr_and_tablet_sessions_slide_like_pre_g1(self):
        # Default OFF must keep the pre-G1 sliding expiry for every existing
        # table surface, not only pickup/kiosk sessions.
        self.profile.require_guest_invoice_grant = 0
        for source, device in (("QR Table", None), ("Table Tablet", "DEVICE-1")):
            with self.subTest(source=source):
                token, session = api._open_session(self.profile, source, "Table 7", device)
                session.expires_at = NOW + timedelta(minutes=1)
                session.last_activity = NOW - timedelta(minutes=4)
                self.set_user("Guest")
                api.get_order_status(token)
                self.assertEqual(session.expires_at, NOW + timedelta(minutes=30))
                self.assertEqual(session.last_activity, NOW)

    def test_flag_on_menu_only_table_session_keeps_fixed_expiry(self):
        self.profile.require_guest_invoice_grant = 1
        token, session = api._open_session(self.profile, "QR Table", "Table 7")
        session.expires_at = NOW + timedelta(minutes=1)
        self.set_user("Guest")
        api.get_order_status(token)
        self.assertEqual(session.expires_at, NOW + timedelta(minutes=1))

    def test_staff_grant_refused_while_invoice_grant_flag_is_off(self):
        self.profile.require_guest_invoice_grant = 0
        self.set_user("captain@example.test")
        self.assert_denied(lambda: self.method("issue_table_grant")("BILL-1"))
        self.assertEqual(self.sessions, {})

    def test_captain_only_visibility_check_follows_profile_flag_without_issuing(self):
        # setUp grants this user URY Captain only: no DocPerm on the profile,
        # which holds qr_signing_secret. The answer must come from the server.
        check = self.method("can_issue_table_grant")
        self.assertNotIn(check, frappe.guest_methods)
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[check], ["POST"])
        self.set_user("captain@example.test")
        for flag, expected in ((1, True), (0, False)):
            with self.subTest(flag=flag):
                self.profile.require_guest_invoice_grant = flag
                self.events.clear()
                frappe.local.message_log = []
                self.assertEqual(check("BILL-1"), {"visible": expected})
                self.assertEqual(self.sessions, {})
                self.assertEqual(frappe.local.message_log, [], "a hidden action must not leak a refusal toast")
                self.assertFalse(any(event[0] in ("insert", "set", "save-session", "save-invoice") for event in self.events))
                self.assertFalse(any(event[0] == "load" and event[3] for event in self.events), "visibility must not lock rows")
        self.profile.require_guest_invoice_grant = 1
        self.invoice.waiter = "other@example.test"
        self.assertEqual(check("BILL-1"), {"visible": False})
        self.invoice.waiter = "captain@example.test"
        self.set_user("Guest")
        self.assertEqual(check("BILL-1"), {"visible": False})

    def test_menu_only_session_never_looks_up_unbound_payment_links(self):
        self.profile.require_guest_invoice_grant = 1
        token, _session = api._open_session(self.profile, "QR Table", "Table 7")
        self.assert_denied(lambda: api.share_payment_link(token, "nobody@example.test"))
        self.assertFalse(any(event[0] == "payment-read" for event in self.events),
                         "Menu-only QR queried a payment request without an authorized invoice")

    def test_non_ascii_grant_fingerprint_refuses_as_permission_not_server_error(self):
        token, session = self.grant()
        session.grant_fingerprint = "é" * 64
        try:
            api.get_customer_order(token)
        except frappe.PermissionError:
            pass
        except Exception as error:
            self.fail(f"Malformed fingerprint escaped as {type(error).__name__}")
        else:
            self.fail("Malformed fingerprint was accepted")

    def test_deferred_guest_assistant_token_surface_is_removed(self):
        self.assertFalse(hasattr(api, "issue_guest_assistant_token"))
        self.assertFalse(hasattr(api, "resolve_guest_assistant_token"))
        issue = self.method("issue_table_grant")
        self.assertNotIn(issue, frappe.guest_methods)
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[issue], ["POST"])

    def test_bill_bearing_guest_reads_are_post_only(self):
        for name in ("get_customer_menu", "get_customer_product", "get_customer_order",
                     "get_order_status", "get_payment_status", "get_granted_ordering_context"):
            with self.subTest(name=name):
                method = self.method(name)
                self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

    def test_issue_table_grant_does_not_wrap_open_session_in_redundant_elevation(self):
        with patch.object(api, "_elevated", wraps=api._elevated) as elevated:
            self.grant()
        self.assertEqual(elevated.call_count, 1)


class TestGuestGrantSchema(unittest.TestCase):
    def test_native_session_grants_are_hidden_readonly_and_assistant_is_default_off(self):
        root = Path(__file__).resolve().parents[1] / "doctype"
        session = json.loads((root / "ury_ordering_session/ury_ordering_session.json").read_text())
        fields = {row["fieldname"]: row for row in session["fields"]}
        for name in ("grant_fingerprint", "granted_by", "is_granted"):
            self.assertIn(name, fields, f"Native session cannot store {name}")
            self.assertEqual(fields[name].get("hidden"), 1)
            self.assertEqual(fields[name].get("read_only"), 1)
            self.assertEqual(fields[name].get("no_copy"), 1)
        profile = json.loads((root / "ury_self_ordering_profile/ury_self_ordering_profile.json").read_text())
        fields = {row["fieldname"]: row for row in profile["fields"]}
        self.assertIn("enable_guest_assistant", fields)
        self.assertEqual(str(fields["enable_guest_assistant"]["default"]), "0")
        self.assertIn("require_guest_invoice_grant", fields)
        self.assertEqual(str(fields["require_guest_invoice_grant"]["default"]), "0")

        custom_fields = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "custom_field.json").read_text())
        indexed = {
            row["fieldname"]: row
            for row in custom_fields
            if row.get("dt") == "POS Invoice"
            and row.get("fieldname") in {"custom_split_from", "custom_merged_pos_invoice", "custom_split_group"}
        }
        self.assertEqual(set(indexed), {"custom_split_from", "custom_merged_pos_invoice", "custom_split_group"})
        for name, row in indexed.items():
            with self.subTest(field=name):
                self.assertEqual(row.get("search_index"), 1)

        session_fields = {row["fieldname"]: row for row in json.loads(
            (root / "ury_ordering_session/ury_ordering_session.json").read_text()
        )["fields"]}
        self.assertEqual(session_fields["invoice"].get("search_index"), 1)
