"""Site-less lifecycle contracts for table reservations.

The behaviour pinned here follows the table-management lifecycle of a
restaurant POS reservation book: a booking holds its table from the buffer
before its time until the grace period after it, a guest who arrives is
Seated (which occupies the table and opens the order), the bill closing
completes the reservation, and a cancellation records its reason.

The real reservation modules and the real URY Table Reservation controller
run against an in-memory site. Its get_value, get_all and sql fakes filter
stored rows, so each assertion reads the state a site would hold rather than
which arguments a call received.
"""

import ast
import importlib.util
import json
import re
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-11-23"
RESERVATION_META = json.loads(
    (ROOT / "ury/doctype/ury_table_reservation/ury_table_reservation.json").read_text())
PTYPES = ("read", "write", "create", "delete")
# The DocPerm rows the DocType ships; a tenant adds its own roles as Custom DocPerm.
SHIPPED_DOCPERM = {perm["role"]: {p for p in PTYPES if perm.get(p)} for perm in RESERVATION_META["permissions"]}


class Invalid(Exception):
    """frappe.ValidationError"""


class Refused(Exception):
    """frappe.PermissionError"""


class WriteFailed(Exception):
    """A database write that failed mid-request."""


class Row(dict):
    """frappe._dict: attribute access, missing fields read as None."""

    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


def at(clock):
    return datetime.fromisoformat(f"{DAY} {clock}")


