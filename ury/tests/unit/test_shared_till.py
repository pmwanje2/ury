"""Site-less regressions: real URY code, mocked Frappe/database boundaries."""

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def load_module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(monkeypatch):
    framework = ModuleType("frappe")
    framework.whitelist = lambda *args, **kwargs: lambda function: function
    framework._ = lambda message: message
    framework.PermissionError = PermissionError
    framework.session = SimpleNamespace(user="operator@example.com")
    framework.db = MagicMock()
    framework.defaults = MagicMock()
    framework.get_doc = MagicMock()
    framework.get_all = MagicMock()
    framework.get_list = MagicMock()
    framework.get_value = MagicMock(return_value="Take Away")
    framework.get_roles = MagicMock(return_value=["URY Cashier"])
    framework.has_permission = MagicMock(return_value=True)
    framework.cache = MagicMock()
    framework.flags = SimpleNamespace(in_bill_merge_sync=False)

    def throw(message, exception=ValueError):
        raise exception(message)

    framework.throw = throw
    utils = ModuleType("frappe.utils")
    clock = SimpleNamespace(now=datetime(2026, 10, 11, 12))
    utils.cint = lambda value: int(value or 0)
    utils.get_system_timezone = lambda: "UTC"
    utils.now_datetime = lambda: clock.now
    utils.get_datetime = lambda value=None: (
        clock.now if value is None else value if isinstance(value, datetime)
        else datetime.fromisoformat(value)
    )
    utils.datetime = datetime
    utils.add_to_date = lambda value, **kwargs: value + timedelta(**kwargs)
    utils.today = lambda: clock.now.date().isoformat()
    utils.flt = lambda value: float(value or 0)
    utils.nestedset = MagicMock()
    utils.validate_phone_number = MagicMock()
    framework.utils = utils
    document = ModuleType("frappe.model.document")
    document.Document = type("Document", (), {})
    queries = ModuleType("erpnext.controllers.queries")
    queries.item_query = MagicMock()
    kot = ModuleType("ury.ury.api.ury_kot_generate")
    kot.kot_execute = MagicMock()
    kot.process_items_for_cancel_kot = MagicMock()
    for name, module in {
        "frappe": framework, "frappe.utils": utils,
        "frappe.model.document": document, "erpnext.controllers.queries": queries,
        "ury.ury.api.ury_kot_generate": kot,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    app = Path(__file__).resolve().parents[2]
    load_module("ury.ury.api.ury_dashboard", app / "ury/api/ury_dashboard.py", monkeypatch)
    api = load_module("ury.ury_pos.api", app / "ury_pos/api.py", monkeypatch)
    order = load_module("ury.ury.doctype.ury_order.ury_order", app / "ury/doctype/ury_order/ury_order.py", monkeypatch)
    return SimpleNamespace(frappe=framework, clock=clock, api=api, order=order)


@pytest.fixture
def till(runtime, monkeypatch):
    profile = SimpleNamespace(
        name="POS-1", company="Test Co", branch="Branch A", restaurant="Rest A",
        warehouse="Stores", custom_enable_multiple_cashier=0, custom_daily_pos_close=0,
        applicable_for_users=[SimpleNamespace(user="operator@example.com", custom_main_cashier=0)],
        role_allowed_for_billing=[SimpleNamespace(role="URY Cashier")], payments=[],
    )
    entry = {
        "name": "OPEN-1", "user": "opener@example.com", "pos_profile": "POS-1",
        "company": "Test Co", "period_start_date": "2026-10-11 08:00:00",
        "status": "Open", "docstatus": 1,
    }
    framework = runtime.frappe
    framework.defaults.get_user_default.return_value = "Test Co"
    framework.get_doc.side_effect = lambda doctype, name: (
        entry if doctype == "POS Opening Entry" else profile if doctype == "POS Profile"
        else SimpleNamespace(default_currency="UGX")
    )
    framework.get_all.side_effect = lambda doctype, **kwargs: (
        [entry] if kwargs["filters"].get("pos_profile") == "POS-1" else []
    )
    cutoff = SimpleNamespace(hours=5)
    framework.db.get_value.side_effect = lambda doctype, name, field: (
        cutoff.hours if doctype == "URY Report Settings"
        else "Opener" if doctype == "User" else None
    )
    monkeypatch.setattr(runtime.api, "_get_allowed_pos_profiles", lambda *args: [{"name": "POS-1"}])
    monkeypatch.setattr(runtime.api, "getPosProfile", lambda: {"pos_profile": "POS-1"})
    monkeypatch.setattr(runtime.api, "validate_pos_close", lambda *args: "Success")
    return SimpleNamespace(**vars(runtime), profile=profile, entry=entry, cutoff=cutoff)


def test_assigned_billing_operator_can_join_without_changing_opening(till):
    original = dict(till.entry)
    occupied = till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"]
    assert occupied.get("joinable") is True
    assert occupied["name"] == "OPEN-1"
    assert till.entry == original
    till.frappe.db.set_value.assert_not_called()


@pytest.mark.parametrize("users", [[], [SimpleNamespace(user="someone-else@example.com")]])
def test_join_requires_explicit_native_profile_assignment(till, users):
    till.profile.applicable_for_users = users
    assert not till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable", False)


def test_billing_role_restriction_denies_other_roles(till):
    till.frappe.get_roles.return_value = ["URY Waiter"]
    assert not till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable", False)


def test_empty_billing_role_list_allows_assigned_operator(till):
    till.profile.role_allowed_for_billing = []
    till.frappe.get_roles.return_value = []
    assert till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable") is True


@pytest.mark.parametrize("opened,expected", [
    ("2026-10-11 04:59:59", False),
    ("2026-10-11 05:00:00", True),
    ("2026-10-12 05:00:00", False),
])
def test_join_uses_inclusive_start_exclusive_end_of_business_day(till, opened, expected):
    till.entry["period_start_date"] = opened
    occupied = till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"]
    assert occupied.get("joinable", False) is expected


def test_before_cutoff_previous_calendar_day_is_current_business_day(till):
    till.clock.now = datetime(2026, 10, 11, 4)
    till.entry["period_start_date"] = "2026-10-10 08:00:00"
    assert till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable") is True


def test_branch_cutoff_overrides_default_five_am(till):
    till.cutoff.hours = 7
    till.entry["period_start_date"] = "2026-10-11 06:59:59"
    assert not till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable", False)


def test_unreadable_opening_is_not_offered_for_resume(till):
    till.frappe.has_permission.side_effect = lambda doctype, permission, **kwargs: permission != "read"
    occupied = till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"]
    assert "name" not in occupied
    assert not occupied.get("joinable", False)


def test_join_does_not_require_permission_to_create_a_new_opening(till):
    till.frappe.has_permission.side_effect = lambda doctype, permission, **kwargs: permission == "read"
    assert till.api.get_pos_opening_screen_data("POS-1")["occupied_entry"].get("joinable") is True


def test_joining_operator_still_cannot_close_another_users_shift(till):
    with pytest.raises(PermissionError, match="Not permitted"):
        till.api.get_till_invoices("2026-10-11 08:00:00", "2026-10-11 12:00:00", "POS-1", "opener@example.com")
    till.frappe.get_list.assert_not_called()


@pytest.mark.parametrize("user", ["opener@example.com", "manager@example.com"])
def test_opener_and_ury_manager_retain_native_close_access(till, user, monkeypatch):
    till.frappe.session.user = user
    till.frappe.get_roles.return_value = ["URY Manager"] if user.startswith("manager") else ["URY Cashier"]
    till.frappe.get_list.return_value = [SimpleNamespace(period_start_date="2026-10-11 08:00:00")]
    monkeypatch.setattr(till.api, "_till_invoice_rows", lambda *args: [])
    assert till.api.get_till_invoices("ignored", "2026-10-11 12:00:00", "POS-1", "opener@example.com") == []


@pytest.mark.parametrize("existing_cashier", ["first-profile-user@example.com", "opener@example.com", None])
def test_settlement_records_authenticated_operator_not_client_or_original_cashier(runtime, monkeypatch, existing_cashier):
    invoice = MagicMock()
    invoice.cashier = existing_cashier
    invoice.custom_merged_pos_invoice = None
    invoice.restaurant_table = None
    invoice.owner = "waiter@example.com"
    invoice.waiter = "waiter@example.com"
    monkeypatch.setattr(runtime.order, "get_order_invoice", lambda *args: invoice)
    monkeypatch.setattr(runtime.order, "_validate_additional_discount", lambda *args: None)
    persisted_cashiers = []
    invoice.save.side_effect = lambda: persisted_cashiers.append(invoice.cashier)
    runtime.order.make_invoice(
        "Walk-in", [{"mode_of_payment": "Cash", "amount": 20000}],
        "forged@example.com", "POS-1", "forged@example.com", invoice="INV-1",
    )
    assert invoice.cashier == "operator@example.com"
    assert persisted_cashiers == ["operator@example.com"]
    assert invoice.owner == "waiter@example.com"
    assert invoice.waiter == "waiter@example.com"
    invoice.submit.assert_called_once_with()


@pytest.mark.parametrize("has_room_number", [True, False])
def test_settlement_sets_room_number_only_when_pos_invoice_has_field(runtime, monkeypatch, has_room_number):
    invoice = MagicMock()
    invoice.cashier = "cashier@example.com"
    invoice.custom_merged_pos_invoice = None
    invoice.restaurant_table = None
    seen_at_payment = []
    invoice.append.side_effect = lambda *args, **kwargs: seen_at_payment.append(
        invoice.__dict__.get("room_number")
    )
    runtime.frappe.get_meta = MagicMock()
    runtime.frappe.get_meta.return_value.has_field.return_value = has_room_number
    monkeypatch.setattr(runtime.order, "get_order_invoice", lambda *args: invoice)
    monkeypatch.setattr(runtime.order, "_validate_additional_discount", lambda *args: None)

    runtime.order.make_invoice(
        "Walk-in", [{"mode_of_payment": "Room Charge", "amount": 100}],
        "cashier@example.com", "POS-1", "cashier@example.com", invoice="INV-1",
        room_number="ROOM-101",
    )

    runtime.frappe.get_meta.assert_called_once_with("POS Invoice")
    runtime.frappe.get_meta.return_value.has_field.assert_called_once_with("room_number")
    assert seen_at_payment == (["ROOM-101"] if has_room_number else [None])
    if has_room_number:
        assert invoice.room_number == "ROOM-101"
    else:
        assert "room_number" not in invoice.__dict__


def test_merged_settlement_attributes_both_paid_invoices_to_operator(runtime, monkeypatch):
    invoice = MagicMock()
    invoice.cashier = "opener@example.com"
    invoice.custom_merged_pos_invoice = "INV-PRIMARY"
    invoice.restaurant_table = None
    invoice.rounded_total = 10000
    target = MagicMock()
    target.cashier = "first-profile-user@example.com"
    target.restaurant_table = None
    target.rounded_total = 15000
    target.owner = "waiter@example.com"
    target.waiter = "waiter@example.com"
    monkeypatch.setattr(runtime.order, "get_order_invoice", lambda *args: invoice)
    monkeypatch.setattr(runtime.order, "_validate_additional_discount", lambda *args: None)
    runtime.frappe.get_doc.return_value = target
    persisted_cashiers = []
    target.save.side_effect = lambda **kwargs: persisted_cashiers.append(target.cashier)
    runtime.order.make_invoice(
        "Walk-in", [{"mode_of_payment": "Cash", "amount": 25000}],
        "forged@example.com", "POS-1", "forged@example.com", invoice="INV-SECONDARY",
    )
    assert target.cashier == "operator@example.com"
    assert invoice.cashier == "operator@example.com"
    assert persisted_cashiers == ["operator@example.com"]
    assert target.owner == "waiter@example.com"
    assert target.waiter == "waiter@example.com"
    target.append.assert_called_once_with("payments", {"mode_of_payment": "Cash", "amount": 15000.0})


@pytest.fixture
def checklist(runtime, monkeypatch):
    """Real checklist API with only the clock and persistence boundaries replaced."""
    framework = runtime.frappe
    framework.db.sql.return_value = [{"branch": "Salama Training"}]
    framework.db.get_value.return_value = "Salama Training"
    framework.db.exists.return_value = True
    class ServerDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 13)

    monkeypatch.setattr(runtime.api, "date", ServerDate, raising=False)
    framework.utils.getdate = lambda value=None: (
        runtime.clock.now.date() if value is None
        else date.fromisoformat(value) if isinstance(value, str) else value
    )
    framework.utils.now = lambda: str(runtime.clock.now)
    configured = [SimpleNamespace(item_label="Count float", applies_to="Opening", is_mandatory=1)]
    logs = {}

    class Log(SimpleNamespace):
        def set(self, key, value):
            setattr(self, key, value)

        def append(self, key, value):
            getattr(self, key).append(SimpleNamespace(**value))

        def save(self):
            if not getattr(self, "name", None):
                self.name = f"CHK-{len(logs) + 1}"
            logs[self.name] = self

    def get_all(doctype, *, fields, filters, parent_doctype=None, limit=None):
        if doctype == "URY Checklist Item":
            assert filters == {"parent": "Salama Training Till", "applies_to": ["in", ["Opening", "Both"]]}
            assert parent_doctype == "POS Profile"
            return configured
        assert doctype == "URY POS Checklist Log"
        return [row for row in logs.values() if all(getattr(row, key) == value for key, value in filters.items())][:limit]

    framework.get_all.side_effect = get_all
    framework.new_doc = lambda doctype: Log(items=[])
    framework.get_doc.side_effect = lambda doctype, name: logs[name]
    payload = '[{"item_label": "Count float", "is_checked": 1, "remarks": "Training only"}]'
    return SimpleNamespace(**vars(runtime), logs=logs, Log=Log, payload=payload)


