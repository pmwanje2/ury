"""G40 Serving Periods / Dayparts and G41 Outlet Timezone Stamping.

Site-less unit tests (frappe double, no bench):
- URY Restaurant Serving Period child DocType schema, URY Restaurant table field,
  URY Menu Item daypart fields, and vendor-free en.csv Desk translation;
- Non-overlapping equal-priority Serving Period validation (including overnight midnight wrap!);
- Outlet timezone conversion from URY Branch Operating Schedule.timezone (zoneinfo.ZoneInfo),
  zero-padded HH:MM:SS custom_transaction_local_time and Mon..Sun custom_transaction_weekday,
  honoring ury_offline_captured_at for offline replays;
- 5-tier Menu & Price List resolution order preserving #143 invariants:
  1. Aggregators (Aggregator Settings.price_list)
  2. room_wise_menu
  3. Active URY Restaurant Serving Period (menu / price_list_override, highest priority wins)
  4. order_type_wise_menu
  5. URY Restaurant.active_menu
- Out-of-daypart item flagging (unavailable_daypart + human window label) in resolve_restaurant_menu;
- Open-check rate stability in validate_price_list after serving period boundary passes.
"""

import csv
import importlib.util
import json
import sys
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
URY_PKG = ROOT / "ury"
TODAY = date(2026, 11, 24)


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class Refused(Exception):
    pass