class Site:
    """An in-memory Frappe site: rows per doctype, a site-local clock and an error log."""

    def __init__(self):
        self.now = at("12:00")
        self.rows = {doctype: {} for doctype in (
            "Branch", "URY Room", "URY Table", "URY Table Reservation", "POS Invoice",
            "POS Profile", "User",
        )}
        self.roles = ["System Manager"]
        self.session = SimpleNamespace(user="staff@example.test")
        self.custom_docperm = {}  # role -> ptypes on URY Table Reservation (Custom DocPerm)
        self.user_permissions = {}  # doctype -> allowed names (User Permission rows)
        self.versions = []  # what Frappe's Version log records for each tracked save
        self.errors = []
        self.sql_log = []
        self.events = []  # ordered row locks and SQL reads, for concurrency contracts
        self.fail_writes = set()

    # -- storage -----------------------------------------------------------------
    def put(self, doctype, name, /, **values):
        row = Row(values)
        row["name"] = name
        self.rows[doctype][name] = row
        return row

    def write(self, doctype, name, values):
        if doctype in self.fail_writes:
            raise WriteFailed(f"cannot write {doctype} {name}")
        self.rows[doctype].setdefault(name, Row(name=name)).update(values)

    # -- query semantics -----------------------------------------------------------
    @staticmethod
    def matches(row, filters):
        for key, condition in (filters or {}).items():
            value = row.get(key)
            if isinstance(condition, (list, tuple)):
                op, operand = condition[0], condition[1]
                if op == "in":
                    ok = value in operand
                elif op == "not in":
                    ok = value not in operand
                elif op == ">":
                    ok = value is not None and value > operand
                elif op == ">=":
                    ok = value is not None and value >= operand
                elif op == "<":
                    ok = value is not None and value < operand
                elif op == "like":
                    ok = value is not None and operand.strip("%") in str(value)
                elif op == "between":
                    ok = value is not None and operand[0] <= value <= operand[1]
                else:
                    raise AssertionError(f"unsupported filter {key}: {condition!r}")
            else:
                ok = value == condition
            if not ok:
                return False
        return True

    def select(self, doctype, filters=None):
        return [row for row in self.rows[doctype].values() if self.matches(row, filters)]

    @staticmethod
    def project(row, fields):
        out = Row()
        for field in fields:
            source, _, alias = field.partition(" as ")
            out[(alias or source).strip()] = row.get(source.strip())
        return out

    def get_all(self, doctype, filters=None, fields=None, order_by=None, limit=None,
            limit_page_length=None, pluck=None, **kwargs):
        rows = self.select(doctype, filters)
        if order_by:
            for clause in reversed([c.strip() for c in order_by.split(",")]):
                field, _, direction = clause.partition(" ")
                rows.sort(key=lambda r: (r.get(field) is None, r.get(field)),
                    reverse=direction.strip().lower() == "desc")
        limit = limit or limit_page_length
        if limit:
            rows = rows[:limit]
        if pluck:
            return [row.get(pluck) for row in rows]
        return [self.project(row, fields or ["name"]) for row in rows]

    def has_permission(self, doctype, ptype="read", doc=None, user=None, **kwargs):
        """Native DocPerm (shipped plus Custom DocPerm) and User Permission row scope."""
        if (user or self.session.user) == "Administrator":
            return True
        if doctype != "URY Table Reservation":
            return True
        granted = set()
        for role in self.roles:
            granted |= SHIPPED_DOCPERM.get(role, set()) | self.custom_docperm.get(role, set())
        if ptype not in granted:
            return False
        branches = self.user_permissions.get("Branch")
        branch = doc.get("branch") if doc is not None else None
        return not (branches is not None and branch and branch not in branches)

    def get_list(self, doctype, filters=None, fields=None, **kwargs):
        if "ignore_permissions" in kwargs:
            raise AssertionError("get_list must not bypass permissions")
        if not self.has_permission(doctype, "read"):
            raise Refused(f"No read permission on {doctype}")
        return self.get_all(doctype, filters=filters, fields=fields, **kwargs)

    def get_value(self, doctype, name=None, fieldname="name", as_dict=False, for_update=False, **kwargs):
        if for_update:
            self.events.append(("lock", doctype, name))
        if isinstance(name, dict):
            found = self.select(doctype, name)
            row = found[0] if found else None
        else:
            row = self.rows.get(doctype, {}).get(name)
        if row is None:
            return None
        if isinstance(fieldname, (list, tuple)):
            values = self.project(row, fieldname)
            return values if as_dict else tuple(values.values())
        return row.get(fieldname)

    def exists(self, doctype, name_or_filters=None):
        if isinstance(name_or_filters, dict):
            found = self.select(doctype, name_or_filters)
            return found[0].name if found else None
        return name_or_filters if name_or_filters in self.rows[doctype] else None

    def set_value(self, doctype, name, fieldname, value=None, update_modified=True, **kwargs):
        if name not in self.rows[doctype]:
            raise AssertionError(f"set_value on a missing {doctype} {name}")
        values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
        self.write(doctype, name, values)

    def sql(self, query, params=(), as_dict=False, **kwargs):
        q = " ".join(query.split())
        self.sql_log.append(q)
        self.events.append(("sql", q))
        params = list(params) if isinstance(params, (list, tuple)) else params
        if "`tabPOS Closing Entry`" in q:
            return []  # no till has closed yet, so table-time averages are unset
        if "FROM `tabURY Table` t" in q:
            branch = params[0]
            rooms = {r.name for r in self.rows["URY Room"].values() if r.branch == branch}
            tables = [Row(t) for t in self.rows["URY Table"].values()
                if (t.branch == branch or t.restaurant_room in rooms) and not t.is_take_away]
            return sorted(tables, key=lambda t: (t.restaurant_room or "", t.name))
        if "FROM `tabPOS Invoice`" in q and "custom_merged_tables LIKE %s" in q:
            needle = params[0].strip("%")
            return [Row(name=i.name) for i in self.rows["POS Invoice"].values()
                if i.docstatus == 0 and needle in (i.custom_merged_tables or "")][:1]
        if "FROM `tabURY Table Reservation`" in q:
            rows = list(self.rows["URY Table Reservation"].values())
            status_clause = re.search(r"status IN \(([^)]*)\)", q)
            if status_clause:
                statuses = set(re.findall(r"'([^']+)'", status_clause.group(1)))
                rows = [r for r in rows if r.status in statuses]
            if "reserved_table = %s" in q:
                table = params.pop(0)
                rows = [r for r in rows if r.reserved_table == table]
            elif "reserved_table IN (" in q:
                count = re.search(r"reserved_table IN \(([^)]*)\)", q).group(1).count("%s")
                tables, params = set(params[:count]), params[count:]
                rows = [r for r in rows if r.reserved_table in tables]
            if "WHERE branch = %s" in q:
                branch = params.pop(0)
                rows = [r for r in rows if r.branch == branch]
                if "reserved_at >= %s" in q:
                    start = datetime.fromisoformat(params.pop(0))
                    rows = [r for r in rows if r.reserved_at >= start]
                if "reserved_at <= %s" in q:
                    end = datetime.fromisoformat(params.pop(0))
                    rows = [r for r in rows if r.reserved_at <= end]
                rows.sort(key=lambda r: r.reserved_at)
            if "name != %s" in q:
                excluded = params.pop(0)
                rows = [r for r in rows if r.name != excluded]
            return [Row(r) for r in rows]
        raise AssertionError(f"unexpected SQL: {q}")


