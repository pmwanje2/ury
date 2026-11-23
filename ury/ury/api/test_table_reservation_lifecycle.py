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
import contextlib
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


class Invalid(Exception):
    """frappe.ValidationError"""


class Refused(Exception):
    """frappe.PermissionError"""


class WriteFailed(Exception):
    """A database write that failed mid-request."""


class Deadlock(Exception):
    """frappe.QueryDeadlockError: MariaDB 1213, or 1020 under innodb_snapshot_isolation=ON."""


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
        self.errors = []
        self.sql_log = []
        self.events = []  # ordered row locks and SQL reads, for concurrency contracts
        self.fail_writes = set()
        self.versions = []  # Version rows: track_changes records who changed a status
        # MariaDB 11.8 runs innodb_snapshot_isolation=ON: a locking read that meets a
        # row committed after this request's snapshot raises 1020 instead of reading it.
        self.committed_after_snapshot = False
        # The shipped Custom Field fixture: frappe.get_meta reads each field's default from it.
        self.custom_fields = json.loads((ROOT / "fixtures/custom_field.json").read_text())

    def meta(self, doctype):
        fields = {f["fieldname"]: Row(f) for f in self.custom_fields if f.get("dt") == doctype}
        return SimpleNamespace(get_field=fields.get)

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
                elif op == "!=":
                    ok = value != operand
                elif op == "like":
                    ok = value is not None and operand.strip("%") in str(value)
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
            if self.committed_after_snapshot and q.endswith("FOR UPDATE"):
                raise Deadlock("1020 Record has changed since last read in table 'tabURY Table Reservation'")
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
            # A SELECT returns only the columns it names.
            columns = [c.strip() for c in re.search(r"SELECT (.*?) FROM", q).group(1).split(",")]
            return [self.project(r, columns) for r in rows]
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
        return True  # native DocPerm is outside these contracts

    def validate(self):
        pass

    def on_update(self):
        pass

    def save(self, ignore_permissions=False):
        self.flags.ignore_permissions = ignore_permissions
        self.validate()
        self.__dict__["modified"] = self.site.now
        self.site.write(self.doctype, self.name, self.values())
        before = self._before
        if before is not None and before.get("status") != self.status:
            # URY Table Reservation tracks changes: the Version names the session user.
            self.site.versions.append(Row(docname=self.name, owner=sys.modules["frappe"].session.user,
                status=(before.get("status"), self.status)))
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
    fr.QueryDeadlockError = Deadlock
    fr.session = SimpleNamespace(user="staff@example.test")
    fr.flags = SimpleNamespace()
    fr.throw.side_effect = lambda message, exc=Invalid, *a, **k: (_ for _ in ()).throw(exc(message))
    fr.log_error.side_effect = lambda *a, **k: site.errors.append((a, k))
    fr.get_traceback.return_value = "traceback"
    fr.get_roles.side_effect = lambda user=None: list(site.roles)
    fr.get_all.side_effect = site.get_all
    fr.get_meta.side_effect = site.meta
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

        # Branch A takes bookings: 30 minute buffer, 15 minute grace, a 90 minute target turn
        # time and no till closed yet (so a 90 minute stay). Other timings take their seeds.
        self.site.put("Branch", "Branch A", custom_enable_reservation=1, custom_buffer_time=30,
            custom_grace_period=15, custom_target_turn_time=90,
            custom_avg_table_time_last_day=0, custom_avg_table_time_last_week=0)
        self.site.put("Branch", "Branch Off", custom_enable_reservation=0, custom_buffer_time=30,
            custom_grace_period=15, custom_target_turn_time=90,
            custom_avg_table_time_last_day=0, custom_avg_table_time_last_week=0)
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

    def test_the_overlap_read_is_itself_a_locking_read(self):
        # Under REPEATABLE READ a plain SELECT after the row lock still reads the
        # snapshot the request opened before it waited, so it misses a booking
        # the first writer committed meanwhile. Only a locking read sees it.
        self.booking("TR-1", "18:00")
        self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 20:00:00")
        events = self.site.events
        lock = events.index(("lock", "URY Table", "T1"))
        reads = [(i, e[1]) for i, e in enumerate(events)
            if e[0] == "sql" and "FROM `tabURY Table Reservation`" in e[1]]
        self.assertEqual(len(reads), 1)
        index, query = reads[0]
        self.assertGreater(index, lock)
        self.assertTrue(query.rstrip().endswith("FOR UPDATE"), query)

    def test_a_booking_committed_after_this_request_s_snapshot_is_a_clean_refusal(self):
        # On the estate's MariaDB the locking read raises 1020 for that booking:
        # the host is asked to try again, never shown a database error.
        self.booking("TR-1", "22:00")
        self.site.committed_after_snapshot = True
        with self.assertRaisesRegex(Invalid, "try again"):
            self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 19:00:00")

    def test_the_booking_table_is_indexed_so_the_lock_touches_only_its_own_bookings(self):
        # Without an index the locking read scans, and locks, every booking on the site.
        meta = json.loads((ROOT / "ury/doctype/ury_table_reservation/ury_table_reservation.json").read_text())
        field = next(f for f in meta["fields"] if f["fieldname"] == "reserved_table")
        self.assertEqual(field.get("search_index"), 1)


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

    def test_a_captain_without_a_billing_role_may_seat_but_not_cancel(self):
        self.site.roles = ["URY Captain"]
        self.till(billing_roles=["URY Cashier"], manager_roles=["URY Manager"])
        self.clock("18:55")
        self.booking("TR-1", "19:00")
        self.booking("TR-2", "21:00", table="T2")
        self.api.update_reservation_status("TR-1", "Seated")
        self.assertEqual(self.status("TR-1"), "Seated")
        with self.assertRaises(Refused):
            self.api.update_reservation_status("TR-2", "Cancelled", cancel_reason="Guest called")
        self.assertEqual(self.status("TR-2"), "Confirmed")


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
        # The till names its billing role, and the cashier who settles the bill
        # closes the party's visit under that same authority.
        self.site.roles = ["URY Cashier"]
        self.frappe.session.user = "cashier@example.test"
        self.till(billing_roles=["URY Cashier"])
        self.clock("20:30")

    def bill(self, table="T1", merged="", branch="Branch A", name="INV-1", opened="19:05", **extra):
        values = dict(doctype="POS Invoice", name=name, restaurant_table=table, custom_merged_tables=merged,
            branch=branch, order_type="Dine In", docstatus=1, is_return=0, customer="CUST-1",
            creation=at(opened))
        values.update(extra)
        return Row(values)

    def check(self, name, opened, table="T1", **extra):
        """An open (draft) check on the table."""
        values = dict(restaurant_table=table, custom_merged_tables="", branch="Branch A",
            order_type="Dine In", docstatus=0, invoice_printed=0, creation=at(opened))
        values.update(extra)
        return self.site.put("POS Invoice", name, **values)

    def close(self, name, **extra):
        """Submit an open check: it leaves the open list and its on_submit runs."""
        row = self.site.rows["POS Invoice"][name]
        row.docstatus = 1
        self.api.complete_seated_reservation_on_submit(self.bill(
            table=row.restaurant_table, merged=row.custom_merged_tables, name=name,
            opened=row.creation.strftime("%H:%M"), custom_split_group=row.custom_split_group,
            custom_split_from=row.custom_split_from, **extra))

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

    def test_a_credit_note_never_completes_the_party(self):
        self.booking("TR-1", "19:00", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill(is_return=1, return_against="INV-LUNCH"))
        self.assertEqual(self.stored("TR-1"), ("Seated", None))

    def test_only_a_dine_in_bill_of_the_party_s_own_branch_completes_it(self):
        self.site.put("Branch", "Branch B", custom_enable_reservation=1, custom_buffer_time=30,
            custom_grace_period=15, custom_target_turn_time=90,
            custom_avg_table_time_last_day=0, custom_avg_table_time_last_week=0)
        self.booking("TR-1", "19:00", status="Seated")
        for bill in (self.bill(order_type="Take Away"), self.bill(branch="Branch B")):
            with self.subTest(bill=(bill.order_type, bill.branch)):
                self.api.complete_seated_reservation_on_submit(bill)
                self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))

    def test_the_first_check_of_a_split_bill_leaves_the_party_seated(self):
        self.booking("TR-1", "19:00", status="Seated")
        self.check("INV-1", "19:05", custom_split_group="grp1")
        self.check("INV-2", "19:50", custom_split_group="grp1", custom_split_from="INV-1")
        self.close("INV-1")
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.close("INV-2")
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-2"))

    def test_a_walk_in_check_on_the_released_table_neither_takes_nor_holds_the_party(self):
        # The party's check was printed, which released the table, and a walk-in
        # was seated there. Whichever pays first, the party is closed by its own bill.
        for first in ("INV-W", "INV-A"):
            with self.subTest(pays_first=first):
                self.site.rows["URY Table Reservation"].clear()
                self.site.rows["POS Invoice"].clear()
                self.booking("TR-1", "19:00", status="Seated")
                self.check("INV-A", "19:05", invoice_printed=1)
                self.check("INV-W", "19:40", customer="Guest")
                self.close(first)
                if first == "INV-W":
                    self.assertEqual(self.stored("TR-1"), ("Seated", None))
                    self.close("INV-A")
                self.assertEqual(self.stored("TR-1"), ("Completed", "INV-A"))
                if first == "INV-A":
                    self.close("INV-W")
                    self.assertEqual(self.stored("TR-1"), ("Completed", "INV-A"))

    def test_a_party_already_linked_to_another_bill_is_not_relinked(self):
        self.booking("TR-1", "19:00", status="Seated", pos_invoice="INV-0")
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Seated", "INV-0"))

    def test_a_bill_opened_after_the_party_s_hold_ended_does_not_complete_it(self):
        # 19:00 + a 90 minute stay: the hold ended at 20:30, so a check opened at
        # 20:40 belongs to someone else.
        self.booking("TR-1", "19:00", status="Seated")
        self.clock("21:30")
        self.api.complete_seated_reservation_on_submit(self.bill(opened="20:40", customer="Guest"))
        self.assertEqual(self.stored("TR-1"), ("Seated", None))

    def test_the_closer_s_own_authority_decides_and_the_version_names_them(self):
        # No bypass: a closer the till does not authorise to complete a
        # reservation leaves the party Seated, and the payment still stands.
        self.site.roles = ["URY Captain"]
        self.frappe.session.user = "captain@example.test"
        self.booking("TR-1", "19:00", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill())  # never raises into the payment
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.assertTrue(self.site.errors)
        self.assertEqual(self.site.versions, [])
        # The till's cashier completes it, and the Version records that cashier.
        self.site.roles = ["URY Cashier"]
        self.frappe.session.user = "cashier@example.test"
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))
        self.assertEqual([(v.owner, v.status) for v in self.site.versions if v.docname == "TR-1"],
            [("cashier@example.test", ("Seated", "Completed"))])

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