def _make_frappe():
    framework = MagicMock()
    framework.ValidationError = Refused
    framework.PermissionError = type("PermissionError", (Exception,), {})
    framework._ = lambda msg, *args: msg
    framework.bold = lambda x: f"<b>{x}</b>"
    framework.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    framework.flags = SimpleNamespace()
    framework.session = SimpleNamespace(user="cashier@example.com")

    def throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    framework.throw = throw
    utils = _stub(
        "frappe.utils",
        cint=lambda v: int(float(v or 0)),
        flt=lambda v, precision=None: round(float(v or 0), int(precision)) if precision is not None else float(v or 0),
        getdate=lambda v=None: TODAY if v is None else (v.date() if isinstance(v, datetime) else v if isinstance(v, date) else date.fromisoformat(str(v)[:10])),
        get_datetime=lambda v=None: datetime.fromisoformat(str(v).replace("Z", "+00:00")) if isinstance(v, str) else (v or datetime(2026, 11, 24, 14, 0, 0)),
        now_datetime=lambda: datetime(2026, 11, 24, 14, 0, 0),
        get_time=lambda v: v if isinstance(v, time) else time.fromisoformat(str(v)),
        get_system_timezone=lambda: "UTC",
        now=lambda: "2026-11-24 14:00:00",
        get_first_day=MagicMock(),
        get_last_day=MagicMock(),
    )
    framework.utils = utils
    return framework, utils


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise FileNotFoundError(f"Missing module file: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestDaypartSchemaAndTranslations(unittest.TestCase):
    def test_serving_period_doctype_schema(self):
        schema_path = (
            URY_PKG / "ury/doctype/ury_restaurant_serving_period/ury_restaurant_serving_period.json"
        )
        self.assertTrue(schema_path.is_file(), f"Missing DocType schema: {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema.get("name"), "URY Restaurant Serving Period")
        self.assertEqual(schema.get("module"), "URY")
        self.assertEqual(schema.get("istable"), 1)
        fields = {f["fieldname"]: f for f in schema.get("fields", [])}
        for fname in (
            "period_name",
            "enabled",
            "start_time",
            "end_time",
            "weekdays",
            "order_types",
            "menu",
            "price_list_override",
            "priority",
        ):
            self.assertIn(fname, fields, f"Missing field {fname} on URY Restaurant Serving Period")
        self.assertEqual(fields["menu"]["options"], "URY Menu")
        self.assertEqual(fields["price_list_override"]["options"], "Price List")

    def test_ury_restaurant_and_menu_item_daypart_fields(self):
        rest = json.loads((URY_PKG / "ury/doctype/ury_restaurant/ury_restaurant.json").read_text(encoding="utf-8"))
        rest_fields = {f["fieldname"]: f for f in rest.get("fields", [])}
        self.assertIn("custom_serving_periods", rest_fields)
        self.assertEqual(rest_fields["custom_serving_periods"]["fieldtype"], "Table")
        self.assertEqual(rest_fields["custom_serving_periods"]["options"], "URY Restaurant Serving Period")

        mi = json.loads((URY_PKG / "ury/doctype/ury_menu_item/ury_menu_item.json").read_text(encoding="utf-8"))
        mi_fields = {f["fieldname"]: f for f in mi.get("fields", [])}
        for fname in ("custom_available_from", "custom_available_to", "custom_serving_period"):
            self.assertIn(fname, mi_fields, f"URY Menu Item missing field {fname}")

    def test_en_csv_translates_ury_restaurant_serving_period(self):
        with (URY_PKG / "translations/en.csv").open(encoding="utf-8", newline="") as fh:
            mapping = {row[0]: row[1] for row in list(csv.reader(fh))[1:] if len(row) >= 2}
        self.assertIn("URY Restaurant Serving Period", mapping)
        self.assertNotRegex(mapping["URY Restaurant Serving Period"], r"(?i)\bury\b")


class TestServingPeriodResolutionAndTimezone(unittest.TestCase):
    def setUp(self):
        self.frappe, self.utils = _make_frappe()
        self.enterContext(patch.dict(sys.modules, {"frappe": self.frappe, "frappe.utils": self.utils}))
        mod_path = URY_PKG / "ury_pos/dayparts.py"
        if not mod_path.is_file():
            self.fail(f"Missing module {mod_path}")
        self.dayparts = _load("ury.ury_pos.dayparts", mod_path)

    def test_outlet_timezone_stamps_zero_padded_local_time_and_weekday_from_utc(self):
        self.frappe.db.get_value.side_effect = lambda dt, flt, field=None, **kw: (
            "Africa/Kampala" if dt == "URY Branch Operating Schedule" else None
        )
        # 2026-11-24 is Tuesday. 06:05 UTC == 09:05:00 EAT (UTC+3)
        captured_utc = datetime(2026, 11, 24, 6, 5, 0, tzinfo=timezone.utc)
        local_dt, local_time_str, weekday = self.dayparts.resolve_outlet_local_datetime(
            "Kampala Branch", at=captured_utc
        )
        self.assertEqual(local_time_str, "09:05:00")
        self.assertEqual(weekday, "Tue")
        self.assertEqual(local_dt.tzinfo.key, "Africa/Kampala")

    def test_midnight_wrapping_serving_period_matches_before_and_after_midnight(self):
        periods = [
            Row(
                period_name="Late Night",
                enabled=1,
                start_time="22:00:00",
                end_time="02:00:00",
                weekdays="Fri",
                order_types="",
                menu="Late Menu",
                price_list_override="Late Prices",
                priority=10,
                idx=1,
            ),
            Row(
                period_name="Breakfast",
                enabled=1,
                start_time="06:00:00",
                end_time="11:00:00",
                weekdays="",
                order_types="",
                menu="Breakfast Menu",
                price_list_override="Breakfast Prices",
                priority=10,
                idx=2,
            ),
        ]
        self.frappe.get_all.return_value = periods
        # Friday 2026-11-27 23:30 local -> matches Fri Late Night
        fri_late = datetime(2026, 11, 27, 23, 30, 0)
        active = self.dayparts.resolve_active_serving_period("Bistro", order_type="Dine In", local_dt=fri_late)
        self.assertIsNotNone(active)
        self.assertEqual(active["period_name"], "Late Night")

        # Saturday 2026-11-28 01:15 local -> carryover from Friday Late Night!
        sat_early = datetime(2026, 11, 28, 1, 15, 0)
        active2 = self.dayparts.resolve_active_serving_period("Bistro", order_type="Dine In", local_dt=sat_early)
        self.assertIsNotNone(active2)
        self.assertEqual(active2["period_name"], "Late Night")

        # Saturday 2026-11-28 02:00 local -> window closed
        sat_after = datetime(2026, 11, 28, 2, 0, 0)
        active3 = self.dayparts.resolve_active_serving_period("Bistro", order_type="Dine In", local_dt=sat_after)
        self.assertIsNone(active3)

    def test_overlap_validation_rejects_ambiguous_equal_priority_and_midnight_cross_overlap(self):
        # Equal priority daytime overlap
        rows_day = [
            Row(period_name="Breakfast", enabled=1, start_time="06:00:00", end_time="11:00:00", weekdays="", order_types="", priority=10, idx=1),
            Row(period_name="Brunch", enabled=1, start_time="10:00:00", end_time="13:00:00", weekdays="", order_types="", priority=10, idx=2),
        ]
        with self.assertRaises(Refused):
            self.dayparts.validate_serving_periods(rows_day)

        # Equal priority overnight carryover overlap (Fri 22:00-02:00 vs Sat 01:00-04:00)
        rows_overnight = [
            Row(period_name="Fri Late", enabled=1, start_time="22:00:00", end_time="02:00:00", weekdays="Fri", order_types="", priority=10, idx=1),
            Row(period_name="Sat Early", enabled=1, start_time="01:00:00", end_time="04:00:00", weekdays="Sat", order_types="", priority=10, idx=2),
        ]
        with self.assertRaises(Refused):
            self.dayparts.validate_serving_periods(rows_overnight)

        # Higher priority intentional overlap (Happy Hour overrides All Day) -> ALLOWED
        rows_prio = [
            Row(period_name="All Day", enabled=1, start_time="06:00:00", end_time="23:00:00", weekdays="", order_types="", priority=10, idx=1),
            Row(period_name="Happy Hour", enabled=1, start_time="16:00:00", end_time="19:00:00", weekdays="", order_types="Dine In", priority=20, idx=2),
        ]
        self.dayparts.validate_serving_periods(rows_prio)

    def test_five_tier_menu_and_price_list_priority_order(self):
        periods = [
            Row(
                period_name="Happy Hour",
                enabled=1,
                start_time="16:00:00",
                end_time="19:00:00",
                weekdays="",
                order_types="Dine In,Take Away",
                menu="Happy Menu",
                price_list_override="Happy Prices",
                priority=20,
                idx=1,
            )
        ]
        self.frappe.get_all.return_value = periods
        state = {
            "room_wise_menu": 0,
            "order_type_wise_menu": 1,
            "active_menu": "Main Menu",
            "room_menu": "Pool Menu",
            "ot_menu": "Pickup Menu",
            "agg_pl": "Glovo Prices",
        }

        def db_get_value(dt, flt=None, field=None, **kw):
            if dt == "Aggregator Settings":
                return state["agg_pl"]
            if dt == "URY Restaurant":
                if isinstance(flt, dict):
                    return "Bistro"
                return state.get(field)
            if dt == "URY Table":
                return "Pool Bar"
            if dt == "Menu for Room":
                return state["room_menu"]
            if dt == "Order Type Menu":
                return state["ot_menu"]
            if dt == "Price List":
                return f"{flt['restaurant_menu']} Prices"
            if dt == "URY Branch Operating Schedule":
                return "Africa/Kampala"
            return None

        self.frappe.db.get_value.side_effect = db_get_value
        hh_dt = datetime(2026, 11, 24, 17, 0, 0)
        off_dt = datetime(2026, 11, 24, 13, 0, 0)

        # Tier 1: Aggregators always wins
        r1 = self.dayparts.resolve_menu_and_price_list(
            "Main Branch", restaurant="Bistro", order_type="Aggregators", customer="Glovo", local_dt=hh_dt
        )
        self.assertEqual(r1["price_list"], "Glovo Prices")

        # Tier 2: room_wise_menu wins over active Serving Period
        state["room_wise_menu"] = 1
        r2 = self.dayparts.resolve_menu_and_price_list(
            "Main Branch", restaurant="Bistro", table="Pool-1", order_type="Dine In", local_dt=hh_dt
        )
        self.assertEqual(r2["menu"], "Pool Menu")
        self.assertEqual(r2["price_list"], "Pool Menu Prices")

        # Tier 3: Active Serving Period wins over order_type_wise_menu & active_menu
        state["room_wise_menu"] = 0
        r3 = self.dayparts.resolve_menu_and_price_list(
            "Main Branch", restaurant="Bistro", order_type="Take Away", local_dt=hh_dt
        )
        self.assertEqual(r3["menu"], "Happy Menu")
        self.assertEqual(r3["price_list"], "Happy Prices")
        self.assertEqual(r3["serving_period"]["period_name"], "Happy Hour")

        # Tier 4: Outside Serving Period -> order_type_wise_menu wins for counter orders
        r4 = self.dayparts.resolve_menu_and_price_list(
            "Main Branch", restaurant="Bistro", order_type="Take Away", local_dt=off_dt
        )
        self.assertEqual(r4["menu"], "Pickup Menu")
        self.assertEqual(r4["price_list"], "Pickup Menu Prices")

        # Tier 5: Default active_menu fallback
        state["order_type_wise_menu"] = 0
        r5 = self.dayparts.resolve_menu_and_price_list(
            "Main Branch", restaurant="Bistro", order_type="Take Away", local_dt=off_dt
        )
        self.assertEqual(r5["menu"], "Main Menu")
        self.assertEqual(r5["price_list"], "Main Menu Prices")

    def test_item_daypart_window_flags_unavailable_and_human_label(self):
        item = Row(
            item="EGGS",
            custom_available_from="06:00:00",
            custom_available_to="11:00:00",
            custom_serving_period="",
        )
        out_status = self.dayparts.evaluate_item_daypart(item, local_dt=datetime(2026, 11, 24, 13, 15, 0))
        self.assertTrue(out_status["unavailable_daypart"])
        self.assertEqual(out_status["daypart_label"], "Available 06:00–11:00")

        in_status = self.dayparts.evaluate_item_daypart(item, local_dt=datetime(2026, 11, 24, 8, 30, 0))
        self.assertFalse(in_status["unavailable_daypart"])
        self.assertIsNone(in_status["daypart_label"])


class TestOpenCheckRateStability(unittest.TestCase):
    def test_validate_price_list_preserves_stamped_open_check_after_period_switch(self):
        frappe_mock, utils = _make_frappe()
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.ury.doctype.ury_order.ury_order": _stub(
                "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()
            ),
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
                "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
                get_applicable_policy=MagicMock(),
            ),
            "ury.ury_pos.discount": _stub(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
            ),
        }
        with patch.dict(sys.modules, modules):
            if not (URY_PKG / "ury_pos/dayparts.py").is_file():
                self.fail("Missing ury/ury_pos/dayparts.py")
            _load("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            hooks = _load("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")

            # Simulate Breakfast period ended and Lunch period is now active.
            frappe_mock.get_all.side_effect = lambda dt, **kw: (
                [
                    Row(
                        period_name="Breakfast",
                        enabled=1,
                        start_time="06:00:00",
                        end_time="11:00:00",
                        weekdays="",
                        order_types="",
                        menu="Breakfast Menu",
                        price_list_override="Breakfast Prices",
                        priority=10,
                        idx=1,
                    ),
                    Row(
                        period_name="Lunch",
                        enabled=1,
                        start_time="11:00:00",
                        end_time="16:00:00",
                        weekdays="",
                        order_types="",
                        menu="Lunch Menu",
                        price_list_override="Lunch Prices",
                        priority=10,
                        idx=2,
                    ),
                ]
                if dt == "URY Restaurant Serving Period"
                else []
            )
            frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
                1
                if dt == "Price List" and field == "enabled"
                else ("Lunch Prices" if dt == "Price List" else ("Africa/Kampala" if dt == "URY Branch Operating Schedule" else "Main Menu"))
            )

            # Open check opened during Breakfast (persisted DB state), now re-saved at 13:00 during Lunch
            doc = Row(
                name="POS-0001",
                restaurant="Bistro",
                branch="Main Branch",
                restaurant_table="T-1",
                order_type="Dine In",
                customer="Walk In",
                selling_price_list="Breakfast Prices",
                custom_serving_period="Breakfast",
            )
            doc.is_new = lambda: False
            doc.get_doc_before_save = lambda: Row(
                name="POS-0001",
                selling_price_list="Breakfast Prices",
                custom_serving_period="Breakfast",
                custom_transaction_local_time="08:30:00",
                custom_transaction_weekday="Tue",
            )
            hooks.validate_price_list(doc, "validate")
            self.assertEqual(
                doc.selling_price_list,
                "Breakfast Prices",
                "Open Breakfast check was silently re-priced to Lunch when period boundary passed",
            )