class FakeDocument:
    """frappe.model.document.Document over the in-memory site."""

    site = None

    def __init__(self, values, before=None):
        self.__dict__.update(values)
        self.__dict__["_before"] = before
        self.__dict__["flags"] = Row()

    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return None

    def values(self):
        return {k: v for k, v in self.__dict__.items() if k not in ("_before", "flags")}

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def is_new(self):
        return self._before is None

    def get_doc_before_save(self):
        return self._before

    def check_permission(self, ptype="read"):
        if not self.site.has_permission(self.doctype, ptype, doc=self):
            raise Refused(f"No {ptype} permission on {self.doctype} {self.name}")

    def validate(self):
        pass

    def on_update(self):
        pass

    def save(self, ignore_permissions=False):
        self.flags.ignore_permissions = ignore_permissions
        if not ignore_permissions:
            self.check_permission("write")
        self.validate()
        self.__dict__["modified"] = self.site.now
        self.__dict__["modified_by"] = self.site.session.user
        self.site.write(self.doctype, self.name, self.values())
        self.site.versions.append(Row(docname=self.name, user=self.site.session.user,
            ignore_permissions=ignore_permissions, status=self.status, pos_invoice=self.pos_invoice))
        self.on_update()
        self.__dict__["_before"] = Row(self.values())
        return self

    def db_set(self, fieldname, value):
        self.__dict__[fieldname] = value
        self.site.write(self.doctype, self.name, {fieldname: value})


def _utils(site):
    utils = ModuleType("frappe.utils")

    def cint(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def flt(value, precision=None):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def get_datetime(value=None):
        if value is None:
            return site.now
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("T", " "))

    def add_to_date(value, days=0, hours=0, minutes=0, seconds=0, **kwargs):
        return get_datetime(value) + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

    utils.cint = cint
    utils.flt = flt
    utils.get_datetime = get_datetime
    utils.now_datetime = lambda: site.now
    utils.add_to_date = add_to_date
    utils.get_time = lambda value: value if isinstance(value, time) else time.fromisoformat(str(value))
    utils.getdate = lambda value=None: get_datetime(value).date()
    utils.nowdate = lambda: site.now.date().isoformat()
    utils.add_days = lambda value, days: get_datetime(value) + timedelta(days=days)
    # The site runs on Kampala time; Frappe's converter returns an aware datetime.
    utils.get_system_timezone = lambda: "Africa/Kampala"
    utils.convert_utc_to_timezone = lambda value, tz: value.astimezone(ZoneInfo(tz))
    return utils


def _framework(site):
    fr = MagicMock(name="frappe")
    fr.whitelist = lambda *a, **k: (lambda f: f)
    fr._ = lambda message, *a: message
    fr.ValidationError = Invalid
    fr.PermissionError = Refused
    fr.session = site.session
    fr.flags = SimpleNamespace()
    fr.throw.side_effect = lambda message, exc=Invalid, *a, **k: (_ for _ in ()).throw(exc(message))
    fr.log_error.side_effect = lambda *a, **k: site.errors.append((a, k))
    fr.get_traceback.return_value = "traceback"
    fr.get_roles.side_effect = lambda user=None: list(site.roles)
    fr.get_all.side_effect = site.get_all
    fr.get_list.side_effect = site.get_list
    fr.has_permission.side_effect = site.has_permission
    fr.db = SimpleNamespace(
        get_value=site.get_value, get_all=site.get_all, exists=site.exists,
        set_value=site.set_value, sql=site.sql, has_column=lambda doctype, column: True,
        commit=lambda: None, rollback=lambda: None,
    )
    return fr