class TestASeatedPartyHoldsItsTable(_Site):
    """Simphony counts a seated table as occupied: no booking may overlap it, buffer included."""

    def test_an_occupied_table_refuses_a_booking_whose_buffer_starts_inside_the_stay(self):
        # A walk-in check opened at 18:00 runs to 19:30 (90 minute stay). A 19:45
        # booking's 30 minute buffer starts at 19:15, inside that stay.
        self.clock("18:05")
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.site.put("POS Invoice", "INV-W", restaurant_table="T1", docstatus=0, creation=at("18:00"))
        with self.assertRaisesRegex(Invalid, "currently occupied"):
            self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 19:45:00")
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 20:00:00")
        names = lambda clock: [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} {clock}:00")]
        self.assertEqual(names("19:45"), ["T2", "T3"])
        self.assertEqual(names("20:00"), ["T1", "T2", "T3"])

    def test_a_seated_reservation_holds_its_window_even_when_the_table_reads_free(self):
        self.clock("18:20")
        self.booking("TR-1", "18:00", status="Seated")
        for clock in ("18:35", "19:45"):
            with self.subTest(refused=clock):
                with self.assertRaisesRegex(Invalid, "already has a reservation"):
                    self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} {clock}:00")
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 20:00:00")
        names = lambda clock: [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} {clock}:00")]
        self.assertEqual(names("18:35"), ["T2", "T3"])
        self.assertEqual(names("19:45"), ["T2", "T3"])
        self.assertEqual(names("20:00"), ["T1", "T2", "T3"])

    def test_a_party_seated_at_six_without_an_order_keeps_the_table_at_six_thirty_one(self):
        self.clock("17:55")
        self.booking("TR-1", "18:00")
        self.clock("18:00")
        self.api.update_reservation_status("TR-1", "Seated")
        self.clock("18:31")
        self.api.get_active_reservations("Branch A")  # the floor read runs the occupancy reset
        self.assertEqual(self.status("TR-1"), "Seated")
        self.assertEqual(self.site.rows["URY Table"]["T1"].occupied, 1)
        for clock in ("18:35", "19:45"):
            with self.subTest(refused=clock):
                with self.assertRaises(Invalid):
                    self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} {clock}:00")
        offered = [t.name for t in self.api.get_available_tables_for_reservation("Branch A", f"{DAY} 18:35:00")]
        self.assertNotIn("T1", offered)