class TestAuthoritativeOutletTimeAndPriceListAntiSpoofing(unittest.TestCase):
    """R2 B1: Direct POS Invoice REST POST/PUT must never trust client-supplied time, daypart, or price list."""

    def _load_hooks(self, frappe_mock, utils):
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury.doctype.ury_order.ury_order": _stub(
                "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()
            ),
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
                "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
                get_applicable_policy=MagicMock(),
            ),
            "ury.ury_pos.discount": _stub(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
            ),
        }
        self.enterContext(patch.dict(sys.modules, modules))
        dayparts = _load("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
        hooks = _load("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")
        return dayparts, hooks

    def _periods(self):
        return [
            Row(
                period_name="All Day Dining",
                enabled=1,
                start_time="08:00:00",
                end_time="18:00:00",
                weekdays="",
                order_types="",
                menu="Standard Menu",
                price_list_override="Standard UGX",
                priority=10,
                idx=1,
            ),
            Row(
                period_name="Happy Hour",
                enabled=1,
                start_time="20:00:00",
                end_time="22:00:00",
                weekdays="",
                order_types="",
                menu="Happy Menu",
                price_list_override="Happy Hour UGX",
                priority=20,
                idx=2,
            ),
        ]

    def test_before_validate_overwrites_spoofed_client_time_period_and_price_list_on_new_post(self):
        frappe_mock, utils = _make_frappe()
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        dayparts, hooks = self._load_hooks(frappe_mock, utils)

        frappe_mock.get_all.side_effect = lambda dt, **kw: (
            self._periods() if dt == "URY Restaurant Serving Period" else []
        )

        def db_get_value(dt, flt=None, field=None, **kw):
            if dt == "POS Profile":
                if isinstance(field, (list, tuple)):
                    return Row(branch="Main Branch A", restaurant="Rest A", ury_offline_capture=0)
                if field == "ury_offline_capture":
                    return 0
                return "Main Branch A" if field == "branch" else "Rest A"
            if dt == "URY Branch Operating Schedule":
                return "Africa/Kampala"
            if dt == "URY Restaurant":
                if isinstance(flt, dict):
                    return "Rest A"
                return "Standard Menu" if field == "active_menu" else 0
            if dt == "Price List":
                if field == "enabled":
                    return 1
                return "Standard UGX"
            if dt == "Item Price":
                if isinstance(flt, dict) and flt.get("price_list") == "Standard UGX" and flt.get("item_code") == "KH-BURGER-18":
                    return 20000.0
                return None
            return None

        frappe_mock.db.get_value.side_effect = db_get_value

        # Real Kampala time is 16:03:00 EAT (13:03:00 UTC) -> All Day Dining / Standard UGX
        real_eat = datetime(2026, 11, 24, 16, 3, 0)
        doc = Row(
            name=None,
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk In Guest",
            set_posting_time=1,
            posting_date=TODAY,
            posting_time="21:03:00",
            custom_transaction_local_time="21:03:00",
            custom_transaction_weekday="Sun",
            custom_serving_period="Happy Hour",
            selling_price_list="Happy Hour UGX",
            items=[Row(item_code="KH-BURGER-18", qty=1, price_list_rate=5000.0, rate=5000.0)],
            flags=SimpleNamespace(),
        )
        doc.is_new = lambda: True
        doc.get_doc_before_save = lambda: None

        with patch.object(
            dayparts,
            "resolve_outlet_local_datetime",
            return_value=(real_eat, "16:03:00", "Tue"),
        ):
            hooks.before_validate(doc, "before_validate")
            hooks.validate_price_list(doc, "validate")

        self.assertEqual(doc.custom_transaction_local_time, "16:03:00")
        self.assertEqual(doc.custom_transaction_weekday, "Tue")
        self.assertEqual(doc.custom_serving_period, "All Day Dining")
        self.assertEqual(doc.selling_price_list, "Standard UGX")
        self.assertEqual(doc.set_posting_time, 0)
        # Repriced from authoritative Standard UGX Item Price (never left at 0.0 or spoofed 5000.0)
        self.assertEqual(doc["items"][0].price_list_rate, 20000.0)
        self.assertEqual(doc["items"][0].rate, 20000.0)

        # Direct REST POST omitting item price_list_rate/rate must also resolve authoritative
        # Item Price before ury_pos_tender.before_validate calls calculate_taxes_and_totals().
        doc_no_rate = Row(
            name=None,
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk In Guest",
            items=[Row(item_code="KH-BURGER-18", qty=1, price_list_rate=None, rate=None)],
            flags=SimpleNamespace(),
        )
        doc_no_rate.is_new = lambda: True
        doc_no_rate.get_doc_before_save = lambda: None

        with patch.object(
            dayparts,
            "resolve_outlet_local_datetime",
            return_value=(real_eat, "16:03:00", "Tue"),
        ):
            hooks.before_validate(doc_no_rate, "before_validate")

        self.assertEqual(doc_no_rate.selling_price_list, "Standard UGX")
        self.assertEqual(doc_no_rate["items"][0].price_list_rate, 20000.0)
        self.assertEqual(doc_no_rate["items"][0].rate, 20000.0)

    def test_before_validate_and_validate_price_list_block_spoofed_put_on_existing_invoice(self):
        frappe_mock, utils = _make_frappe()
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        dayparts, hooks = self._load_hooks(frappe_mock, utils)

        frappe_mock.get_all.side_effect = lambda dt, **kw: (
            self._periods() if dt == "URY Restaurant Serving Period" else []
        )
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            1
            if dt == "Price List" and field == "enabled"
            else (
                "Standard UGX"
                if dt == "Price List"
                else ("Africa/Kampala" if dt == "URY Branch Operating Schedule" else ("Standard Menu" if field == "active_menu" else 0))
            )
        )

        persisted = Row(
            name="MFUA00021",
            pos_profile="POS Profile A",
            restaurant="Rest A",
            branch="Main Branch A",
            order_type="Take Away",
            custom_serving_period="All Day Dining",
            custom_transaction_local_time="16:00:00",
            custom_transaction_weekday="Tue",
            selling_price_list="Standard UGX",
            ury_offline_captured_at=None,
        )
        doc = Row(
            name="MFUA00021",
            pos_profile="POS Profile A",
            restaurant="Rest A",
            branch="Main Branch A",
            order_type="Take Away",
            customer="Walk In Guest",
            # Client PUT tries to overwrite open check to Happy Hour & spoofed time
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="21:03:00",
            custom_transaction_weekday="Sun",
            selling_price_list="Happy Hour UGX",
            items=[Row(item_code="KH-BURGER-18", qty=1, price_list_rate=5000.0, rate=5000.0)],
            flags=SimpleNamespace(),
        )
        doc.is_new = lambda: False
        doc.get_doc_before_save = lambda: persisted

        real_eat = datetime(2026, 11, 24, 16, 5, 0)
        with patch.object(
            dayparts,
            "resolve_outlet_local_datetime",
            return_value=(real_eat, "16:05:00", "Tue"),
        ):
            hooks.before_validate(doc, "before_validate")
            hooks.validate_price_list(doc, "validate")

        self.assertEqual(doc.custom_serving_period, "All Day Dining")
        self.assertEqual(doc.custom_transaction_local_time, "16:00:00")
        self.assertEqual(doc.custom_transaction_weekday, "Tue")
        self.assertEqual(doc.selling_price_list, "Standard UGX")

    def test_before_validate_rejects_client_offline_captured_at_when_offline_capture_disabled_with_pos_code(self):
        frappe_mock, utils = _make_frappe()
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        _, hooks = self._load_hooks(frappe_mock, utils)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            Row(branch="Main Branch A", restaurant="Rest A", ury_offline_capture=0)
            if dt == "POS Profile" and isinstance(field, (list, tuple))
            else (0 if dt == "POS Profile" and field == "ury_offline_capture" else None)
        )
        doc = Row(
            name=None,
            pos_profile="POS Profile A",
            branch="Main Branch A",
            restaurant="Rest A",
            order_type="Take Away",
            ury_offline_captured_at="2026-11-24 21:03:00",
            flags=SimpleNamespace(),
        )
        doc.is_new = lambda: True
        doc.get_doc_before_save = lambda: None

        with self.assertRaisesRegex(Refused, r"\[POS-OFFLINE-CAPTURE-DISABLED\]"):
            hooks.before_validate(doc, "before_validate")

    def test_custom_pos_invoice_set_pos_fields_ignores_spoofed_unsaved_price_list(self):
        frappe_mock, utils = _make_frappe()

        class BasePOSInvoice:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                self.flags = SimpleNamespace()

            def get(self, key, default=None):
                return self.__dict__.get(key, default)

            def set_pos_fields(self, for_validate=False):
                self.selling_price_list = "Standard UGX"
                return {}

        erpnext_pos_mod = _stub(
            "erpnext.accounts.doctype.pos_invoice.pos_invoice",
            POSInvoice=BasePOSInvoice,
            get_stock_availability=MagicMock(),
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "erpnext": _stub("erpnext"),
            "erpnext.accounts": _stub("erpnext.accounts"),
            "erpnext.accounts.doctype": _stub("erpnext.accounts.doctype"),
            "erpnext.accounts.doctype.pos_invoice": _stub("erpnext.accounts.doctype.pos_invoice"),
            "erpnext.accounts.doctype.pos_invoice.pos_invoice": erpnext_pos_mod,
        }
        with patch.dict(sys.modules, modules):
            pos_inv_mod = _load("ury.ury.class_overrides.pos_invoice", URY_PKG / "ury/class_overrides/pos_invoice.py")
            # New unsaved REST POST with spoofed period + price list MUST NOT be preserved
            new_doc = pos_inv_mod.CustomPOSInvoice(
                name=None,
                order_type="Take Away",
                custom_serving_period="Happy Hour",
                selling_price_list="Happy Hour UGX",
            )
            new_doc.is_new = lambda: True
            new_doc.get_doc_before_save = lambda: None
            new_doc.set_pos_fields(for_validate=True)
            self.assertEqual(new_doc.selling_price_list, "Standard UGX")

            # Existing persisted Breakfast check MUST keep Breakfast Prices across period switch
            saved_doc = pos_inv_mod.CustomPOSInvoice(
                name="POS-0001",
                order_type="Dine In",
                custom_serving_period="Breakfast",
                selling_price_list="Breakfast Prices",
            )
            saved_doc.is_new = lambda: False
            saved_doc.get_doc_before_save = lambda: Row(
                custom_serving_period="Breakfast",
                selling_price_list="Breakfast Prices",
            )
            saved_doc.set_pos_fields(for_validate=True)
            self.assertEqual(saved_doc.selling_price_list, "Breakfast Prices")