class _Site(unittest.TestCase):
    def setUp(self):
        self.site = Site()
        self.frappe = _framework(self.site)
        FakeDocument.site = self.site
        document = ModuleType("frappe.model.document")
        document.Document = FakeDocument
        model = ModuleType("frappe.model")
        model.document = document
        pytz = ModuleType("pytz")
        pytz.UTC = timezone.utc
        self.enterContext(patch.dict(sys.modules, {
            "frappe": self.frappe, "frappe.utils": _utils(self.site),
            "frappe.model": model, "frappe.model.document": document, "pytz": pytz,
        }))
        self.average = self.load("ury.ury.report.average_table_time.average_table_time",
            "ury/report/average_table_time/average_table_time.py")
        self.api = self.load("ury.ury.api.table_reservation", "ury/api/table_reservation.py")
        self.scheduler = self.load("ury.ury.api.reservation_scheduler", "ury/api/reservation_scheduler.py")
        self.controller = self.load("ury.ury.doctype.ury_table_reservation.ury_table_reservation",
            "ury/doctype/ury_table_reservation/ury_table_reservation.py")
        self.frappe.get_doc.side_effect = self.get_doc

        # Branch A takes bookings: 30 minute buffer, 15 minute grace, no till closed yet (90 minute stay).
        self.site.put("Branch", "Branch A", custom_enable_reservation=1, custom_buffer_time=30,
            custom_grace_period=15, custom_avg_table_time_last_day=0, custom_avg_table_time_last_week=0)
        self.site.put("Branch", "Branch Off", custom_enable_reservation=0, custom_buffer_time=30,
            custom_grace_period=15, custom_avg_table_time_last_day=0, custom_avg_table_time_last_week=0)
        self.site.put("URY Room", "Dining", branch="Branch A")
        self.site.put("URY Room", "Terrace", branch="Branch Off")
        for name, seats in (("T1", 4), ("T2", 4), ("T3", 4)):
            self.table(name, seats)
        self.table("T9", 4, branch="Branch Off", room="Terrace")

    def load(self, name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def get_doc(self, doctype, name=None):
        if doctype == "URY Table Reservation":
            row = self.site.rows[doctype][name]
            return self.controller.URYTableReservation(dict(row), before=Row(row))
        if doctype == "POS Profile":
            return self.site.rows[doctype][name]
        raise AssertionError(f"unexpected get_doc {doctype} {name}")

    def table(self, name, seats, branch="Branch A", room="Dining", minimum=None, occupied=0):
        return self.site.put("URY Table", name, branch=branch, restaurant_room=room, no_of_seats=seats,
            minimum_seating=minimum, occupied=occupied, latest_invoice_time=None, is_take_away=0)

    def booking(self, name, clock, table="T1", status="Confirmed", branch="Branch A", created=None,
            guest="Grace", pax=4, modified=None, **extra):
        return self.site.put("URY Table Reservation", name, doctype="URY Table Reservation",
            branch=branch, reserved_table=table, customer="CUST-1", customer_name=guest,
            customer_phone="+256772123456", no_of_pax=pax, reserved_at=at(clock), status=status,
            creation=at(created) if created else self.site.now, modified=modified or self.site.now,
            reserved_by="staff@example.test", **extra)

    def status(self, name):
        return self.site.rows["URY Table Reservation"][name].status

    def clock(self, value):
        self.site.now = at(value)

    def till(self, billing_roles=(), manager_roles=()):
        self.site.put("POS Profile", "Till A", branch="Branch A", disabled=0,
            role_allowed_for_billing=[Row(role=r) for r in billing_roles],
            transfer_role_permissions=[Row(role=r) for r in manager_roles])


class TestNoShowFollowsTheSiteClock(_Site):
    """B1: the grace period runs on the site's own clock; no second time zone is guessed."""

    def sweep_at(self, clock):
        self.clock(clock)
        self.scheduler.process_reservation_no_shows()

    def test_kampala_booking_is_held_until_its_grace_ends(self):
        self.booking("TR-1", "19:00", created="16:00")
        for clock in ("16:45", "19:14"):
            with self.subTest(clock=clock):
                self.sweep_at(clock)
                self.assertEqual(self.status("TR-1"), "Confirmed")
        self.sweep_at("19:15")
        self.assertEqual(self.status("TR-1"), "No Show")

    def test_short_notice_booking_is_not_released_minutes_after_it_is_made(self):
        self.booking("TR-1", "19:00", created="18:00")
        self.sweep_at("18:15")
        self.assertEqual(self.status("TR-1"), "Confirmed")
        self.sweep_at("19:15")
        self.assertEqual(self.status("TR-1"), "No Show")


class TestBufferKeepsTheTableClear(_Site):
    """B2: each booking holds [time - buffer, time + stay); two holds on one table never overlap."""

    def test_a_new_booking_keeps_the_buffer_on_both_sides_of_an_existing_one(self):
        self.booking("TR-1", "18:00")
        # 19:45 starts its own buffer inside the 18:00 stay; 16:15 ends (17:45) inside the 18:00 buffer.
        for clock in ("19:45", "16:45", "16:15"):
            with self.subTest(refused=clock):
                with self.assertRaisesRegex(Invalid, "already has a reservation"):
                    self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} {clock}:00")
        for clock in ("20:00", "16:00"):
            with self.subTest(admitted=clock):
                self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} {clock}:00")

    def test_availability_hides_a_table_whose_hold_overlaps(self):
        self.booking("TR-1", "18:00")
        names = lambda clock: [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} {clock}:00")]
        self.assertEqual(names("19:45"), ["T2", "T3"])
        self.assertEqual(names("16:15"), ["T2", "T3"])
        self.assertEqual(names("20:00"), ["T1", "T2", "T3"])
        self.assertEqual(names("16:00"), ["T1", "T2", "T3"])