class TestTheSeatedVisitEnds(_Site):
    """Clearing a table ends its seated visit; a later walk-in's bill never completes an old one."""

    def seat_at_six(self):
        self.clock("17:55")
        self.booking("TR-1", "18:00")
        self.clock("18:00")
        self.api.update_reservation_status("TR-1", "Seated")

    def stored(self, name):
        row = self.site.rows["URY Table Reservation"][name]
        return row.status, row.pos_invoice

    def test_a_party_that_never_orders_is_completed_when_its_hold_ends_and_the_table_is_freed(self):
        self.seat_at_six()
        self.clock("19:29")
        self.api.get_active_reservations("Branch A")
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.clock("19:30")  # 18:00 + the 90 minute stay
        self.api.get_active_reservations("Branch A")
        self.assertEqual(self.stored("TR-1"), ("Completed", None))
        self.assertEqual(self.site.rows["URY Table"]["T1"].occupied, 0)
        # A walk-in seated on the freed table pays: the old reservation is untouched.
        self.site.roles = ["System Manager"]
        self.api.complete_seated_reservation_on_submit(Row(doctype="POS Invoice", name="INV-W",
            restaurant_table="T1", custom_merged_tables="", branch="Branch A", order_type="Dine In",
            docstatus=1, is_return=0, customer="Guest", creation=at("19:40")))
        self.assertEqual(self.stored("TR-1"), ("Completed", None))

    def test_a_cleared_table_ends_its_seated_visit_but_a_printed_check_keeps_it(self):
        self.seat_at_six()
        # The party's check was printed, which released the table: it is still paying.
        self.site.put("POS Invoice", "INV-A", restaurant_table="T1", custom_merged_tables="",
            branch="Branch A", docstatus=0, invoice_printed=1, creation=at("18:10"))
        self.site.rows["URY Table"]["T1"].occupied = 0
        self.clock("18:40")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        # The check was cancelled and the table cleared: the visit is over.
        self.site.rows["POS Invoice"]["INV-A"].docstatus = 2
        self.clock("18:41")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.stored("TR-1"), ("Completed", None))


class TestUnseat(_Site):
    """Simphony's Unseat returns a party seated by mistake and frees the table, but never once it has ordered."""

    def seat(self):
        self.clock("18:55")
        self.booking("TR-1", "19:00")
        self.api.update_reservation_status("TR-1", "Seated")

    def test_unseat_returns_the_booking_to_confirmed_and_frees_the_table(self):
        self.site.roles = ["URY Captain"]  # whoever may seat a party may unseat it
        self.till(billing_roles=["URY Cashier"], manager_roles=["URY Manager"])
        self.seat()
        self.clock("18:58")
        self.api.update_reservation_status("TR-1", "Confirmed")
        self.assertEqual(self.status("TR-1"), "Confirmed")
        table = self.site.rows["URY Table"]["T1"]
        self.assertEqual((table.occupied, table.latest_invoice_time), (0, None))

    def test_unseat_is_refused_once_the_party_has_an_open_check(self):
        self.seat()
        self.site.put("POS Invoice", "INV-1", restaurant_table="T1", custom_merged_tables="",
            branch="Branch A", docstatus=0, invoice_printed=0, creation=at("18:57"))
        with self.assertRaisesRegex(Invalid, "open check"):
            self.api.update_reservation_status("TR-1", "Confirmed")
        self.assertEqual(self.status("TR-1"), "Seated")
        self.assertEqual(self.site.rows["URY Table"]["T1"].occupied, 1)


class TestSeatingNeedsAFreeTable(_Site):
    """A party is seated only at a table with no open check and no other seated party."""

    def test_seating_is_refused_on_a_table_with_an_open_check(self):
        self.clock("18:50")
        self.booking("TR-1", "19:00")
        for name, values in (("INV-W", dict(restaurant_table="T1", custom_merged_tables="")),
                ("INV-M", dict(restaurant_table="T2", custom_merged_tables="T3,T1"))):
            with self.subTest(check=name):
                self.site.rows["POS Invoice"].clear()
                self.site.put("POS Invoice", name, branch="Branch A", docstatus=0, invoice_printed=0,
                    creation=at("18:30"), **values)
                with self.assertRaisesRegex(Invalid, "open check"):
                    self.api.update_reservation_status("TR-1", "Seated")
                self.assertEqual(self.status("TR-1"), "Confirmed")

    def test_seating_is_refused_while_another_party_is_seated_there(self):
        self.booking("TR-A", "18:00", status="Seated")
        self.booking("TR-B", "21:00")
        self.clock("19:00")
        with self.assertRaisesRegex(Invalid, "already seated"):
            self.api.update_reservation_status("TR-B", "Seated")
        self.assertEqual(self.status("TR-B"), "Confirmed")


class TestCompletionAndCancellationNeedAuthority(_Site):
    """No save path, however it is called, completes or cancels without the user's own authority."""

    def test_a_system_flagged_save_cannot_cancel_for_a_captain(self):
        self.site.roles = ["URY Captain"]
        self.till(billing_roles=["URY Cashier"], manager_roles=["URY Manager"])
        self.booking("TR-1", "21:00")
        doc = self.get_doc("URY Table Reservation", "TR-1")
        doc.status = "Cancelled"
        doc.cancel_reason = "Guest called"
        with self.assertRaises(Refused):
            doc.save(ignore_permissions=True)
        self.assertEqual(self.status("TR-1"), "Confirmed")


class TestTheFloorListReleasesAtGrace(_Site):
    """The server's lock flag ends at grace even when the no-show sweep has not run."""

    def test_an_unswept_booking_is_not_locked_after_its_grace_ends(self):
        self.booking("TR-1", "19:00", table="T3")
        with patch.object(self.scheduler, "process_reservation_no_shows"):
            self.clock("18:40")
            self.assertIs(self.api.get_active_reservations("Branch A")[0]["is_lock_window_active"], True)
            self.clock("19:15")
            listed = self.api.get_active_reservations("Branch A")
        self.assertEqual(listed[0].status, "Confirmed")
        self.assertIs(listed[0]["is_lock_window_active"], False)

    def test_the_floor_list_carries_the_cancel_reason_and_the_bill(self):
        self.clock("17:00")
        self.booking("TR-C", "19:00", status="Cancelled", cancel_reason="Guest called")
        self.booking("TR-D", "18:00", table="T2", status="Completed", pos_invoice="INV-9")
        listed = {r.name: r for r in self.api.get_active_reservations("Branch A")}
        self.assertEqual(listed["TR-C"].get("cancel_reason"), "Guest called")
        self.assertEqual(listed["TR-D"].get("pos_invoice"), "INV-9")