class TestStampInvoiceOutletClock(unittest.TestCase):
    def setUp(self):
        self.frappe, self.utils = _make_frappe()
        self.enterContext(patch.dict(sys.modules, {"frappe": self.frappe, "frappe.utils": self.utils}))
        mod_path = URY_PKG / "ury_pos/dayparts.py"
        if not mod_path.is_file():
            self.fail(f"Missing module {mod_path}")
        self.dayparts = _load("ury.ury_pos.dayparts", mod_path)

    def test_stamp_invoice_honors_offline_captured_at_over_current_clock(self):
        self.frappe.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Africa/Kampala" if dt == "URY Branch Operating Schedule" else None
        )
        self.frappe.get_all.return_value = [
            Row(
                period_name="Happy Hour",
                enabled=1,
                start_time="16:00:00",
                end_time="19:00:00",
                weekdays="",
                order_types="",
                menu="Happy Menu",
                price_list_override="Happy Prices",
                priority=20,
                idx=1,
            )
        ]
        # Replayed at 21:00 UTC (midnight EAT), but captured offline at 14:15 UTC = 17:15:00 EAT (Tue)
        doc = Row(
            branch="Kampala Branch",
            restaurant="Kampala Bistro",
            order_type="Dine In",
            ury_offline_captured_at=datetime(2026, 11, 24, 14, 15, 0, tzinfo=timezone.utc),
        )
        meta = self.dayparts.stamp_invoice_outlet_time(doc)
        self.assertEqual(doc.custom_transaction_local_time, "17:15:00")
        self.assertEqual(doc.custom_transaction_weekday, "Tue")
        self.assertEqual(doc.custom_serving_period, "Happy Hour")
        self.assertEqual(meta["serving_period"]["period_name"], "Happy Hour")


if __name__ == "__main__":
    unittest.main()