class TestConcurrentBookingsSerialiseOnTheTable(_Site):
    """Two hosts (or a host and an agent) booking one table at once must not both pass the overlap check."""

    def test_the_table_row_is_locked_before_its_bookings_are_read(self):
        self.booking("TR-1", "18:00")
        self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 20:00:00")
        events = self.site.events
        locks = [i for i, e in enumerate(events) if e == ("lock", "URY Table", "T1")]
        reads = [i for i, e in enumerate(events) if e[0] == "sql" and "FROM `tabURY Table Reservation`" in e[1]]
        self.assertTrue(locks, "the URY Table row is never locked FOR UPDATE")
        self.assertTrue(reads, "no overlap read ran")
        self.assertLess(locks[0], reads[0])


class TestPartySizeFitsTheTable(_Site):
    """B3: a party must fit both the seats and the table's minimum seating."""

    def test_a_party_below_the_table_minimum_is_refused(self):
        self.table("T8", 8, minimum=4)
        with self.assertRaisesRegex(Invalid, "minimum"):
            self.api.validate_reservation_rules("T8", "Branch A", f"{DAY} 19:00:00", no_of_pax=2)
        self.api.validate_reservation_rules("T8", "Branch A", f"{DAY} 19:00:00", no_of_pax=4)
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 19:00:00", no_of_pax=1)

    def test_availability_offers_only_tables_that_fit_the_party(self):
        self.site.rows["URY Table"].clear()
        self.table("T4", 4)
        self.table("T8", 8, minimum=4)
        self.table("T10", 10, minimum=8)
        names = lambda **kw: [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} 19:00:00", **kw)]
        self.assertEqual(names(no_of_pax=6), ["T8"])
        self.assertEqual(names(), ["T10", "T4", "T8"])

    def test_availability_is_empty_on_a_branch_without_reservations(self):
        self.assertEqual(self.api.get_available_tables_for_reservation("Branch Off", f"{DAY} 19:00:00"), [])
        self.assertEqual(self.site.sql_log, [])