class TestAnUnsetGraceIsNeverZero(_Site):
    """A blank or zero grace period falls back to the shipped Branch default, never No Show at the booked minute."""

    def shipped_default(self, fieldname):
        fields = json.loads((ROOT / "fixtures/custom_field.json").read_text())
        return int(next(f for f in fields if f.get("dt") == "Branch" and f["fieldname"] == fieldname)["default"])

    def test_a_blank_or_zero_grace_uses_the_shipped_default(self):
        default = self.shipped_default("custom_grace_period")
        self.assertEqual(default, 15)
        for unset in (None, "", 0):
            with self.subTest(grace=unset):
                self.site.rows["Branch"]["Branch A"].custom_grace_period = unset
                self.assertEqual(self.api.get_branch_reservation_settings("Branch A")["grace_period"], default)

    def test_a_booking_on_a_branch_with_no_grace_is_held_until_the_shipped_grace_ends(self):
        self.site.rows["Branch"]["Branch A"].custom_grace_period = 0
        self.booking("TR-1", "19:00", created="18:00")
        for clock in ("19:00", "19:14"):
            with self.subTest(clock=clock):
                self.clock(clock)
                self.scheduler.process_reservation_no_shows()
                self.assertEqual(self.status("TR-1"), "Confirmed")
        self.clock("19:15")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "No Show")


# R3/R4: every reservation timing is a per-Branch setting, seeded by the Custom Field fixture.
# Simphony configures these per property or revenue centre (TMS Parameters, Reservation Periods),
# and has no setting for a seated-party lookback or a measured-average minimum (R4 drops both).
TIMING_SEEDS = {
    "custom_grace_period": "15",  # Auto Abandon Wait List Delay; Simphony documents no default
    "custom_unset_grace_policy": "Use Default Grace Period",  # today's behaviour
    "custom_min_reservation_notice": "-1",  # Minimum Reservation Notice; no Simphony default, today's 1 minute back
    "custom_target_turn_time": "60",  # Target Turn Time: Simphony's documented default
}
TIMING_COLUMNS = [field for field in TIMING_SEEDS if field != "custom_grace_period"]
DROPPED_SETTINGS = ("custom_min_avg_table_time", "custom_seated_lookup_hours")


class _Timing(_Site):
    def branch(self, **values):
        self.site.rows["Branch"]["Branch A"].update(values)

    def seed(self, fieldname, value):
        """Customize Form: the site changes the default its Branch field ships with."""
        field = next((f for f in self.site.custom_fields
            if f.get("dt") == "Branch" and f["fieldname"] == fieldname), None)
        self.assertIsNotNone(field, f"Branch has no {fieldname} setting")
        field["default"] = value

    def settings(self, branch="Branch A"):
        return self.api.get_branch_reservation_settings(branch)

    def measured(self, minutes):
        """Both table-time averages, as the till closings would have measured them."""
        stack = contextlib.ExitStack()
        for module in (self.api, self.average):
            for name in ("get_branch_last_day_avg_time", "get_branch_last_week_avg_time"):
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, return_value=minutes))
        return stack

    def doc_events(self):
        tree = ast.parse((ROOT / "hooks.py").read_text())
        return next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "doc_events" for t in node.targets))

    def hooked(self, doctype, event):
        hooks = self.doc_events().get(doctype, {}).get(event, [])
        return [hooks] if isinstance(hooks, str) else hooks


class TestReservationTimingShipsOnTheBranch(_Timing):
    """Each timing is a Branch Custom Field beside buffer and grace, with its seed."""

    def branch_fields(self):
        return {f["fieldname"]: f for f in self.site.custom_fields if f.get("dt") == "Branch"}

    def test_each_timing_setting_ships_with_its_seed(self):
        fields = self.branch_fields()
        self.assertEqual({name: (fields.get(name) or {}).get("default") for name in TIMING_SEEDS}, TIMING_SEEDS)
        self.assertEqual(fields["custom_unset_grace_policy"]["fieldtype"], "Select")
        self.assertEqual(fields["custom_unset_grace_policy"]["options"].split("\n"),
            ["Use Default Grace Period", "Refuse to Enable"])
        self.assertEqual((fields["custom_target_turn_time"]["fieldtype"],
            fields["custom_target_turn_time"]["non_negative"]), ("Int", 1))
        # A negative notice accepts a time that far back, so this one field may go below zero.
        self.assertEqual((fields["custom_min_reservation_notice"]["fieldtype"],
            fields["custom_min_reservation_notice"]["non_negative"]), ("Int", 0))

    def test_settings_simphony_does_not_have_are_gone(self):
        # Simphony binds a seated party to its table and check (no lookback window), and its
        # Target Turn Time governs the hold (no measured-average minimum).
        fields = self.branch_fields()
        self.assertEqual([name for name in DROPPED_SETTINGS if name in fields], [])
        hooks_source = (ROOT / "hooks.py").read_text()
        self.assertEqual([name for name in DROPPED_SETTINGS if f"Branch-{name}" in hooks_source], [])
        settings = self.settings()
        self.assertEqual([key for key in ("min_avg_table_time", "seated_lookup_hours") if key in settings], [])

    def test_the_settings_sit_beside_buffer_and_grace(self):
        fields = self.branch_fields()
        followers = {f["insert_after"]: name for name, f in fields.items()}
        chain, after = [], "custom_reservation_settings_section"
        while after in followers:
            after = followers[after]
            chain.append(after)
        self.assertEqual(chain, ["custom_enable_reservation", "custom_buffer_time", "custom_grace_period",
            *TIMING_COLUMNS, "custom_avg_table_time_last_day", "custom_avg_table_time_last_week"])
        for name in TIMING_COLUMNS:
            self.assertEqual(fields[name]["depends_on"], "eval:doc.custom_enable_reservation == 1", name)
            self.assertEqual(fields[name]["module"], "URY", name)

    def test_a_branch_reads_as_disabled_until_migrate_adds_every_timing_column(self):
        for column in TIMING_COLUMNS:
            with self.subTest(missing=column):
                self.frappe.db.has_column = lambda doctype, name, missing=column: name != missing
                self.assertFalse(self.api.reservations_enabled("Branch A"))
        self.frappe.db.has_column = lambda doctype, name: True
        self.assertTrue(self.api.reservations_enabled("Branch A"))

    def test_before_migrate_the_settings_read_is_off_and_silent(self):
        # A missing column means "disabled": no query error, and no Error Log row per read.
        read = self.site.get_value

        def get_value(doctype, name=None, fieldname="name", *args, **kwargs):
            names = fieldname if isinstance(fieldname, (list, tuple)) else [fieldname]
            if doctype == "Branch" and "custom_target_turn_time" in names:
                raise WriteFailed("(1054, \"Unknown column 'custom_target_turn_time' in 'field list'\")")
            return read(doctype, name, fieldname, *args, **kwargs)

        self.frappe.db.get_value = get_value
        self.frappe.db.has_column = lambda doctype, name: name != "custom_target_turn_time"
        self.assertEqual(self.settings()["enable_reservation"], 0)
        self.assertEqual(self.site.errors, [])

    def test_the_arabic_catalogue_keeps_its_header_first(self):
        catalogue = (ROOT / "translations/ar.csv").read_text(encoding="utf-8")
        self.assertEqual(catalogue.splitlines()[0], "source,target,context")
        for source in ("Target Turn Time (Minutes)", "Minimum Reservation Notice (Minutes)",
                "Set a target turn time of at least one minute before switching reservations on for this branch."):
            self.assertIn(f"\n{source},", catalogue)
        for gone in ("Seated Party Lookup (Hours)", "Minimum Average Table Time (Minutes)"):
            self.assertNotIn(gone, catalogue)