@pytest.mark.parametrize("site_time,expected_date", [
    (datetime(2026, 10, 13, 23), date(2026, 10, 13)),
    (datetime(2026, 10, 14, 0, 19, 14), date(2026, 10, 14)),
])
def test_checklist_lookup_resumes_the_site_local_day(checklist, site_time, expected_date):
    checklist.clock.now = site_time
    checklist.logs["CHK-LOCAL"] = checklist.Log(
        name="CHK-LOCAL", pos_profile="Salama Training Till", checklist_type="Opening",
        shift_date=expected_date, status="In Progress", items=[],
    )
    result = checklist.api.get_checklist("Salama Training Till", "Opening")
    assert result["log_name"] == "CHK-LOCAL"
    assert result["log_status"] == "In Progress"


@pytest.mark.parametrize("site_time,expected_date", [
    (datetime(2026, 10, 13, 23), date(2026, 10, 13)),
    (datetime(2026, 10, 14, 0, 19, 14), date(2026, 10, 14)),
])
def test_checklist_submission_persists_the_site_local_day(checklist, site_time, expected_date):
    checklist.clock.now = site_time
    result = checklist.api.submit_checklist("Salama Training Till", "Opening", checklist.payload)
    assert result["status"] == "Complete"
    saved = checklist.logs[result["name"]]
    assert saved.shift_date == expected_date
    assert saved.completed_by == "operator@example.com"
    assert saved.items[0].is_checked == 1


@pytest.mark.parametrize("site_time,expected_date", [
    (datetime(2026, 10, 13, 23), date(2026, 10, 13)),
    (datetime(2026, 10, 14, 0, 19, 14), date(2026, 10, 14)),
])
def test_checklist_submission_reuses_the_site_local_log(checklist, site_time, expected_date):
    checklist.clock.now = site_time
    checklist.logs["CHK-LOCAL"] = checklist.Log(
        name="CHK-LOCAL", pos_profile="Salama Training Till", checklist_type="Opening",
        shift_date=expected_date, status="In Progress", items=[],
    )
    result = checklist.api.submit_checklist("Salama Training Till", "Opening", checklist.payload)
    assert result == {"name": "CHK-LOCAL", "status": "Complete"}
    assert checklist.api.submit_checklist("Salama Training Till", "Opening", checklist.payload) == result
    assert list(checklist.logs) == ["CHK-LOCAL"]
    resumed = checklist.api.get_checklist("Salama Training Till", "Opening")
    assert resumed["log_name"] == "CHK-LOCAL"
    assert resumed["log_status"] == "Complete"