class TestSeatedLifecycle(_Site):
    """B4-B6: Requested/Confirmed -> Seated -> Completed, with Cancelled and No Show as exits."""

    def transition(self, before, after):
        self.booking("TR-1", "19:00", status=before)
        doc = self.get_doc("URY Table Reservation", "TR-1")
        doc.status = after
        doc.validate_status_transitions()

    def test_seated_sits_between_confirmed_and_completed(self):
        meta = json.loads((ROOT / "ury/doctype/ury_table_reservation/ury_table_reservation.json").read_text())
        status = next(f for f in meta["fields"] if f["fieldname"] == "status")
        self.assertEqual(status["options"].split("\n"),
            ["Requested", "Confirmed", "Seated", "Completed", "Cancelled", "No Show"])
        for before, after in (("Confirmed", "Seated"), ("Seated", "Completed")):
            with self.subTest(allowed=(before, after)):
                self.transition(before, after)
        for before, after in (("Confirmed", "Completed"), ("Seated", "Cancelled")):
            with self.subTest(refused=(before, after)):
                with self.assertRaisesRegex(Invalid, "Invalid status transition"):
                    self.transition(before, after)

    def test_seating_occupies_the_table_and_completion_does_not(self):
        self.clock("18:55")
        self.booking("TR-1", "19:00")
        self.api.update_reservation_status("TR-1", "Seated")
        self.assertEqual(self.status("TR-1"), "Seated")
        self.assertEqual(self.site.rows["URY Table"]["T1"].occupied, 1)
        # The bill closed and released the table; completing the booking must not re-occupy it.
        self.site.rows["URY Table"]["T1"].occupied = 0
        self.api.update_reservation_status("TR-1", "Completed")
        self.assertEqual(self.status("TR-1"), "Completed")
        self.assertEqual(self.site.rows["URY Table"]["T1"].occupied, 0)

    def test_a_cancellation_needs_a_reason_and_keeps_it(self):
        self.booking("TR-1", "19:00")
        doc = self.get_doc("URY Table Reservation", "TR-1")
        doc.status = "Cancelled"
        with self.assertRaisesRegex(Invalid, "reason"):
            doc.save()
        self.assertEqual(self.status("TR-1"), "Confirmed")
        self.api.update_reservation_status("TR-1", "Cancelled", cancel_reason="Guest called")
        stored = self.site.rows["URY Table Reservation"]["TR-1"]
        self.assertEqual((stored.status, stored.cancel_reason), ("Cancelled", "Guest called"))

    def test_the_cancellation_reason_field_appears_and_is_required_only_when_cancelled(self):
        meta = json.loads((ROOT / "ury/doctype/ury_table_reservation/ury_table_reservation.json").read_text())
        field = next((f for f in meta["fields"] if f["fieldname"] == "cancel_reason"), None)
        self.assertIsNotNone(field)
        self.assertEqual(field["depends_on"], "eval:doc.status=='Cancelled'")
        self.assertEqual(field["mandatory_depends_on"], "eval:doc.status=='Cancelled'")
        self.assertEqual(meta["field_order"][meta["field_order"].index("status") + 1], "cancel_reason")

    def test_a_captain_with_the_shipped_write_docperm_may_seat_and_cancel(self):
        # D4: write DocPerm decides; URY Captain ships write on URY Table Reservation.
        self.site.roles = ["URY Captain"]
        self.till(billing_roles=["URY Cashier"], manager_roles=["URY Manager"])
        self.clock("18:55")
        self.booking("TR-1", "19:00")
        self.booking("TR-2", "21:00", table="T2")
        self.api.update_reservation_status("TR-1", "Seated")
        self.assertEqual(self.status("TR-1"), "Seated")
        self.api.update_reservation_status("TR-2", "Cancelled", cancel_reason="Guest called")
        self.assertEqual(self.status("TR-2"), "Cancelled")