class TestTargetTurnTimeIsBranchConfiguration(_Timing):
    """Simphony Target Turn Time: how long a booking holds its table. It alone sets the hold."""

    def test_the_turn_time_sets_each_booking_s_hold(self):
        self.branch(custom_target_turn_time=45)
        self.assertEqual(self.settings()["calculated_duration"], 45)
        self.booking("TR-1", "18:00")
        # 18:00 holds T1 until 18:45, where a 19:15 booking's 30 minute buffer starts.
        with self.assertRaisesRegex(Invalid, "already has a reservation"):
            self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 19:14:00")
        self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 19:15:00")
        offered = lambda clock: [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} {clock}:00")]
        self.assertEqual(offered("19:14"), ["T2", "T3"])
        self.assertEqual(offered("19:15"), ["T1", "T2", "T3"])
        with patch.object(self.scheduler, "process_reservation_no_shows"):
            self.assertEqual(self.api.get_active_reservations("Branch A")[0]["duration_minutes"], 45)

    def test_an_occupied_table_is_expected_free_after_the_turn_time(self):
        self.branch(custom_target_turn_time=45)
        self.site.rows["URY Table"]["T1"].update(occupied=1, latest_invoice_time=at("12:00"))
        # Free at 12:45, so a 13:15 booking (its buffer starts 12:45) fits and 13:14 does not.
        with self.assertRaisesRegex(Invalid, "currently occupied"):
            self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 13:14:00")
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 13:15:00")
        self.assertIn("T1", [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} 13:15:00")])

    def test_a_seated_party_s_hold_runs_out_after_the_turn_time(self):
        self.branch(custom_target_turn_time=45)
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.booking("TR-1", "19:00", status="Seated")
        self.clock("19:44")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "Seated")
        self.clock("19:45")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "Completed")

    def test_an_unset_or_cleared_turn_time_takes_the_seeded_one(self):
        # Clearing an Int field in Desk saves 0: a 0-minute stay is meaningless, so it reads as unset.
        for unset in (None, "", 0, -5):
            with self.subTest(turn=unset):
                self.branch(custom_target_turn_time=unset)
                self.assertEqual(self.settings()["calculated_duration"], 60)
                self.assertEqual(self.api.reservation_stay_minutes(self.settings()), 60)
        self.seed("custom_target_turn_time", "75")
        self.assertEqual(self.settings()["calculated_duration"], 75)
        self.assertEqual(self.settings(None)["calculated_duration"], 75)

    def test_a_cleared_turn_time_never_lets_two_bookings_share_a_minute(self):
        self.branch(custom_target_turn_time=0, custom_buffer_time=0)
        self.booking("TR-1", "19:00")
        with self.assertRaisesRegex(Invalid, "already has a reservation"):
            self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 19:00:00")

    def test_a_cleared_turn_time_never_ends_a_seated_visit_at_once(self):
        self.branch(custom_target_turn_time=0)
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.booking("TR-1", "19:00", status="Seated")
        self.clock("19:00")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "Seated")

    def test_a_measured_average_never_overrides_the_turn_time(self):
        # Simphony keeps Average Dining Time in reporting; the hold follows the Target Turn Time.
        for minutes in (20, 200):
            with self.subTest(average=minutes), self.measured(minutes):
                settings = self.settings()
                self.assertEqual(settings["calculated_duration"], 90)
                self.assertEqual(self.api.reservation_stay_minutes(settings), 90)
                self.assertEqual(settings["avg_table_time_last_day"], minutes)  # still reported
        with self.measured(20):
            self.booking("TR-1", "18:00")  # holds T1 until 19:30, inside a 19:45 booking's buffer
            with self.assertRaisesRegex(Invalid, "already has a reservation"):
                self.api.validate_reservation_conflicts("T1", "Branch A", f"{DAY} 19:45:00")
            self.site.rows["URY Table"]["T2"].update(occupied=1, latest_invoice_time=at("12:00"))
            with self.assertRaisesRegex(Invalid, "currently occupied"):  # free at 13:30, not 12:20
                self.api.validate_reservation_rules("T2", "Branch A", f"{DAY} 13:59:00")

    def test_branch_save_refuses_a_cleared_turn_time_on_a_branch_taking_bookings(self):
        def validate(**values):
            doc = Row(doctype="Branch", name="Branch A", custom_enable_reservation=1,
                custom_unset_grace_policy="Use Default Grace Period", custom_grace_period=15,
                custom_target_turn_time=60)
            doc.update(values)
            self.api.validate_branch_reservation_settings(doc)

        # Desk sends JSON null (None) when an operator clears an Int field, and Frappe
        # otherwise stores cint(None) == 0. All non-positive/blank inputs are refused
        # identically whenever the column exists (Simphony requires positive minutes).
        for cleared in (None, "", "   ", 0, -5):
            with self.subTest(turn=cleared), self.assertRaisesRegex(Invalid, "target turn time"):
                validate(custom_target_turn_time=cleared)
        validate(custom_target_turn_time=1)
        for off_val in (None, "", 0, -5):
            with self.subTest(disabled_turn=off_val):
                validate(custom_target_turn_time=off_val, custom_enable_reservation=0)
        # Before migrate adds the column, saving a Branch without it is not refused.
        self.frappe.db.has_column = lambda doctype, name: name != "custom_target_turn_time"
        validate(custom_target_turn_time=None)


