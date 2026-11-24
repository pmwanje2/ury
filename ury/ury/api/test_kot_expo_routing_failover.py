"""RED/GREEN unit tests for G26 (KDS Expo/All-Day/Rush/VIP/Course/Recall),
G27 (Routing Overrides & Cycle-Safe Failover), and G28 (Backup Printer Fallback).

Site-less unit tests: run directly with
`PYTHONPATH=. python3 -m unittest ury.ury.api.test_kot_expo_routing_failover`.
"""

import importlib
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 24, 14, 0, 0)


class Row(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def _build_frappe_env():
    frappe_mod = MagicMock()
    frappe_mod._ = lambda msg, *args: msg
    frappe_mod.whitelist = lambda *args, **kwargs: (lambda fn: fn)

    class ValidationError(Exception):
        pass

    class PermissionError(Exception):
        pass

    class DoesNotExistError(Exception):
        pass

    frappe_mod.ValidationError = ValidationError
    frappe_mod.PermissionError = PermissionError
    frappe_mod.DoesNotExistError = DoesNotExistError

    def _throw(msg, exc=ValidationError):
        raise exc(msg)

    frappe_mod.throw.side_effect = _throw
    frappe_mod.session = SimpleNamespace(user="chef@example.com")
    frappe_mod.request = SimpleNamespace(method="POST")
    frappe_mod.flags = SimpleNamespace(in_test=True)
    frappe_mod.get_roles.return_value = ["URY Manager"]
    frappe_mod.has_permission.return_value = True
    frappe_mod.as_json.side_effect = json.dumps
    frappe_mod.generate_hash.return_value = "test1234"
    frappe_mod.logger.return_value = MagicMock()

    utils_mod = ModuleType("frappe.utils")
    utils_mod.cint = lambda v: int(float(v)) if v not in (None, "", False) else 0
    utils_mod.flt = lambda v: float(v or 0)
    utils_mod.get_datetime = lambda v=None: (
        NOW if v is None else (v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace(" ", "T")))
    )
    utils_mod.now_datetime = lambda: NOW
    utils_mod.now = lambda: NOW.strftime("%Y-%m-%d %H:%M:%S")
    utils_mod.add_to_date = lambda value, hours=0, minutes=0, **kwargs: (
        (value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace(" ", "T")))
        + timedelta(hours=hours, minutes=minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")
    frappe_mod.utils = utils_mod

    printview_mod = ModuleType("frappe.www.printview")
    printview_mod.validate_print_permission = MagicMock()

    pos_api_mod = ModuleType("ury.ury_pos.api")
    pos_api_mod.getBranch = MagicMock(return_value="Main Branch")

    monitor_mod = ModuleType("ury.ury.printing.print_job_monitor")
    monitor_mod.register_print_job = MagicMock(return_value=True)

    exec_mod = ModuleType("ury.ury.api.ury_kot_item_execution_service")
    exec_mod.mark_item_ready = MagicMock()

    modules = {
        "frappe": frappe_mod,
        "frappe.utils": utils_mod,
        "frappe.www": ModuleType("frappe.www"),
        "frappe.www.printview": printview_mod,
        "ury.ury_pos.api": pos_api_mod,
        "ury.ury.printing.print_job_monitor": monitor_mod,
        "ury.ury.api.ury_kot_item_execution_service": exec_mod,
    }
    return frappe_mod, modules


class TestRoutingOverridesAndFailover(unittest.TestCase):
    def setUp(self):
        self.frappe, self.modules = _build_frappe_env()
        self._patcher = patch.dict(sys.modules, self.modules)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        sys.modules.pop("ury.ury.api.ury_kot_routing", None)
        self.routing = importlib.import_module("ury.ury.api.ury_kot_routing")

    def _unit_fieldnames(self, doctype):
        if doctype == "URY Routing Override":
            return {
                "name", "active", "priority", "branch", "pos_profile",
                "room", "order_type", "item_group", "item", "target_production_unit",
            }
        if doctype == "URY Production Unit":
            return {
                "name", "branch", "enabled", "item_groups",
                "custom_failover_production_unit", "custom_auto_failover_enabled",
                "custom_failover_active", "custom_heartbeat_timeout_seconds",
                "custom_last_heartbeat",
            }
        if doctype == "URY Item Production Configuration":
            return {"name", "item", "branch", "department", "production_unit", "production_policy", "active"}
        if doctype == "URY Production Department":
            return {"name", "enabled"}
        return {"name"}

    def test_routing_override_routes_by_order_type_and_room_precedence(self):
        overrides = [
            Row(name="RO-1", branch="Main Branch", active=1, priority=100,
                order_type="Take Away", room=None, pos_profile=None,
                item_group="Burgers", item=None, target_production_unit="Pack Station"),
            Row(name="RO-2", branch="Main Branch", active=1, priority=100,
                order_type="Dine In", room="Patio", pos_profile=None,
                item_group="Burgers", item=None, target_production_unit="Patio Grill"),
        ]
        units = {
            "Pack Station": Row(enabled=1, custom_failover_active=0, custom_auto_failover_enabled=0, custom_failover_production_unit=None, custom_last_heartbeat=None, custom_heartbeat_timeout_seconds=120),
            "Patio Grill": Row(enabled=1, custom_failover_active=0, custom_auto_failover_enabled=0, custom_failover_production_unit=None, custom_last_heartbeat=None, custom_heartbeat_timeout_seconds=120),
        }

        self.frappe.db.exists.side_effect = lambda dt, name=None: True
        self.frappe.db.get_value.side_effect = lambda dt, name, field, as_dict=False: (
            "Burgers" if (dt, field) == ("Item", "item_group")
            else (units[name] if dt == "URY Production Unit" and as_dict else units.get(name, {}).get(field))
        )
        self.frappe.get_all.side_effect = lambda dt, **kw: overrides if dt == "URY Routing Override" else []

        with patch.object(self.routing, "_doctype_fieldnames", side_effect=self._unit_fieldnames):
            res_takeaway = self.routing.resolve_production_units(
                item_code="BURGER-1", company="Maximus Co", branch="Main Branch", order_type="Take Away"
            )
            res_patio = self.routing.resolve_production_units(
                item_code="BURGER-1", company="Maximus Co", branch="Main Branch", order_type="Dine In", room="Patio"
            )

        self.assertEqual(res_takeaway, ["Pack Station"])
        self.assertEqual(res_patio, ["Patio Grill"])

    def test_disabled_or_failover_station_reroutes_to_healthy_failover_unit(self):
        units = {
            "Hot Kitchen": Row(
                enabled=0, custom_failover_active=0, custom_auto_failover_enabled=0,
                custom_failover_production_unit="Backup Kitchen",
                custom_last_heartbeat=None, custom_heartbeat_timeout_seconds=120,
            ),
            "Cold Kitchen": Row(
                enabled=1, custom_failover_active=0, custom_auto_failover_enabled=1,
                custom_failover_production_unit="Backup Kitchen",
                custom_last_heartbeat=NOW - timedelta(seconds=300),
                custom_heartbeat_timeout_seconds=120,
            ),
            "Backup Kitchen": Row(
                enabled=1, custom_failover_active=0, custom_auto_failover_enabled=0,
                custom_failover_production_unit=None,
                custom_last_heartbeat=NOW, custom_heartbeat_timeout_seconds=120,
            ),
        }

        self.frappe.db.exists.side_effect = lambda dt, name=None: (
            name in units if dt == "URY Production Unit" else True
        )
        self.frappe.db.get_value.side_effect = lambda dt, name, field, as_dict=False: (
            units[name] if dt == "URY Production Unit" and as_dict
            else (units.get(name, {}).get(field) if dt == "URY Production Unit" else None)
        )

        def _get_all(dt, **kw):
            if dt == "URY Item Production Configuration":
                item = kw.get("filters", {}).get("item")
                if item == "STEAK":
                    return [{"name": "CFG-1", "production_unit": "Hot Kitchen", "department": None}]
                if item == "SALAD":
                    return [{"name": "CFG-2", "production_unit": "Cold Kitchen", "department": None}]
            return []

        self.frappe.get_all.side_effect = _get_all

        with patch.object(self.routing, "_doctype_fieldnames", side_effect=self._unit_fieldnames):
            self.assertEqual(
                self.routing.resolve_production_units(item_code="STEAK", company="Co", branch="Main Branch"),
                ["Backup Kitchen"],
            )
            self.assertEqual(
                self.routing.resolve_production_units(item_code="SALAD", company="Co", branch="Main Branch"),
                ["Backup Kitchen"],
            )

    def test_circular_failover_a_to_b_to_a_raises_routing_failover_cycle(self):
        cycle_code = getattr(self.routing, "ROUTING_FAILOVER_CYCLE", None)
        self.assertEqual(cycle_code, "ROUTING_FAILOVER_CYCLE")

        units = {
            "Unit A": Row(
                enabled=0, custom_failover_active=1, custom_auto_failover_enabled=0,
                custom_failover_production_unit="Unit B",
                custom_last_heartbeat=None, custom_heartbeat_timeout_seconds=120,
            ),
            "Unit B": Row(
                enabled=0, custom_failover_active=1, custom_auto_failover_enabled=0,
                custom_failover_production_unit="Unit A",
                custom_last_heartbeat=None, custom_heartbeat_timeout_seconds=120,
            ),
        }
        self.frappe.db.exists.side_effect = lambda dt, name=None: (
            name in units if dt == "URY Production Unit" else True
        )
        self.frappe.db.get_value.side_effect = lambda dt, name, field, as_dict=False: (
            units[name] if dt == "URY Production Unit" and as_dict
            else (units.get(name, {}).get(field) if dt == "URY Production Unit" else None)
        )
        self.frappe.get_all.side_effect = lambda dt, **kw: (
            [{"name": "CFG-1", "production_unit": "Unit A", "department": None}]
            if dt == "URY Item Production Configuration"
            else []
        )

        with patch.object(self.routing, "_doctype_fieldnames", side_effect=self._unit_fieldnames):
            with self.assertRaises(self.routing.RoutingError) as ctx:
                self.routing.resolve_production_units(item_code="ITEM-1", company="Co", branch="Main Branch")
            self.assertEqual(ctx.exception.reason_code, "ROUTING_FAILOVER_CYCLE")

            validate_chain = getattr(self.routing, "validate_failover_chain", None)
            self.assertTrue(callable(validate_chain), "validate_failover_chain is missing")
            with self.assertRaises(self.routing.RoutingError) as ctx2:
                validate_chain("Unit A", "Unit B")
            self.assertEqual(ctx2.exception.reason_code, "ROUTING_FAILOVER_CYCLE")

    def test_routing_override_doctype_and_production_unit_fields_exist(self):
        override_json = APP_ROOT / "ury/doctype/ury_routing_override/ury_routing_override.json"
        self.assertTrue(override_json.exists(), "URY Routing Override DocType JSON missing")
        override_meta = json.loads(override_json.read_text(encoding="utf-8"))
        override_fields = {f["fieldname"] for f in override_meta["fields"]}
        for required in ("branch", "room", "order_type", "pos_profile", "item_group", "item", "target_production_unit", "active", "priority"):
            self.assertIn(required, override_fields)

        unit_meta = json.loads((APP_ROOT / "ury/doctype/ury_production_unit/ury_production_unit.json").read_text(encoding="utf-8"))
        unit_fields = {f["fieldname"] for f in unit_meta["fields"]}
        for required in ("custom_is_expo", "custom_failover_production_unit", "custom_backup_printer", "custom_device_mode", "custom_failover_active"):
            self.assertIn(required, unit_fields)


class TestBackupPrinterFailoverService(unittest.TestCase):
    def setUp(self):
        self.frappe, self.modules = _build_frappe_env()
        self._patcher = patch.dict(sys.modules, self.modules)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        sys.modules.pop("ury.ury.printing.service", None)
        self.service = importlib.import_module("ury.ury.printing.service")

    def test_primary_printer_failure_falls_back_to_backup_printer_and_logs_comment(self):
        primary = SimpleNamespace(
            name="Kitchen Primary", server_ip="192.0.2.10", port=631,
            printer_name="KITCHEN-PRI", custom_backup_printer="Kitchen Backup",
        )
        backup = SimpleNamespace(
            name="Kitchen Backup", server_ip="192.0.2.11", port=631,
            printer_name="KITCHEN-BAK", custom_backup_printer=None,
        )
        kot_doc = MagicMock()
        kot_doc.name = "KOT-001"
        kot_doc.production = "Hot Kitchen"
        kot_doc.pos_profile = "Main Till"
        kot_doc.owner = "chef@example.com"

        def _get_doc(dt, name):
            if dt == "Network Printer Settings":
                return primary if name == "Kitchen Primary" else backup
            return kot_doc

        self.frappe.get_doc.side_effect = _get_doc
        self.frappe.get_print.return_value = b"%PDF-1.4 mock"
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None: (
            "Kitchen Backup" if (dt, name, field) == ("Network Printer Settings", "Kitchen Primary", "custom_backup_printer")
            else 0
        )

        fake_cups = MagicMock()
        connected_servers = []

        def _set_server(ip):
            connected_servers.append(ip)

        def _connect():
            if connected_servers[-1] == "192.0.2.10":
                raise RuntimeError("CUPS primary unreachable")
            conn = MagicMock()
            conn.printFile.return_value = 42
            return conn

        fake_cups.setServer.side_effect = _set_server
        fake_cups.Connection.side_effect = _connect

        with patch.dict(sys.modules, {"cups": fake_cups}):
            res = self.service.submit_and_monitor_print_job(
                doctype="URY KOT",
                name="KOT-001",
                printer_setting="Kitchen Primary",
                print_format="Restaurant Kitchen Ticket 80mm",
                doc=kot_doc,
                job_type="KOT",
            )

        self.assertEqual(res["status"], "Success")
        self.assertTrue(res.get("failover_used"))
        self.assertEqual(res.get("printer"), "Kitchen Backup")
        self.assertEqual(res.get("primary_printer"), "Kitchen Primary")
        kot_doc.add_comment.assert_called()
        comment_text = kot_doc.add_comment.call_args.args[1]
        self.assertIn("[KOT-FAILOVER-PRINTED]", comment_text)
        self.assertIn("Kitchen Backup", comment_text)


class TestKdsExpoPriorityAllDayAndRecallWindow(unittest.TestCase):
    def setUp(self):
        self.frappe, self.modules = _build_frappe_env()
        self._patcher = patch.dict(sys.modules, self.modules)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        sys.modules.pop("ury.ury.api.ury_kot_display", None)
        self.display = importlib.import_module("ury.ury.api.ury_kot_display")

    def test_configurable_recall_window_honors_pos_profile_minutes(self):
        kot_doc = MagicMock()
        kot_doc.name = "KOT-100"
        kot_doc.branch = "Main Branch"
        kot_doc.pos_profile = "Main Till"
        kot_doc.order_status = "Served"
        # Created 40 min ago, production took 15 min -> served 25 min ago.
        kot_doc.creation = NOW - timedelta(minutes=40)
        kot_doc.production_time = "15"
        self.frappe.get_doc.return_value = kot_doc

        # Default 15 min window must reject at 25 min.
        self.frappe.db.get_value.return_value = None
        with self.assertRaisesRegex(self.frappe.ValidationError, r"15 minutes"):
            self.display.recall_kot("KOT-100")

        # Profile configured to 30 min must ALLOW at 25 min.
        self.frappe.db.get_value.side_effect = lambda dt, filters, field=None: (
            30 if (dt, field) == ("POS Profile", "custom_kds_recall_window_minutes") else None
        )
        self.display.recall_kot("KOT-100")
        kot_doc.db_set.assert_called_once_with("order_status", "Ready For Prepare")

    def test_set_check_priority_sets_rush_and_vip_with_audit_and_branch_isolation(self):
        set_prio = getattr(self.display, "set_check_priority", None)
        self.assertTrue(callable(set_prio), "ury_kot_display.set_check_priority is missing")

        inv = MagicMock()
        inv.name = "POS-INV-1"
        inv.branch = "Main Branch"
        inv.custom_is_rush = 0
        inv.custom_is_vip = 0

        kot = MagicMock()
        kot.name = "KOT-1"
        kot.branch = "Main Branch"
        kot.production = "Hot Kitchen"

        self.frappe.get_doc.side_effect = lambda dt, name: inv if dt == "POS Invoice" else kot
        self.frappe.get_all.return_value = [Row(name="KOT-1", production="Hot Kitchen")]

        res = set_prio("POS-INV-1", is_rush=1, is_vip=1, reason="VIP guest waiting")
        self.assertEqual(res["custom_is_rush"], 1)
        self.assertEqual(res["custom_is_vip"], 1)
        self.assertEqual(res["updated_kots"], ["KOT-1"])
        inv.db_set.assert_called()
        inv.add_comment.assert_called()
        kot.db_set.assert_called()

        # Cross-branch user must be rejected.
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Other Branch"
        with self.assertRaises(self.frappe.PermissionError):
            set_prio("POS-INV-1", is_rush=1)

    def test_expo_mode_all_day_summary_course_grouping_and_priority_sort(self):
        kot_hot = Row(
            name="KOT-HOT", invoice="POS-INV-9", production="Hot Kitchen", branch="Main Branch",
            order_status="Ready For Prepare", type="New Order", docstatus=1, verified=0,
            creation="2026-11-24 13:50:00", time="13:50:00", restaurant_table="T-4",
            table_takeaway=0, order_no="9", user="Waiter", custom_is_rush=0, custom_is_vip=0,
            kot_items=[
                Row(name="KI-2", item="CAKE", item_name="Lava Cake", quantity=1, course="Dessert", serve_priority=3, comments=""),
                Row(name="KI-1", item="SOUP", item_name="Tomato Soup", quantity=2, course="Starter", serve_priority=1, comments="No cream"),
            ],
        )
        kot_bar = Row(
            name="KOT-BAR", invoice="POS-INV-9", production="Bar", branch="Main Branch",
            order_status="Ready For Prepare", type="New Order", docstatus=1, verified=0,
            creation="2026-11-24 13:50:05", time="13:50:05", restaurant_table="T-4",
            table_takeaway=0, order_no="9", user="Waiter", custom_is_rush=0, custom_is_vip=0,
            kot_items=[
                Row(name="KI-3", item="JUICE", item_name="Mango Juice", quantity=2, course="Drinks", serve_priority=0, comments=""),
            ],
        )
        kot_rush = Row(
            name="KOT-RUSH", invoice="POS-INV-10", production="Hot Kitchen", branch="Main Branch",
            order_status="Ready For Prepare", type="New Order", docstatus=1, verified=0,
            creation="2026-11-24 13:55:00", time="13:55:00", restaurant_table="T-8",
            table_takeaway=0, order_no="10", user="Waiter", custom_is_rush=1, custom_is_vip=1,
            kot_items=[
                Row(name="KI-4", item="SOUP", item_name="Tomato Soup", quantity=3, course="Starter", serve_priority=1, comments=""),
            ],
        )

        kots_by_name = {"KOT-HOT": kot_hot, "KOT-BAR": kot_bar, "KOT-RUSH": kot_rush}
        units = {
            "Expo": Row(name="Expo", enable_order_type_wise_display_on_mosaic=0, custom_is_expo=1),
            "Hot Kitchen": Row(name="Hot Kitchen", enable_order_type_wise_display_on_mosaic=0, custom_is_expo=0),
            "Bar": Row(name="Bar", enable_order_type_wise_display_on_mosaic=0, custom_is_expo=0),
        }

        self.frappe.get_list.return_value = [Row(name="KOT-HOT"), Row(name="KOT-BAR"), Row(name="KOT-RUSH")]

        def _get_all(dt, **kw):
            if dt == "URY KOT Item Execution":
                return [
                    Row(kot_item="KI-1", state="READY"),
                    Row(kot_item="KI-2", state="READY"),
                    Row(kot_item="KI-3", state="IN_PREPARATION"),
                    Row(kot_item="KI-4", state="QUEUED"),
                ]
            return []

        self.frappe.get_all.side_effect = _get_all
        self.frappe.get_doc.side_effect = lambda dt, name: (
            units[name] if dt == "URY Production Unit" else kots_by_name[name]
        )
        self.frappe.db.get_value.side_effect = lambda dt, filters, field=None, as_dict=False: (
            1 if (dt, filters, field) == ("URY Production Unit", "Expo", "custom_is_expo")
            else (25 if (dt, field) == ("POS Profile", "custom_kds_recall_window_minutes") else 0)
        )

        result = self.display.kot_list(production_unit="Expo")
        self.assertTrue(result.get("is_expo"))
        self.assertEqual(result.get("recall_window_minutes"), 25)

        # Rush/VIP check pinned first!
        self.assertEqual(result["KOT"][0]["name"], "KOT-RUSH")

        # Expo orders group Hot Kitchen + Bar under POS-INV-9 and pin Rush POS-INV-10 first!
        expo_orders = result.get("expo_orders")
        self.assertIsInstance(expo_orders, list)
        self.assertEqual(len(expo_orders), 2)
        self.assertEqual(expo_orders[0]["invoice"], "POS-INV-10")
        inv9 = expo_orders[1]
        self.assertEqual(inv9["invoice"], "POS-INV-9")
        self.assertFalse(inv9["all_stations_ready"])
        station_statuses = {s["production"]: s["status"] for s in inv9["stations"]}
        self.assertEqual(station_statuses, {"Hot Kitchen": "Ready", "Bar": "Preparing"})

        # All-Day summary aggregates Tomato Soup (2 + 3 = 5) across stations/tickets!
        all_day = result.get("all_day_summary")
        self.assertIsInstance(all_day, list)
        soup_total = sum(row["total_qty"] for row in all_day if row["item_name"] == "Tomato Soup")
        self.assertEqual(soup_total, 5)

        # Course groups ordered by serve_priority (Starter before Dessert).
        hot_kot = next(k for k in result["KOT"] if k["name"] == "KOT-HOT")
        courses = [g["course"] for g in hot_kot.get("course_groups", [])]
        self.assertEqual(courses, ["Starter", "Dessert"])

    def test_admin_kot_list_resolves_recall_window_from_station_profile_or_branch(self):
        # All-branch admin board has branch=None; recall window must still read the station's profile/branch.
        self.frappe.session.user = "Administrator"
        self.frappe.get_list.return_value = []
        self.frappe.get_all.return_value = []

        def _get_value(dt, filters, field=None, as_dict=False):
            if (dt, filters, field) == ("URY Production Unit", "Expo", "branch"):
                return "Main Branch"
            if (dt, filters, field) == ("URY Production Unit", "Expo", "pos_profile"):
                return "Main Till"
            if (dt, filters, field) == ("URY Production Unit", "Expo", "custom_is_expo"):
                return 1
            if (dt, filters, field) == ("POS Profile", "Main Till", "custom_kds_recall_window_minutes"):
                return 30
            return 0

        self.frappe.db.get_value.side_effect = _get_value
        result = self.display.kot_list(production_unit="Expo")
        self.assertIsNone(result["Branch"])
        self.assertEqual(result["recall_window_minutes"], 30)

    def test_kot_list_rejects_cross_branch_production_unit_before_leaking_config(self):
        # Branch B cashier requests Branch A's Hot Kitchen -> MUST 403 before returning station_device_config or recall window.
        self.frappe.session.user = "cashier_b@example.com"
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.has_permission.return_value = True
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Branch B"

        unit_a = Row(name="R1 Hot Kitchen", branch="Branch A", pos_profile="R1 Till A", custom_is_expo=0)
        self.frappe.get_doc.side_effect = lambda dt, name: unit_a if dt == "URY Production Unit" else MagicMock()
        self.frappe.get_list.return_value = []
        self.frappe.get_all.return_value = []

        def _get_value(dt, filters, field=None, as_dict=False):
            if dt == "URY Production Unit" and filters == "R1 Hot Kitchen":
                if as_dict:
                    return {
                        "custom_is_expo": 0,
                        "custom_device_mode": "KDS + Auto Print",
                        "custom_printer_transport": "QZ Tray",
                        "custom_primary_printer": "10.0.0.5:9100",
                        "custom_backup_printer": "R1-NP-A-BAK",
                    }
                if field == "branch":
                    return "Branch A"
                if field == "pos_profile":
                    return "R1 Till A"
            if (dt, filters, field) == ("POS Profile", "R1 Till A", "custom_kds_recall_window_minutes"):
                return 30
            return None

        self.frappe.db.get_value.side_effect = _get_value

        with self.assertRaises(self.frappe.PermissionError):
            self.display.kot_list(production_unit="R1 Hot Kitchen")

        # Also recent_served_kots must refuse cross-branch production_unit.
        with self.assertRaises(self.frappe.PermissionError):
            self.display.recent_served_kots(production="R1 Hot Kitchen")

        # Plain Employee without URY Production Unit read permission on same branch MUST also get 403.
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Branch A"
        self.frappe.get_roles.return_value = ["Employee"]
        self.frappe.has_permission.return_value = False
        with self.assertRaises(self.frappe.PermissionError):
            self.display.kot_list(production_unit="R1 Hot Kitchen")

    def test_heartbeat_and_device_config_require_production_unit_permission(self):
        # Plain same-branch Employee without URY Production Unit / URY KOT permissions MUST get 403 on both endpoints.
        self.frappe.session.user = "emp_a@example.com"
        self.frappe.get_roles.return_value = ["Employee"]
        self.frappe.has_permission.return_value = False
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Main Branch"

        unit_doc = Row(name="R1 Hot Kitchen", branch="Main Branch")
        self.frappe.get_doc.side_effect = lambda dt, name: unit_doc

        with self.assertRaises(self.frappe.PermissionError):
            self.display.get_station_device_config("R1 Hot Kitchen")

        with self.assertRaises(self.frappe.PermissionError):
            self.display.record_station_heartbeat("R1 Hot Kitchen")
        self.frappe.db.set_value.assert_not_called()

        # Authorized KDS operator on same branch (read URY Production Unit + write URY KOT) succeeds on both.
        def _has_perm(dt, ptype="read", doc=None, **kwargs):
            if dt == "URY Production Unit" and ptype == "read":
                return True
            if dt == "URY KOT" and ptype == "write":
                return True
            return False

        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.has_permission.side_effect = _has_perm
        self.frappe.db.get_value.return_value = {
            "custom_is_expo": 0,
            "custom_device_mode": "KDS Display",
            "custom_printer_transport": "None",
            "custom_primary_printer": "",
            "custom_backup_printer": "",
        }
        cfg = self.display.get_station_device_config("R1 Hot Kitchen")
        self.assertEqual(cfg["production_unit"], "R1 Hot Kitchen")
        hb = self.display.record_station_heartbeat("R1 Hot Kitchen")
        self.assertEqual(hb["production_unit"], "R1 Hot Kitchen")
        self.frappe.db.set_value.assert_called_once()

    def test_expo_bump_order_rejects_cross_branch_invoice_with_permission_error(self):
        # Branch B cashier calling expo_bump_order for a Branch A invoice MUST get 403 [POS-KOT-EXPO-OTHER-BRANCH],
        # NEVER silent HTTP 200 {"served_kots": []}.
        self.frappe.session.user = "cashier_b@example.com"
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.has_permission.return_value = True
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Branch B"

        all_kots = [Row(name="KOT-A1", invoice="ACC-PSINV-A1", branch="Branch A", order_status="Ready For Prepare")]

        def _get_all(dt, filters=None, **kwargs):
            if dt != "URY KOT":
                return []
            filters = filters or {}
            # Simulate real SQL filtering by branch if caller passed filters["branch"].
            return [
                k for k in all_kots
                if (not filters.get("branch") or k["branch"] == filters["branch"])
                and (not filters.get("invoice") or k["invoice"] == filters["invoice"])
            ]

        self.frappe.get_all.side_effect = _get_all
        self.frappe.get_doc.side_effect = lambda dt, name: Row(
            name=name, branch="Branch A", creation=NOW - timedelta(minutes=2), db_set=MagicMock()
        )
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None: (
            "Branch A" if (dt, name, field) == ("POS Invoice", "ACC-PSINV-A1", "branch") else None
        )

        with self.assertRaisesRegex(self.frappe.PermissionError, r"\[POS-KOT-EXPO-OTHER-BRANCH\]"):
            self.display.expo_bump_order("ACC-PSINV-A1")

        # Even if active KOTs are empty (already served), cross-branch invoice MUST still raise 403.
        all_kots.clear()
        with self.assertRaisesRegex(self.frappe.PermissionError, r"\[POS-KOT-EXPO-OTHER-BRANCH\]"):
            self.display.expo_bump_order("ACC-PSINV-A1")

    def test_process_items_for_cancel_kot_forwards_pos_profile_order_type_and_room(self):
        # G27 parity: cancelling a Patio-routed item must forward pos_profile, order_type, and room
        # to resolve_production_units so Cancel KOT routes to Patio Grill (not Hot Kitchen).
        ctx_mod = ModuleType("ury.ury.api.ury_production_context")
        ctx_mod.resolve_production_context = MagicMock(return_value=None)
        res_mod = ModuleType("ury.ury.api.ury_order_reservation_service")
        res_mod._line_context = MagicMock(return_value={})
        res_mod._line_key = MagicMock(return_value="k1")
        res_mod._line_ref = MagicMock(return_value=None)

        extra = {
            "ury.ury.api.ury_production_context": ctx_mod,
            "ury.ury.api.ury_order_reservation_service": res_mod,
        }
        with patch.dict(sys.modules, extra):
            sys.modules.pop("ury.ury.api.ury_kot_generate", None)
            kot_gen = importlib.import_module("ury.ury.api.ury_kot_generate")

            pos_profile = Row(name="R1 Till A", branch="Branch A", company="Maximus Co")
            pos_invoice = Row(name="ACC-PSINV-PATIO", company="Maximus Co", order_type="Dine In", restaurant_table="R1-T4")
            self.frappe.get_doc.side_effect = lambda dt, name: pos_profile if dt == "POS Profile" else pos_invoice
            self.frappe.db.get_value.side_effect = lambda dt, name, field=None: (
                "Patio" if (dt, name, field) == ("URY Table", "R1-T4", "restaurant_room") else None
            )

            def _resolve(**kw):
                if kw.get("room") == "Patio" and kw.get("pos_profile") == "R1 Till A" and kw.get("order_type") == "Dine In":
                    return ["R1 Patio Grill"]
                return ["R1 Hot Kitchen"]

            with patch.object(kot_gen, "resolve_production_units", side_effect=_resolve) as mock_resolve, \
                 patch.object(kot_gen, "create_cancel_kot_doc", return_value="CNCL-KOT-1") as mock_create_cancel:
                kot_gen.process_items_for_cancel_kot(
                    invoice_id="ACC-PSINV-PATIO",
                    customer="Walk In",
                    restaurant_table="R1-T4",
                    items=[{"item_code": "R1-SOUP", "item_name": "Tomato Soup", "qty": -1, "comments": ""}],
                    comments="Guest changed mind",
                    pos_profile_id="R1 Till A",
                    cancel_kot_naming_series="CNCL-KOT-",
                    kot_type="Partially cancelled",
                    invoiceItems=[{"item_code": "R1-SOUP", "qty": 3, "comments": ""}],
                )

            mock_resolve.assert_called_once_with(
                item_code="R1-SOUP",
                company="Maximus Co",
                branch="Branch A",
                production_policy=None,
                pos_profile="R1 Till A",
                order_type="Dine In",
                room="Patio",
            )
            self.assertEqual(mock_create_cancel.call_args.args[-1], "R1 Patio Grill")


if __name__ == "__main__":
    unittest.main()