class TestNativeDocPermAuthorisesEditAndCancel(_Site):
    """D4: native DocPerm on URY Table Reservation decides who edits and cancels a booking.

    A hotel's concierge desk books and cancels dining reservations, so a tenant
    grants Front Desk, Hotel Admin and F&B Manager write as Custom DocPerm; no
    POS billing role is needed. The till's billing roles stay an extra path.
    Cancelling needs no second approver, but it always needs a reason.
    """

    HOTEL_ROLES = (("Front Desk", "T1"), ("Hotel Admin", "T2"), ("F&B Manager", "T3"))

    def setUp(self):
        super().setUp()
        self.till(billing_roles=["URY Cashier"], manager_roles=["URY Manager"])
        self.clock("18:00")

    def as_role(self, role, ptypes=("read", "write")):
        self.site.roles = [role]
        self.site.custom_docperm[role] = set(ptypes)
        self.site.session.user = f"{role.split()[0].lower().strip('&')}@hotel.example.test"

    def cancelling(self, name):
        doc = self.get_doc("URY Table Reservation", name)
        doc.status = "Cancelled"
        doc.cancel_reason = "Guest called the hotel"
        return doc

    def test_hotel_roles_with_write_docperm_edit_and_cancel_without_a_billing_role(self):
        for role, table in self.HOTEL_ROLES:
            with self.subTest(role=role):
                self.as_role(role)
                name = f"TR-{table}"
                self.booking(name, "19:00", table=table)
                self.api.update_table_reservation(name, no_of_pax=2)
                self.api.update_reservation_status(name, "Cancelled", cancel_reason="Guest called the hotel")
                stored = self.site.rows["URY Table Reservation"][name]
                self.assertEqual((stored.no_of_pax, stored.status, stored.cancel_reason),
                    (2, "Cancelled", "Guest called the hotel"))

    def test_the_booker_cancels_their_own_booking_without_a_second_approver(self):
        self.as_role("Front Desk")
        self.booking("TR-1", "19:00").reserved_by = self.site.session.user
        self.api.update_reservation_status("TR-1", "Cancelled", cancel_reason="Guest called the hotel")
        self.assertEqual(self.status("TR-1"), "Cancelled")

    def test_a_cancellation_still_needs_a_reason(self):
        self.as_role("Front Desk")
        self.booking("TR-1", "19:00")
        for reason in (None, "   "):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(Invalid, "reason"):
                    self.api.update_reservation_status("TR-1", "Cancelled", cancel_reason=reason)
                self.assertEqual(self.status("TR-1"), "Confirmed")

    def test_without_write_docperm_or_a_billing_role_a_cancel_is_refused(self):
        self.as_role("Front Desk", ptypes=("read",))
        self.booking("TR-1", "19:00")
        with self.assertRaises(Refused):
            self.api.update_reservation_status("TR-1", "Cancelled", cancel_reason="Guest called the hotel")
        with self.assertRaises(Refused):
            self.cancelling("TR-1").validate_status_transitions()
        self.assertEqual(self.status("TR-1"), "Confirmed")

    def test_write_docperm_is_checked_against_the_booking_itself(self):
        # A User Permission keeps this desk on another branch: native row scope applies.
        self.as_role("Front Desk")
        self.site.user_permissions["Branch"] = {"Branch Off"}
        self.booking("TR-1", "19:00")
        with self.assertRaises(Refused):
            self.cancelling("TR-1").validate_status_transitions()

    def test_the_till_billing_roles_remain_an_additional_path(self):
        self.till(billing_roles=["Till Operator"])
        self.site.roles = ["Till Operator"]  # bills on the till, holds no reservation DocPerm
        self.booking("TR-1", "19:00")
        self.cancelling("TR-1").validate_status_transitions()
        self.assertTrue(self.api.is_user_authorized_for_reservation_status_change(branch="Branch A"))