class TestMinimumReservationNoticeIsBranchConfiguration(_Timing):
    """Simphony Minimum Reservation Notice applies to the booking time: when it is made or moved."""

    def test_a_booking_inside_the_notice_is_refused(self):
        self.branch(custom_min_reservation_notice=30)
        self.assertEqual(self.settings()["min_reservation_notice"], 30)
        with self.assertRaisesRegex(Invalid, "30 minutes"):
            self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 12:29:00")
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 12:30:00")

    def test_the_seeded_notice_accepts_a_minute_back_and_no_more(self):
        self.assertEqual(self.settings()["min_reservation_notice"], -1)
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 11:59:00")
        with self.assertRaisesRegex(Invalid, "in the past"):
            self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 11:58:59")

    def test_a_branch_can_accept_a_longer_delay(self):
        self.branch(custom_min_reservation_notice=-5)
        self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 11:55:00")
        with self.assertRaisesRegex(Invalid, "in the past"):
            self.api.validate_reservation_rules("T1", "Branch A", f"{DAY} 11:54:59")

    def test_an_edit_that_keeps_the_time_is_not_held_to_the_notice(self):
        # Booked at 11:00 for 12:15; by 12:00 it is inside a 30 minute notice, and the guest calls.
        self.branch(custom_min_reservation_notice=30)
        self.booking("TR-1", "12:15", created="11:00", pax=2)
        self.api.update_table_reservation("TR-1", notes="Needs a high chair")
        self.api.update_table_reservation("TR-1", no_of_pax=3, customer_phone="+256700000001")
        self.api.update_table_reservation("TR-1", table="T2")
        row = self.site.rows["URY Table Reservation"]["TR-1"]
        self.assertEqual((row.comments, row.no_of_pax, row.reserved_table), ("Needs a high chair", 3, "T2"))

    def test_a_party_already_due_can_still_be_edited(self):
        # 11:50, inside its grace at 12:00: the seeded notice no longer refuses it as "in the past".
        self.booking("TR-1", "11:50", created="09:00")
        self.api.update_table_reservation("TR-1", notes="Running ten minutes late")
        self.assertEqual(self.site.rows["URY Table Reservation"]["TR-1"].comments, "Running ten minutes late")

    def test_moving_the_time_is_held_to_the_notice(self):
        self.branch(custom_min_reservation_notice=30)
        self.booking("TR-1", "15:00", created="09:00")
        with self.assertRaisesRegex(Invalid, "30 minutes"):
            self.api.update_table_reservation("TR-1", reserved_at=f"{DAY} 12:20:00")
        self.assertEqual(self.site.rows["URY Table Reservation"]["TR-1"].reserved_at, at("15:00"))
        self.api.update_table_reservation("TR-1", reserved_at=f"{DAY} 12:45:00")
        self.assertEqual(str(self.site.rows["URY Table Reservation"]["TR-1"].reserved_at)[:16], f"{DAY} 12:45")


class TestTheSeatedPartyIsBoundToItsTableAndCheck(_Timing):
    """Simphony seats a party into a guest check on its table; no lookback window decides the party."""

    def setUp(self):
        super().setUp()
        self.site.rows["URY Table"]["T1"].occupied = 1

    def check(self, name, table="T1", **values):
        check = Row(doctype="POS Invoice", name=name, restaurant_table=table, branch="Branch A",
            order_type="Dine In", is_return=0, docstatus=0)
        check.update(values)
        return check

    def found(self, pos_invoice=None, branch="Branch A"):
        return (self.api.get_completed_reservation_for_table("T1", branch, pos_invoice=pos_invoice) or {}).get("name")

    def test_the_table_s_seated_party_is_found_however_long_ago_it_was_seated(self):
        self.booking("TR-S", "00:30", status="Seated", modified=at("00:30"))
        self.clock("23:30")
        self.assertEqual(self.found(), "TR-S")
        self.assertEqual(self.found(branch=None), "TR-S")  # the table's own branch

    def test_the_check_opened_for_the_party_is_bound_to_it(self):
        self.booking("TR-S", "19:00", status="Seated")
        self.booking("TR-2", "21:00", table="T2")  # another table's booking stays unbound
        self.api.bind_seated_reservation_to_check(self.check("INV-1"))
        self.assertEqual(self.site.rows["URY Table Reservation"]["TR-S"].pos_invoice, "INV-1")
        self.assertIsNone(self.site.rows["URY Table Reservation"]["TR-2"].pos_invoice)
        # A second check on the table never takes over the bound party.
        self.api.bind_seated_reservation_to_check(self.check("INV-2"))
        self.assertEqual(self.site.rows["URY Table Reservation"]["TR-S"].pos_invoice, "INV-1")

    def test_only_a_dine_in_sale_on_a_branch_taking_bookings_binds(self):
        self.booking("TR-S", "19:00", status="Seated")
        for name, values in (("INV-R", dict(is_return=1)), ("INV-T", dict(order_type="Take Away")),
                ("INV-O", dict(branch="Branch Off"))):
            with self.subTest(check=name):
                self.api.bind_seated_reservation_to_check(self.check(name, **values))
                self.assertIsNone(self.site.rows["URY Table Reservation"]["TR-S"].pos_invoice)

    def test_a_bound_party_prefills_only_its_own_check(self):
        self.booking("TR-S", "19:00", status="Seated", pos_invoice="INV-1")
        self.site.put("POS Invoice", "INV-1", docstatus=0)
        self.assertEqual(self.found(pos_invoice="INV-1"), "TR-S")
        self.assertIsNone(self.found(pos_invoice="INV-2"))
        self.assertIsNone(self.found())  # a new check on the table is someone else's

    def test_the_check_binding_runs_when_a_check_is_created(self):
        self.assertIn("ury.ury.api.table_reservation.bind_seated_reservation_to_check",
            self.hooked("POS Invoice", "after_insert"))


class TestUnsetGracePolicyIsBranchConfiguration(_Timing):
    """A blank or zero grace takes the seeded grace, or, under "Refuse to Enable", stops the branch switching on."""

    def validate(self, **values):
        doc = Row(doctype="Branch", name="Branch A", custom_enable_reservation=1,
            custom_unset_grace_policy="Refuse to Enable", custom_grace_period=0,
            custom_target_turn_time=60)
        doc.update(values)
        self.api.validate_branch_reservation_settings(doc)
        return doc

    def test_an_unset_grace_takes_the_seed_the_site_ships(self):
        self.seed("custom_grace_period", "20")
        for unset in (None, "", 0):
            with self.subTest(grace=unset):
                self.branch(custom_grace_period=unset)
                self.assertEqual(self.settings()["grace_period"], 20)
        self.booking("TR-1", "19:00", created="18:00")
        self.clock("19:19")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "Confirmed")
        self.clock("19:20")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.status("TR-1"), "No Show")

    def test_refuse_to_enable_stops_a_branch_without_a_grace_from_switching_on(self):
        for unset in (None, "", "   ", 0, -5):
            with self.subTest(grace=unset), self.assertRaisesRegex(Invalid, "grace period"):
                self.validate(custom_grace_period=unset)
        self.validate(custom_grace_period=10)
        self.validate(custom_enable_reservation=0)
        self.validate(custom_unset_grace_policy="Use Default Grace Period")
        self.validate(custom_unset_grace_policy=None)  # an unset policy takes the seeded one
        self.seed("custom_unset_grace_policy", "Refuse to Enable")
        with self.assertRaisesRegex(Invalid, "grace period"):
            self.validate(custom_unset_grace_policy=None)

    def test_use_default_grace_restores_the_seed_on_save_for_cleared_zero_or_negative_grace(self):
        for unset in (None, "", "   ", 0, -5):
            with self.subTest(grace=unset):
                doc = self.validate(
                    custom_unset_grace_policy="Use Default Grace Period",
                    custom_grace_period=unset,
                )
                self.assertEqual(doc.custom_grace_period, 15)
        self.seed("custom_grace_period", "20")
        doc = self.validate(custom_unset_grace_policy="Use Default Grace Period", custom_grace_period=None)
        self.assertEqual(doc.custom_grace_period, 20)
        off = self.validate(
            custom_enable_reservation=0,
            custom_unset_grace_policy="Use Default Grace Period",
            custom_grace_period=0,
        )
        self.assertEqual(off.custom_grace_period, 0)

    def test_a_refusing_branch_saved_without_a_grace_is_still_never_zero(self):
        # Reached only by a write that skipped Branch validate: the booking is still held.
        self.branch(custom_unset_grace_policy="Refuse to Enable", custom_grace_period=0)
        self.assertEqual(self.settings()["grace_period"], 15)

    def test_the_policy_runs_on_every_branch_save(self):
        self.assertIn("ury.ury.api.table_reservation.validate_branch_reservation_settings",
            self.hooked("Branch", "validate"))


class TestWalkInInBufferDoesNotBlockNonTimeEdits(_Timing):
    """P1-3: a walk-in running into the buffer never blocks edits that keep both table and time."""

    def setUp(self):
        super().setUp()
        self.branch(custom_target_turn_time=60, custom_buffer_time=30)
        self.clock("12:00")
        # Confirmed at 12:35 (buffer starts 12:05). Walk-in on T1 since 11:15 finishes at 12:15 > 12:05.
        self.booking("TR-1", "12:35", table="T1", created="10:00", pax=2)
        self.site.rows["URY Table"]["T1"].update(occupied=1, latest_invoice_time=at("11:15"))

    def test_editing_notes_covers_or_phone_on_same_table_and_time_succeeds(self):
        self.api.update_table_reservation(
            "TR-1", notes="Needs high chair", no_of_pax=3, customer_phone="+256772888999"
        )
        row = self.site.rows["URY Table Reservation"]["TR-1"]
        self.assertEqual((row.comments, row.no_of_pax, row.customer_phone), ("Needs high chair", 3, "+256772888999"))
        # Capacity check still runs on same-table edits.
        with self.assertRaisesRegex(Invalid, "capacity"):
            self.api.update_table_reservation("TR-1", no_of_pax=5)

    def test_moving_the_time_or_the_table_still_checks_table_occupancy(self):
        with self.assertRaisesRegex(Invalid, "currently occupied"):
            self.api.update_table_reservation("TR-1", reserved_at=f"{DAY} 12:30:00")
        self.site.rows["URY Table"]["T2"].update(occupied=1, latest_invoice_time=at("11:15"))
        with self.assertRaisesRegex(Invalid, "currently occupied"):
            self.api.update_table_reservation("TR-1", table="T2")
        # Moving to a free table T3 still succeeds.
        self.api.update_table_reservation("TR-1", table="T3")
        self.assertEqual(self.site.rows["URY Table Reservation"]["TR-1"].reserved_table, "T3")

    def test_availability_keeps_the_edited_booking_s_table_when_time_is_unchanged(self):
        self.site.rows["URY Table"]["T2"].update(occupied=1, latest_invoice_time=at("11:15"))
        offered_same = [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} 12:35:00", exclude_reservation="TR-1")]
        self.assertIn("T1", offered_same)
        self.assertNotIn("T2", offered_same)
        self.assertIn("T3", offered_same)
        # Moving the time re-applies the occupied check to T1.
        offered_moved = [t.name for t in self.api.get_available_tables_for_reservation(
            "Branch A", f"{DAY} 12:30:00", exclude_reservation="TR-1")]
        self.assertNotIn("T1", offered_moved)