class TestTheSeatedPartyFeedsTheOrder(_Site):
    """B7: the order opened on an occupied table takes its guest from the Seated reservation."""

    def test_the_seated_reservation_prefills_the_order_and_a_completed_one_does_not(self):
        self.clock("19:10")
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.booking("TR-S", "19:00", status="Seated", guest="Grace", modified=at("19:00"))
        self.booking("TR-C", "12:30", status="Completed", guest="Earlier Guest", modified=at("19:05"))
        found = self.api.get_completed_reservation_for_table("T1", "Branch A")
        self.assertEqual((found or {}).get("name"), "TR-S")
        self.assertEqual(found["customer_name"], "Grace")
        self.site.rows["URY Table Reservation"]["TR-S"].status = "Completed"
        self.assertIsNone(self.api.get_completed_reservation_for_table("T1", "Branch A"))


class TestClosingTheBillCompletesTheReservation(_Site):
    """B8: submitting the table's POS Invoice completes the Seated reservation and links the bill."""

    def setUp(self):
        super().setUp()
        # A till that does not restrict billing to named roles; whoever settles the bill closes it.
        self.site.roles = ["URY Cashier"]
        self.till()
        self.clock("20:30")

    def bill(self, table="T1", merged="", branch="Branch A"):
        return Row(doctype="POS Invoice", name="INV-1", restaurant_table=table, custom_merged_tables=merged,
            branch=branch, order_type="Dine In", docstatus=1)

    def stored(self, name):
        row = self.site.rows["URY Table Reservation"][name]
        return row.status, row.pos_invoice

    def test_the_seated_reservation_on_the_table_is_completed_with_the_bill(self):
        self.booking("TR-1", "19:00", status="Seated")
        self.booking("TR-2", "21:30", table="T1")
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))
        self.assertEqual(self.stored("TR-2"), ("Confirmed", None))

    def test_a_seated_reservation_on_a_merged_table_is_completed_too(self):
        self.booking("TR-2", "19:00", table="T2", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill(merged="T2,T3"))
        self.assertEqual(self.stored("TR-2"), ("Completed", "INV-1"))

    def test_a_branch_without_reservations_is_untouched(self):
        self.booking("TR-9", "19:00", table="T9", branch="Branch Off", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill(table="T9", branch="Branch Off"))
        self.assertEqual(self.stored("TR-9"), ("Seated", None))
        self.assertEqual(self.site.errors, [])

    def test_a_failed_completion_is_logged_and_never_blocks_the_payment(self):
        self.booking("TR-1", "19:00", status="Seated")
        self.site.fail_writes.add("URY Table Reservation")
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.assertTrue(self.site.errors)

    def test_the_completion_runs_on_pos_invoice_submit(self):
        tree = ast.parse((ROOT / "hooks.py").read_text())
        doc_events = next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "doc_events" for t in node.targets))
        self.assertIn("ury.ury.api.table_reservation.complete_seated_reservation_on_submit",
            doc_events["POS Invoice"]["on_submit"])


class TestTheLockEndsAtGrace(_Site):
    """B9: a booking blocks walk-ins from its buffer until its grace end; the lock check is a pure read."""

    def test_the_lock_runs_from_buffer_to_grace_end_and_the_check_writes_nothing(self):
        self.booking("TR-1", "19:00")
        self.clock("18:40")
        self.assertTrue(self.api.check_table_reservation("T1")["is_lock_window_active"])
        self.clock("19:20")
        held = self.api.check_table_reservation("T1")
        self.assertEqual((held or {}).get("name"), "TR-1")
        self.assertIs(held["is_lock_window_active"], False)
        self.assertEqual(self.status("TR-1"), "Confirmed")

    def test_the_floor_list_sweeps_no_shows_once_per_read(self):
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.site.rows["URY Table"]["T2"].occupied = 1
        self.booking("TR-1", "19:00", table="T3")
        self.clock("19:20")
        real = self.scheduler.process_reservation_no_shows
        with patch.object(self.scheduler, "process_reservation_no_shows", wraps=real) as sweep:
            listed = self.api.get_active_reservations("Branch A")
        self.assertEqual(sweep.call_count, 1)
        self.assertEqual([(r.name, r.status) for r in listed], [("TR-1", "No Show")])


if __name__ == "__main__":
    unittest.main()