class TestSeatedCheckLifecycleTransferSplitMergeAndDiscard(_Timing):
    """P1-2 & P2-1/2/3: a Seated reservation follows its check across transfer, split, merge and discard."""

    def setUp(self):
        super().setUp()
        self.site.roles = ["URY Cashier"]
        self.frappe.session.user = "cashier@example.test"
        self.till(billing_roles=["URY Cashier"])
        self.clock("19:10")

    def check(self, name, table="T1", **extra):
        values = dict(doctype="POS Invoice", name=name, restaurant_table=table, custom_merged_tables="",
            branch="Branch A", order_type="Dine In", is_return=0, docstatus=0, invoice_printed=0,
            customer="CUST-1", creation=at("19:05"))
        values.update(extra)
        return self.site.put("POS Invoice", name, **values)

    def close(self, name):
        row = self.site.rows["POS Invoice"][name]
        row.docstatus = 1
        self.api.complete_seated_reservation_on_submit(Row(row))

    def stored(self, name):
        row = self.site.rows["URY Table Reservation"][name]
        return row.status, row.reserved_table, row.pos_invoice

    def test_sweep_never_completes_a_seated_party_whose_bound_check_is_still_open(self):
        # Even if T1 was cleared and the check is now on T2 (or past hold end), an open bound check keeps Seated.
        self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.check("INV-1", table="T2")
        self.site.rows["URY Table"]["T1"].occupied = 0
        self.site.rows["URY Table"]["T2"].occupied = 1
        self.clock("22:00")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-1"))

    def test_bill_close_completes_seated_party_by_bound_pos_invoice_even_when_table_differs(self):
        # Coordinator (a): settling a bound check completes its Seated reservation by pos_invoice
        # even if reserved_table still points to the old table (or the check carries no table).
        for close_table in ("T2", ""):
            with self.subTest(close_table=close_table):
                self.site.rows["URY Table Reservation"].clear()
                self.site.rows["POS Invoice"].clear()
                self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
                self.booking("TR-U", "19:00", table="T1", status="Seated")  # unbound: must not be matched
                self.check("INV-1", table=close_table)
                self.close("INV-1")
                self.assertEqual(self.stored("TR-S"), ("Completed", "T1", "INV-1"))
                self.assertEqual(self.stored("TR-U"), ("Seated", "T1", None))

    def test_sweep_never_completes_a_seated_party_while_a_split_sibling_is_still_open(self):
        self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.check("INV-1", table="T1", docstatus=1, custom_split_group="grp1")
        self.check("INV-2", table="T2", docstatus=0, custom_split_group="grp1", custom_split_from="INV-1")
        self.site.rows["URY Table"]["T1"].occupied = 0
        self.clock("22:00")
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-1"))

    def test_transfer_moves_the_bound_seated_reservation_to_the_new_table(self):
        self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        inv = self.check("INV-1", table="T1")
        self.api.transfer_seated_reservation("T1", "T2", inv)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T2", "INV-1"))
        # Sweep afterwards keeps TR-S Seated while INV-1 is open on T2.
        inv.restaurant_table = "T2"
        self.site.rows["URY Table"]["T1"].occupied = 0
        self.site.rows["URY Table"]["T2"].occupied = 1
        self.scheduler.process_reservation_no_shows()
        self.assertEqual(self.stored("TR-S"), ("Seated", "T2", "INV-1"))
        # Closing INV-1 on T2 completes TR-S.
        self.close("INV-1")
        self.assertEqual(self.stored("TR-S"), ("Completed", "T2", "INV-1"))

    def test_transfer_refuses_target_table_that_already_has_another_seated_party(self):
        self.booking("TR-1", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.booking("TR-2", "19:00", table="T2", status="Seated", pos_invoice="INV-2")
        inv1 = self.check("INV-1", table="T1")
        with self.assertRaisesRegex(Invalid, "already seated"):
            self.api.transfer_seated_reservation("T1", "T2", inv1)
        self.assertEqual(self.stored("TR-1"), ("Seated", "T1", "INV-1"))

    def test_transfer_of_an_unrelated_check_leaves_the_bound_seated_party_behind(self):
        self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.check("INV-1", table="T1", invoice_printed=1)
        inv2 = self.check("INV-2", table="T1", creation=at("19:30"))
        self.api.transfer_seated_reservation("T1", "T2", inv2)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-1"))

    def test_transfer_handles_split_bills_and_merged_partner_reservations(self):
        # Split child moving after bound primary settled carries the Seated reservation with it.
        self.booking("TR-SP", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.check("INV-1", table="T1", docstatus=1, custom_split_group="grp1")
        inv2 = self.check("INV-2", table="T1", docstatus=0, custom_split_group="grp1", custom_split_from="INV-1")
        self.api.transfer_seated_reservation("T1", "T2", inv2)
        self.assertEqual(self.stored("TR-SP"), ("Seated", "T2", "INV-1"))

        # Split child moving while bound primary stays open on T1 leaves the reservation on T1.
        self.site.rows["URY Table Reservation"].clear()
        self.site.rows["POS Invoice"].clear()
        self.booking("TR-SP2", "19:00", table="T1", status="Seated", pos_invoice="INV-A")
        self.check("INV-A", table="T1", docstatus=0, custom_split_group="grp2")
        inv_b = self.check("INV-B", table="T1", docstatus=0, custom_split_group="grp2", custom_split_from="INV-A")
        self.api.transfer_seated_reservation("T1", "T2", inv_b)
        self.assertEqual(self.stored("TR-SP2"), ("Seated", "T1", "INV-A"))

        # Reservation originally on merged partner T1, bound to check opened on T2, unmerged then transferred to T3.
        self.site.rows["URY Table Reservation"].clear()
        self.site.rows["POS Invoice"].clear()
        self.booking("TR-M", "19:00", table="T1", status="Seated", pos_invoice="INV-M")
        inv_m = self.check("INV-M", table="T2")
        self.api.transfer_seated_reservation("T2", "T3", inv_m)
        self.assertEqual(self.stored("TR-M"), ("Seated", "T3", "INV-M"))

    def test_a_discarded_or_cancelled_check_releases_binding_and_allows_rebinding(self):
        # Hook wiring on POS Invoice for discard, cancel and trash.
        for event in ("on_discard", "on_cancel", "on_trash"):
            with self.subTest(event=event):
                self.assertIn(
                    "ury.ury.api.table_reservation.release_reservation_check_binding",
                    self.hooked("POS Invoice", event),
                )

        # Hook clears pos_invoice when bound check is discarded.
        self.site.rows["URY Table"]["T1"].occupied = 1
        self.booking("TR-S", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        inv1 = self.check("INV-1", table="T1", docstatus=2)
        self.api.release_reservation_check_binding(inv1)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", None))

        # Even if pos_invoice still points to a docstatus=2 check in DB, rebind/prefill/submit treat it as unbound.
        self.site.rows["URY Table Reservation"]["TR-S"].pos_invoice = "INV-1"
        found = self.api.get_completed_reservation_for_table("T1", "Branch A")
        self.assertEqual((found or {}).get("name"), "TR-S")
        inv2 = self.check("INV-2", table="T1", docstatus=0)
        self.api.bind_seated_reservation_to_check(inv2)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-2"))
        self.close("INV-2")
        self.assertEqual(self.stored("TR-S"), ("Completed", "T1", "INV-2"))

        # Discarding a check for a Completed reservation keeps historical pos_invoice intact.
        self.api.release_reservation_check_binding(Row(doctype="POS Invoice", name="INV-2", branch="Branch A", docstatus=2))
        self.assertEqual(self.stored("TR-S"), ("Completed", "T1", "INV-2"))

    def test_settling_a_bound_split_bill_with_the_split_child_last_completes_the_visit(self):
        # Probe 5B: primary INV-1 bound on insert, split into INV-2, INV-1 settles first, INV-2 settles last.
        self.booking("TR-1", "19:00", table="T1", status="Seated")
        inv1 = self.check("INV-1", table="T1", creation=at("19:05"))
        self.api.bind_seated_reservation_to_check(inv1)
        self.assertEqual(self.stored("TR-1"), ("Seated", "T1", "INV-1"))
        inv1.custom_split_group = "grp1"
        self.check("INV-2", table="T1", creation=at("19:50"), custom_split_group="grp1", custom_split_from="INV-1")
        # Also open an unrelated walk-in on T1 at 19:40: it must neither take nor block the split party.
        self.check("INV-W", table="T1", creation=at("19:40"), customer="Guest")

        self.close("INV-1")
        self.assertEqual(self.stored("TR-1"), ("Seated", "T1", "INV-1"))
        self.close("INV-2")
        self.assertEqual(self.stored("TR-1")[0], "Completed")

    def test_settling_a_split_child_on_another_table_last_completes_the_visit(self):
        self.booking("TR-1", "19:00", table="T1", status="Seated", pos_invoice="INV-1")
        self.check("INV-1", table="T1", creation=at("19:05"), custom_split_group="grp1")
        self.check("INV-2", table="T2", creation=at("19:50"), custom_split_group="grp1", custom_split_from="INV-1")

        self.close("INV-1")
        self.assertEqual(self.stored("TR-1")[0], "Seated")
        self.close("INV-2")
        self.assertEqual(self.stored("TR-1")[0], "Completed")

    def test_a_check_opened_on_a_merged_partner_table_binds_the_seated_party(self):
        # Probe 5D: Seated on T1, check opened on merged partner T2 binds TR-S.
        self.booking("TR-S", "19:00", table="T1", status="Seated")
        inv_csv = self.check("INV-M1", table="T2", custom_merged_tables="T1")
        self.api.bind_seated_reservation_to_check(inv_csv)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-M1"))

        # Also works via live URY Table.merged_with even before custom_merged_tables is populated on the invoice.
        self.site.rows["URY Table Reservation"]["TR-S"].pos_invoice = None
        self.site.rows["URY Table"]["T1"].merged_with = "T2"
        self.site.rows["URY Table"]["T2"].merged_with = "T1"
        inv_live = self.check("INV-M2", table="T2", custom_merged_tables="")
        self.api.bind_seated_reservation_to_check(inv_live)
        self.assertEqual(self.stored("TR-S"), ("Seated", "T1", "INV-M2"))


if __name__ == "__main__":
    unittest.main()
