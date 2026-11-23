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
RESERVATION = "URY Table Reservation"
RESERVATION_META = json.loads(
    (ROOT / "ury/doctype/ury_table_reservation/ury_table_reservation.json").read_text())

# The permission model below mirrors pinned Frappe v16 (pmwanje2/frappe
# maximus/v16.60.2 @ 88a9cee0) so a contract cannot pass here and fail on a site:
#   rights        frappe/permissions.py:13-28 std_rights, :784-788 get_rights (+ Permission Types)
#   role perms    frappe/permissions.py:281-337 get_role_permissions (permlevel 0, if_owner)
#   doc perms     frappe/permissions.py:229-278 get_doc_permissions (owner, User Permission)
#   row scope     frappe/permissions.py:350-480 has_user_permission (doctype itself, then link fields)
#   Custom DocPerm frappe/model/meta.py:637-650 set_custom_permissions REPLACES DocPerm
#   list scope    frappe/database/query.py:1600-1645, 1704-1752, 1826-1842 (owner OR User Permission)
#   entry point   frappe/__init__.py:613-657 has_permission (no print_logs argument; logs only on throw)
# Shares are not modelled: they only ever add access, so leaving them out never lets a contract pass.
STD_RIGHTS = ("select", "read", "write", "create", "delete", "submit", "cancel", "amend", "print",
    "email", "report", "import", "export", "share")
# Permission Types an app ships live in <module>/permission_type/<name>/<name>.json
# (frappe/model/sync.py:18-23 imports them before DocTypes, so DocPerm has their column).
PERMISSION_TYPES = [json.loads(path.read_text()) for path in sorted(ROOT.glob("*/permission_type/*/*.json"))]
RIGHTS = STD_RIGHTS + tuple(pt["perm_type"] for pt in PERMISSION_TYPES if pt.get("doc_type") == RESERVATION)
# The DocPerm rows the DocType ships. A tenant's Custom DocPerm replaces them wholesale.
SHIPPED_DOCPERM = [dict(perm, permlevel=perm.get("permlevel", 0), if_owner=perm.get("if_owner", 0))
    for perm in RESERVATION_META["permissions"]]
LINK_FIELDS = [(f["fieldname"], f["options"]) for f in RESERVATION_META["fields"]
    if f["fieldtype"] == "Link" and not f.get("ignore_user_permissions")]


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
            "POS Profile", "POS Profile User", "User",
        )}
        self.session = SimpleNamespace(user="staff@example.test")
        self.user_roles = {}  # user -> roles (Has Role) for users a test names
        self.session_roles = []  # roles of whoever holds the session, unless named above
        self.user_permission_rows = {}  # user -> {doctype: allowed names} (User Permission)
        self.roles = ["System Manager"]
        self.custom_docperm = []  # Custom DocPerm rows on URY Table Reservation; any row replaces DocPerm
        self.strict_user_permissions = False  # System Settings.apply_strict_user_permissions
        self.closed_till_average = None  # minutes; set when a till has closed (see _Site.till_closed)
        self.versions = []  # what Frappe's Version log records for each tracked save
        self.errors = []
        self.sql_log = []
        self.events = []  # ordered row locks and SQL reads, for concurrency contracts
        self.fail_writes = set()
        # MariaDB 11.8 runs innodb_snapshot_isolation=ON: a locking read that meets a
        # row committed after this request's snapshot raises 1020 instead of reading it.
        self.committed_after_snapshot = False
        # The shipped Custom Field fixture: frappe.get_meta reads each field's default from it.
        self.custom_fields = json.loads((ROOT / "fixtures/custom_field.json").read_text())

    def meta(self, doctype):
        fields = {f["fieldname"]: Row(f) for f in self.custom_fields if f.get("dt") == doctype}
        return SimpleNamespace(get_field=fields.get)

    # -- users -------------------------------------------------------------------
    @property
    def roles(self):
        """The session user's roles."""
        return self.roles_of(self.session.user)

    @roles.setter
    def roles(self, value):
        if self.session.user in self.user_roles:
            self.user_roles[self.session.user] = list(value)
        else:
            self.session_roles = list(value)

    @property
    def user_permissions(self):
        """The session user's User Permission rows: doctype -> allowed names."""
        return self.user_permission_rows.setdefault(self.session.user, {})

    def login(self, user, roles=None):
        self.session.user = user
        if roles is not None:
            self.user_roles[user] = list(roles)

    def roles_of(self, user):
        if user in self.user_roles:
            return list(self.user_roles[user])
        return list(self.session_roles) if user == self.session.user else []

    # -- Role Permission Manager ---------------------------------------------------
    def docperms(self):
        """Meta.set_custom_permissions: any Custom DocPerm row replaces the shipped DocPerm."""
        return self.custom_docperm or SHIPPED_DOCPERM

    def copy_shipped_docperms(self):
        """permissions.setup_custom_perms: the first edit copies DocPerm into Custom DocPerm."""
        if not self.custom_docperm:
            self.custom_docperm = [dict(perm) for perm in SHIPPED_DOCPERM]

    def grant(self, role, *ptypes, if_owner=0):
        """A tenant rule added in Role Permission Manager (Custom DocPerm)."""
        self.copy_shipped_docperms()
        self.custom_docperm.append(dict({p: 1 for p in ptypes}, role=role, permlevel=0, if_owner=if_owner))

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
                if op == "=":
                    ok = value == operand
                elif op == "in":
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
            limit_page_length=None, pluck=None, or_filters=None, **kwargs):
        rows = self.select(doctype, filters)
        if or_filters:
            pairs = or_filters.items() if isinstance(or_filters, dict) else (
                (f[0], f[1:] if len(f) > 2 else f[1]) for f in or_filters)
            pairs = list(pairs)
            rows = [row for row in rows if any(self.matches(row, {key: cond}) for key, cond in pairs)]
        return self.shape(rows, fields, order_by, limit or limit_page_length, pluck)

    def shape(self, rows, fields=None, order_by=None, limit=None, pluck=None):
        rows = list(rows)
        if order_by:
            for clause in reversed([c.strip() for c in order_by.split(",")]):
                field, _, direction = clause.partition(" ")
                rows.sort(key=lambda r: (r.get(field) is None, r.get(field)),
                    reverse=direction.strip().lower() == "desc")
        if limit:
            rows = rows[:limit]
        if pluck:
            return [row.get(pluck) for row in rows]
        return [self.project(row, fields or ["name"]) for row in rows]

    # -- native permissions (frappe/permissions.py @ 88a9cee0) ------------------------
    def has_permission(self, doctype=None, ptype="read", doc=None, user=None, throw=False, *,
            parent_doctype=None, debug=False, ignore_share_permissions=False):
        """frappe.has_permission. Its signature is the pinned one: there is no print_logs."""
        if not doctype and doc is not None:
            doctype = doc.get("doctype")
        user = user or self.session.user
        allowed = self.permitted(doctype, ptype, doc, user)
        if throw and not allowed:
            raise Refused(f"No permission for {doctype}")
        return allowed

    def permitted(self, doctype, ptype, doc, user):
        if user == "Administrator":
            return True
        if doctype != RESERVATION:
            return True  # other doctypes' DocPerm is outside these contracts
        if isinstance(doc, str):
            doc = self.rows[doctype][doc]  # frappe.get_lazy_doc
        if doc is not None:
            return bool(self.doc_permissions(doc, user).get(ptype))
        return bool(self.role_permissions(user).get(ptype))

    def role_permissions(self, user, is_owner=False):
        """get_role_permissions: permlevel 0 rules for the user's roles, with if_owner."""
        roles = self.roles_of(user)
        rules = [p for p in self.docperms() if p["role"] in roles and int(p.get("permlevel") or 0) == 0]
        has_if_owner = any(p.get("if_owner") for p in rules)
        perms = {"if_owner": {}, "has_if_owner_enabled": has_if_owner}
        for ptype in RIGHTS:
            granted = any(p.get(ptype) for p in rules)
            perms[ptype] = int(granted)
            unconditional = any(p.get(ptype) and not p.get("if_owner") for p in rules)
            if granted and has_if_owner and not unconditional and ptype != "create":
                perms["if_owner"][ptype] = int(is_owner)
                perms[ptype] = 1 if ptype in ("select", "read") else 0
        return perms

    def doc_permissions(self, doc, user):
        """get_doc_permissions: role rules, owner rules on doc.owner, then User Permission."""
        is_owner = (doc.get("owner") or "").lower() == user.lower()
        perms = self.role_permissions(user, is_owner=is_owner)
        perms = dict(perms, if_owner=dict(perms["if_owner"]), submit=0, **{"import": 0})
        if perms["has_if_owner_enabled"]:
            perms.update(perms["if_owner"])
        if not self.has_user_permission(doc, user):
            if not is_owner:
                return {}
            perms = dict(perms["if_owner"], create=0)
        return perms

    def has_user_permission(self, doc, user):
        """has_user_permission: the doctype's own rows, then every link field."""
        rows = self.user_permission_rows.get(user) or {}
        if not rows:
            return True
        own = rows.get(RESERVATION)
        if own and (not doc.get("name") or str(doc.get("name")) not in own):
            return False
        for fieldname, options in LINK_FIELDS:
            value = doc.get(fieldname)
            if not value and not self.strict_user_permissions:
                continue
            allowed = rows.get(options)
            if allowed and str(value) not in allowed:
                return False
        return True

    def get_list(self, doctype, filters=None, fields=None, user=None, **kwargs):
        """frappe.get_list: role read, then (if_owner OR User Permission) row conditions."""
        if "ignore_permissions" in kwargs:
            raise AssertionError("get_list must not bypass permissions")
        user = user or self.session.user
        if doctype != RESERVATION or user == "Administrator":
            return self.get_all(doctype, filters=filters, fields=fields, **kwargs)
        perms = self.role_permissions(user)
        if not (perms.get("read") or perms.get("select")):
            raise Refused(f"No read permission on {doctype}")
        names = {row.name for row in self.select(doctype) if self.listable(row, perms, user)}
        visible = dict(filters or {})
        visible["name"] = ["in", names]
        if "name" in (filters or {}):
            raise AssertionError("unsupported: a name filter on a permission-scoped list")
        return self.get_all(doctype, filters=visible, fields=fields, **kwargs)

    def listable(self, row, perms, user):
        if perms["has_if_owner_enabled"] and perms["if_owner"] and not any(
                perms.get(p) and p not in perms["if_owner"] for p in ("select", "read")):
            return row.get("owner") == user  # requires_owner_constraint
        rows = self.user_permission_rows.get(user) or {}
        for fieldname, options in [("name", RESERVATION), *LINK_FIELDS]:
            allowed = rows.get(options)
            if not allowed:
                continue
            value = row.get(fieldname)
            if self.strict_user_permissions:
                if value not in allowed:
                    return False
            elif value and value not in allowed:
                return False
        return True

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
            if self.closed_till_average is None:
                return []  # no till has closed yet, so table-time averages are unset
            return [Row(closing_entry="CLS-1", pos_opening_entry="OPN-1", branch=params.get("branch"),
                period_start_date=at("08:00"), period_end_date=at("11:00"))]
        if "AS `avg_table_time` FROM `tabPOS Invoice` inv" in q:
            return [Row(avg_table_time=self.closed_till_average)]
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

    def has_permission(self, permtype="read", *, debug=False, user=None):
        """Document.has_permission: a flagged system save skips the check."""
        if self.flags.ignore_permissions:
            return True
        return self.site.has_permission(self.doctype, permtype, self, user=user)

    def check_permission(self, ptype="read"):
        if not self.has_permission(ptype):
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
        before = self._before
        if before is not None and any(before.get(k) != v for k, v in self.values().items()
                if k not in ("modified", "modified_by")):
            # URY Table Reservation tracks changes: a Version for every save of an
            # existing booking that changed something, naming the session user.
            self.site.versions.append(Row(docname=self.name, owner=self.site.session.user,
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
    fr.session = site.session
    fr.flags = SimpleNamespace()
    fr.throw.side_effect = lambda message, exc=Invalid, *a, **k: (_ for _ in ()).throw(exc(message))
    fr.log_error.side_effect = lambda *a, **k: site.errors.append((a, k))
    fr.get_traceback.return_value = "traceback"
    fr.get_roles.side_effect = lambda user=None: site.roles_of(user or site.session.user)
    fr.get_all.side_effect = site.get_all
    fr.get_list.side_effect = site.get_list
    fr.has_permission.side_effect = site.has_permission
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
            guest="Grace", pax=4, modified=None, owner="staff@example.test", **extra):
        return self.site.put("URY Table Reservation", name, doctype="URY Table Reservation",
            branch=branch, reserved_table=table, customer="CUST-1", customer_name=guest,
            customer_phone="+256772123456", no_of_pax=pax, reserved_at=at(clock), status=status,
            creation=at(created) if created else self.site.now, modified=modified or self.site.now,
            owner=owner, reserved_by=extra.pop("reserved_by", owner), **extra)

    def status(self, name):
        return self.site.rows["URY Table Reservation"][name].status

    def clock(self, value):
        self.site.now = at(value)

    def till_closed(self, average, branch="Branch A"):
        """A till closed after tables averaged `average` minutes.

        The sales history then yields that average, and closing the till stored it
        on the Branch (update_branch_avg_table_times writes both fields).
        """
        self.site.closed_till_average = average
        self.site.rows["Branch"][branch].update(
            custom_avg_table_time_last_day=average, custom_avg_table_time_last_week=average)

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


class _Privileges(_Site):
    """Bookings and save paths for the reservation privilege contracts."""

    DESK = "desk@hotel.example.test"

    def setUp(self):
        super().setUp()
        for name in ("T4", "T5", "T6"):
            self.table(name, 4)
        self.clock("18:00")
        self.booking("TR-1", "19:00")
        self.booking("TR-2", "21:00", table="T2")

    def cancel(self, name, reason="Guest called"):
        self.api.update_reservation_status(name, "Cancelled", cancel_reason=reason)

    def refused(self, call, name):
        before = self.status(name)
        with self.assertRaises(Refused):
            call()
        self.assertEqual(self.status(name), before)

    def desk_save(self, name, status, reason=None):
        """A Desk or REST save that changes the status field directly."""
        doc = self.get_doc(RESERVATION, name)
        doc.status = status
        doc.cancel_reason = reason
        doc.save()


class TestCancellingIsItsOwnPrivilege(_Privileges):
    """D4: seating and cancelling are separate privileges (Paul, 2026-11-23).

    Simphony Guest Management grants them as separate employee-class
    operations: greeting and seating the arriving party (32006 Greet Wait
    List Entry) and 32012 Cancel Reservation Entry, which hosts, concierge and
    managers hold and waiters do not. URY ships that privilege as the native
    Frappe v16 Permission Type cancel_reservation on URY Table Reservation:
    captains keep write (they seat and edit) without it; the till's cashier
    and manager roles hold it; a tenant grants it to Front Desk, Hotel Admin
    and F&B Manager as Custom DocPerm. Completing a Seated party by hand
    needs the same privilege; closing its bill does not (R3, see
    TestClosingTheCheckCompletesTheVisitWithoutAReservationPrivilege).
    """

    PTYPE_FILE = ROOT / ("ury/permission_type/ury_table_reservation_cancel_reservation/"
        "ury_table_reservation_cancel_reservation.json")

    def test_the_app_ships_cancel_reservation_as_a_native_permission_type(self):
        self.assertTrue(self.PTYPE_FILE.exists(), "URY ships no cancel_reservation Permission Type")
        ptype = json.loads(self.PTYPE_FILE.read_text())
        # PermissionType.autoname is scrub(doc_type) + "_" + scrub(perm_type); sync reads <name>/<name>.json.
        self.assertEqual((ptype["doctype"], ptype["doc_type"], ptype["perm_type"], ptype["name"]),
            ("Permission Type", RESERVATION, "cancel_reservation", self.PTYPE_FILE.stem))
        self.assertIn("cancel_reservation", RIGHTS)

    def test_the_shipped_docperm_gives_cancel_to_cashier_manager_and_system_manager_not_the_captain(self):
        cancel = {perm["role"]: bool(perm.get("cancel_reservation")) for perm in SHIPPED_DOCPERM}
        self.assertEqual(cancel, {"System Manager": True, "URY Manager": True, "URY Cashier": True,
            "URY Captain": False})
        captain = next(perm for perm in SHIPPED_DOCPERM if perm["role"] == "URY Captain")
        self.assertEqual((captain.get("read"), captain.get("write")), (1, 1))  # still seats and edits

    def test_a_captain_seats_and_edits_but_may_not_cancel_or_complete_by_hand(self):
        self.site.roles = ["URY Captain"]
        self.api.update_reservation_status("TR-1", "Seated")
        self.assertEqual(self.status("TR-1"), "Seated")
        self.api.update_table_reservation("TR-2", no_of_pax=2)
        self.assertEqual(self.site.rows[RESERVATION]["TR-2"].no_of_pax, 2)
        self.refused(lambda: self.cancel("TR-2"), "TR-2")
        self.refused(lambda: self.api.update_reservation_status("TR-1", "Completed"), "TR-1")
        # A Desk or REST save of the status field meets the same rule.
        self.refused(lambda: self.desk_save("TR-2", "Cancelled", "Guest called"), "TR-2")
        self.refused(lambda: self.desk_save("TR-1", "Completed"), "TR-1")

    def test_the_till_cashier_and_manager_cancel_and_complete_with_the_shipped_docperm(self):
        for role, name in (("URY Cashier", "TR-1"), ("URY Manager", "TR-2")):
            with self.subTest(role=role):
                self.site.roles = [role]
                self.cancel(name)
                self.assertEqual(self.status(name), "Cancelled")
        self.booking("TR-3", "17:30", table="T3", status="Seated")
        self.site.roles = ["URY Cashier"]
        self.api.update_reservation_status("TR-3", "Completed")
        self.assertEqual(self.status("TR-3"), "Completed")

    def test_a_hotel_desk_cancels_only_with_the_tenant_cancel_grant(self):
        # Tenant configuration: Custom DocPerm copies the shipped rows, then adds the desks.
        for role in ("Front Desk", "Hotel Admin", "F&B Manager"):
            self.site.grant(role, "read", "write", "create", "cancel_reservation")
        self.site.grant("Banquet Clerk", "read", "write", "create")
        for role, name, table in (("Front Desk", "TR-4", "T4"), ("Hotel Admin", "TR-5", "T5"),
                ("F&B Manager", "TR-6", "T6")):
            with self.subTest(role=role):
                self.site.roles = [role]
                self.booking(name, "19:30", table=table)
                self.api.update_table_reservation(name, no_of_pax=2)
                self.cancel(name, reason="Guest called the hotel")
                stored = self.site.rows[RESERVATION][name]
                self.assertEqual((stored.no_of_pax, stored.status, stored.cancel_reason),
                    (2, "Cancelled", "Guest called the hotel"))
        # write alone edits and seats; it never cancels.
        self.site.roles = ["Banquet Clerk"]
        self.api.update_table_reservation("TR-2", no_of_pax=3)
        self.refused(lambda: self.cancel("TR-2"), "TR-2")
        self.site.roles = ["URY Captain"]  # the copied shipped rows still apply
        self.refused(lambda: self.cancel("TR-1"), "TR-1")

    def test_an_if_owner_cancel_grant_covers_only_the_bookings_that_desk_made(self):
        self.site.grant("Front Desk", "read", "write", "create")
        self.site.grant("Front Desk", "cancel_reservation", if_owner=1)
        self.site.login(self.DESK, ["Front Desk"])
        self.booking("TR-MINE", "19:30", table="T4", owner=self.DESK)
        # if_owner reads the document owner, never the reserved_by field.
        self.booking("TR-THEIRS", "20:00", table="T5", owner="other@hotel.example.test", reserved_by=self.DESK)
        self.cancel("TR-MINE")
        self.assertEqual(self.status("TR-MINE"), "Cancelled")
        self.api.update_table_reservation("TR-THEIRS", no_of_pax=2)
        self.refused(lambda: self.cancel("TR-THEIRS"), "TR-THEIRS")

    def test_the_gate_decides_on_the_booking_never_on_the_role_alone(self):
        self.site.roles = ["URY Cashier"]
        gate = self.api.is_user_authorized_for_reservation_status_change
        self.assertFalse(gate(doc=None))
        self.assertTrue(gate(doc=self.get_doc(RESERVATION, "TR-1")))

    def test_a_desk_scoped_to_another_branch_is_refused_even_as_a_till_billing_role(self):
        # The old till fallback read POS Profile rows and ignored the booking's User Permission.
        self.till(billing_roles=["Front Desk"])
        self.site.grant("Front Desk", "read", "write", "create", "cancel_reservation")
        self.site.login(self.DESK, ["Front Desk"])
        self.site.user_permissions["Branch"] = {"Branch Off"}
        self.assertFalse(self.api.is_user_authorized_for_reservation_status_change(
            doc=self.get_doc(RESERVATION, "TR-1")))
        self.refused(lambda: self.cancel("TR-1"), "TR-1")

    def test_a_till_billing_or_manager_role_without_the_grant_does_not_cancel(self):
        # #139's second path (the till's billing and manager roles) sat behind the write check
        # every caller already made; the native grant is now the only path.
        self.till(billing_roles=["Till Operator"], manager_roles=["Floor Lead"])
        for role, name in (("Till Operator", "TR-1"), ("Floor Lead", "TR-2")):
            with self.subTest(role=role):
                self.site.grant(role, "read", "write")
                self.site.roles = [role]
                self.refused(lambda: self.cancel(name), name)

    def test_a_cancellation_still_needs_a_reason(self):
        self.site.roles = ["URY Cashier"]
        for reason in (None, "   "):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(Invalid, "reason"):
                    self.api.update_reservation_status("TR-1", "Cancelled", cancel_reason=reason)
                self.assertEqual(self.status("TR-1"), "Confirmed")

    def test_the_captain_seats_and_the_cashier_s_bill_close_completes_the_party(self):
        self.till(billing_roles=["URY Cashier"])
        self.site.login("captain@example.test", ["URY Captain"])
        self.api.update_reservation_status("TR-1", "Seated")
        self.site.login("cashier@example.test", ["URY Cashier"])
        self.api.complete_seated_reservation_on_submit(Row(doctype="POS Invoice", name="INV-1",
            restaurant_table="T1", custom_merged_tables="", branch="Branch A", order_type="Dine In",
            docstatus=1, is_return=0, creation=at("18:05")))
        stored = self.site.rows[RESERVATION]["TR-1"]
        self.assertEqual((stored.status, stored.pos_invoice), ("Completed", "INV-1"))

    def test_the_reservation_list_says_who_may_cancel_each_booking(self):
        self.site.grant("Front Desk", "read", "write", "create")
        self.site.grant("Front Desk", "cancel_reservation", if_owner=1)
        self.booking("TR-MINE", "19:30", table="T4", owner=self.DESK)
        self.booking("TR-SEATED", "17:45", table="T5", status="Seated")

        def listed(user, roles):
            self.site.login(user, roles)
            return {row.name: row.get("can_cancel") for row in self.api.get_active_reservations("Branch A")}

        everyone_else = {"TR-1": False, "TR-2": False, "TR-SEATED": False}
        self.assertEqual(listed("captain@example.test", ["URY Captain"]), dict(everyone_else, **{"TR-MINE": False}))
        self.assertEqual(listed("cashier@example.test", ["URY Cashier"]),
            {"TR-1": True, "TR-2": True, "TR-MINE": True, "TR-SEATED": False})
        self.assertEqual(listed(self.DESK, ["Front Desk"]), dict(everyone_else, **{"TR-MINE": True}))


class TestMarkingNoShowIsItsOwnPrivilege(_Privileges):
    """R3: Simphony grants 32007 Mark as No Show apart from 32012 Cancel Reservation Entry.

    URY ships it as the native Permission Type mark_no_show on URY Table
    Reservation, with the same shipped DocPerm roles as cancel_reservation;
    a tenant reassigns either in Role Permissions Manager. Every manual path
    (API, REST, set_value, Desk save) needs it. The site clock's own No Show
    after the grace period stays a system action.
    """

    NO_SHOW_FILE = ROOT / ("ury/permission_type/ury_table_reservation_mark_no_show/"
        "ury_table_reservation_mark_no_show.json")

    def setUp(self):
        super().setUp()
        self.clock("15:00")  # TR-1 (19:00) is four hours out, TR-2 (21:00) six

    def no_show(self, name):
        self.api.update_reservation_status(name, "No Show")

    def test_the_app_ships_mark_no_show_as_a_native_permission_type(self):
        self.assertTrue(self.NO_SHOW_FILE.exists(), "URY ships no mark_no_show Permission Type")
        ptype = json.loads(self.NO_SHOW_FILE.read_text())
        self.assertEqual((ptype["doctype"], ptype["doc_type"], ptype["perm_type"], ptype["name"]),
            ("Permission Type", RESERVATION, "mark_no_show", self.NO_SHOW_FILE.stem))
        self.assertIn("mark_no_show", RIGHTS)

    def test_the_shipped_docperm_gives_mark_no_show_to_the_roles_that_hold_cancel(self):
        no_show = {perm["role"]: bool(perm.get("mark_no_show")) for perm in SHIPPED_DOCPERM}
        cancel = {perm["role"]: bool(perm.get("cancel_reservation")) for perm in SHIPPED_DOCPERM}
        self.assertEqual(no_show, cancel)
        self.assertEqual(no_show, {"System Manager": True, "URY Manager": True, "URY Cashier": True,
            "URY Captain": False})

    def test_a_captain_may_not_mark_a_future_booking_no_show_on_any_path(self):
        # Neon R2: as a captain, update_reservation_status(TR, "No Show") four hours ahead and
        # REST PUT {"status": "No Show"} five hours ahead both returned 200 and freed the table.
        self.site.roles = ["URY Captain"]
        self.refused(lambda: self.no_show("TR-1"), "TR-1")
        # REST v1/v2 PUT, frappe.client.set_value and the Desk form all save the document.
        self.refused(lambda: self.desk_save("TR-2", "No Show"), "TR-2")
        doc = self.get_doc(RESERVATION, "TR-2")
        doc.status = "No Show"
        self.refused(lambda: doc.save(ignore_permissions=True), "TR-2")  # a flagged save is no bypass
        self.assertEqual(self.site.versions, [])

    def test_the_till_cashier_and_manager_mark_no_show_with_the_shipped_docperm(self):
        for role, name in (("URY Cashier", "TR-1"), ("URY Manager", "TR-2")):
            with self.subTest(role=role):
                self.site.roles = [role]
                self.no_show(name)
                self.assertEqual(self.status(name), "No Show")

    def test_a_tenant_grants_mark_no_show_and_cancel_apart(self):
        self.site.grant("Front Desk", "read", "write", "create", "mark_no_show")
        self.site.grant("Banquet Clerk", "read", "write", "create", "cancel_reservation")
        self.site.roles = ["Front Desk"]
        self.no_show("TR-1")
        self.assertEqual(self.status("TR-1"), "No Show")
        self.refused(lambda: self.cancel("TR-2"), "TR-2")
        self.site.roles = ["Banquet Clerk"]
        self.refused(lambda: self.no_show("TR-2"), "TR-2")
        self.refused(lambda: self.desk_save("TR-2", "No Show"), "TR-2")
        self.cancel("TR-2")
        self.assertEqual(self.status("TR-2"), "Cancelled")

    def test_an_if_owner_grant_marks_only_the_bookings_that_desk_made(self):
        self.site.grant("Front Desk", "read", "write", "create")
        self.site.grant("Front Desk", "mark_no_show", if_owner=1)
        self.site.login(self.DESK, ["Front Desk"])
        self.booking("TR-MINE", "19:30", table="T4", owner=self.DESK)
        self.booking("TR-THEIRS", "20:00", table="T5", owner="other@hotel.example.test", reserved_by=self.DESK)
        self.no_show("TR-MINE")
        self.assertEqual(self.status("TR-MINE"), "No Show")
        self.refused(lambda: self.no_show("TR-THEIRS"), "TR-THEIRS")

    def test_the_site_clock_no_show_stays_a_system_action(self):
        # A captain's floor read sweeps a booking past its grace: the system marks it,
        # through a save (a Version, not the db_set fallback).
        self.site.roles = ["URY Captain"]
        self.clock("19:15")  # TR-1's 15 minute grace has ended
        self.api.get_active_reservations("Branch A")
        self.assertEqual((self.status("TR-1"), self.status("TR-2")), ("No Show", "Confirmed"))
        self.assertEqual([v.status for v in self.site.versions if v.docname == "TR-1"],
            [("Confirmed", "No Show")])

    def test_the_system_transitions_open_no_other_status_change(self):
        # Only the site clock's No Show and the bill close's Seated -> Completed are system moves.
        self.site.roles = ["URY Captain"]
        for transition, status in ((("Confirmed", "No Show"), "Cancelled"),
                (("Seated", "Completed"), "No Show"), (("Confirmed", "Cancelled"), "Cancelled")):
            with self.subTest(flag=transition, status=status):
                doc = self.get_doc(RESERVATION, "TR-2")
                doc.status = status
                doc.cancel_reason = "Guest called"
                doc.flags.system_transition = transition
                self.refused(lambda: doc.save(ignore_permissions=True), "TR-2")

    def test_the_reservation_list_says_who_may_mark_each_booking_no_show(self):
        self.site.grant("Front Desk", "read", "write", "create")
        self.site.grant("Front Desk", "mark_no_show", if_owner=1)
        self.booking("TR-MINE", "19:30", table="T4", owner=self.DESK)
        self.booking("TR-SEATED", "14:45", table="T5", status="Seated")
        self.site.rows["URY Table"]["T5"].occupied = 1  # the party is still at its table

        def listed(user, roles):
            self.site.login(user, roles)
            return {row.name: row.get("can_mark_no_show") for row in self.api.get_active_reservations("Branch A")}

        everyone_else = {"TR-1": False, "TR-2": False, "TR-SEATED": False}
        self.assertEqual(listed("captain@example.test", ["URY Captain"]), dict(everyone_else, **{"TR-MINE": False}))
        self.assertEqual(listed("cashier@example.test", ["URY Cashier"]),
            {"TR-1": True, "TR-2": True, "TR-MINE": True, "TR-SEATED": False})
        self.assertEqual(listed(self.DESK, ["Front Desk"]), dict(everyone_else, **{"TR-MINE": True}))


class TestThePermissionModelIsFrappes(_Site):
    """The in-memory site is no more permissive than Frappe v16 (88a9cee0); see the module header."""

    def test_a_custom_docperm_rule_replaces_the_shipped_docperm(self):
        self.site.roles = ["URY Captain"]
        # A tenant that adds only its own role, without copying the shipped rows, revokes them all.
        self.site.custom_docperm = [dict(role="Front Desk", permlevel=0, if_owner=0, read=1, write=1)]
        self.assertFalse(self.site.has_permission(RESERVATION, "write"))
        self.site.custom_docperm = []
        self.site.grant("Front Desk", "read", "write")  # copies the shipped rows first
        self.assertTrue(self.site.has_permission(RESERVATION, "write"))

    def test_the_list_applies_user_permission_and_if_owner_row_conditions(self):
        self.booking("TR-1", "19:00")
        self.booking("TR-9", "19:00", table="T9", branch="Branch Off")
        def names():
            return sorted(self.site.get_list(RESERVATION, filters={}, pluck="name"))

        self.site.roles = ["URY Cashier"]
        self.site.user_permissions["Branch"] = {"Branch A"}
        self.assertEqual(names(), ["TR-1"])
        self.site.custom_docperm = [dict(role="Guest Host", permlevel=0, if_owner=1, read=1)]
        self.site.login("host@example.test", ["Guest Host"])
        self.booking("TR-3", "19:30", table="T3", owner="host@example.test")
        self.assertEqual(names(), ["TR-3"])

    def test_if_owner_reads_the_document_owner_not_reserved_by(self):
        self.site.grant("Front Desk", "read", "write", if_owner=1)
        self.site.login("desk@example.test", ["Front Desk"])
        mine = self.booking("TR-1", "19:00", owner="desk@example.test", reserved_by="other@example.test")
        theirs = self.booking("TR-2", "19:00", table="T2", owner="other@example.test",
            reserved_by="desk@example.test")
        self.assertTrue(self.site.has_permission(RESERVATION, "write", doc=mine))
        self.assertFalse(self.site.has_permission(RESERVATION, "write", doc=theirs))
        self.assertFalse(self.site.has_permission(RESERVATION, "write"))  # without a doc there is no owner

    def test_the_user_argument_is_honoured(self):
        self.site.user_roles["cashier@example.test"] = ["URY Cashier"]
        self.site.roles = ["Guest Viewer"]
        self.assertFalse(self.site.has_permission(RESERVATION, "read"))
        self.assertTrue(self.site.has_permission(RESERVATION, "read", user="cashier@example.test"))

    def test_has_permission_takes_frappes_arguments_and_no_print_logs(self):
        # frappe.has_permission (frappe/__init__.py:613-623) has no print_logs: it logs only
        # when throw=True, so a production call that passed print_logs would raise on a site.
        with self.assertRaises(TypeError):
            self.frappe.has_permission(RESERVATION, "read", print_logs=False)
        self.assertTrue(self.frappe.has_permission(RESERVATION, "read", throw=False))


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


class _BillClose(_Site):
    """A cashier's till and the checks it closes."""

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


class TestClosingTheBillCompletesTheReservation(_BillClose):
    """B8: submitting the table's POS Invoice completes the Seated reservation and links the bill."""

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

    def test_the_closer_s_own_write_authority_decides_and_the_version_names_them(self):
        # R3: closing the check completes the visit with no reservation privilege (Simphony
        # TMS), but the save is still the closer's own: a closer with no write on the
        # booking leaves the party Seated, and the payment still stands.
        self.site.grant("Till Operator", "read")
        self.site.login("operator@example.test", ["Till Operator"])
        self.booking("TR-1", "19:00", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill())  # never raises into the payment
        self.assertEqual(self.stored("TR-1"), ("Seated", None))
        self.assertTrue(self.site.errors)
        self.assertEqual(self.site.versions, [])
        # A captain, with write but neither reservation privilege, completes it; the Version names them.
        self.site.login("captain@example.test", ["URY Captain"])
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))
        self.assertEqual([(v.owner, v.status) for v in self.site.versions if v.docname == "TR-1"],
            [("captain@example.test", ("Seated", "Completed"))])

    def test_a_closer_s_user_permission_still_scopes_the_completion(self):
        self.site.login("captain@example.test", ["URY Captain"])
        self.site.user_permissions["Branch"] = {"Branch Off"}
        self.booking("TR-1", "19:00", status="Seated")
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Seated", None))

    def test_the_completion_runs_on_pos_invoice_submit(self):
        tree = ast.parse((ROOT / "hooks.py").read_text())
        doc_events = next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "doc_events" for t in node.targets))
        self.assertIn("ury.ury.api.table_reservation.complete_seated_reservation_on_submit",
            doc_events["POS Invoice"]["on_submit"])


class TestClosingTheCheckCompletesTheVisitWithoutAReservationPrivilege(_BillClose):
    """R3: in Simphony TMS, closing the guest check completes the seated visit.

    No reservation privilege is involved (neither 32012 Cancel Reservation
    Entry nor 32007 Mark as No Show): the bill close is a system transition
    that moves only Seated -> Completed, only for the booking linked to that
    check. Everything else stays under the closer's own permissions.
    """

    CAPTAIN, OTHER_CAPTAIN = "captain@example.test", "captain2@example.test"

    def setUp(self):
        super().setUp()
        self.site.login(self.CAPTAIN, ["URY Captain"])
        self.site.user_roles[self.OTHER_CAPTAIN] = ["URY Captain"]

    def test_a_captain_seats_and_a_captain_without_either_privilege_closes_the_bill(self):
        # Neon R2: a captain closing KVU-INV-CAP-REAL logged PermissionError and left the party Seated.
        self.clock("19:00")
        self.booking("TR-1", "19:00")
        self.api.update_reservation_status("TR-1", "Seated")
        self.assertEqual(self.status("TR-1"), "Seated")
        self.site.login(self.OTHER_CAPTAIN)
        for ptype in ("cancel_reservation", "mark_no_show"):
            self.assertFalse(self.site.has_permission(RESERVATION, ptype, self.get_doc(RESERVATION, "TR-1")))
        self.clock("20:30")
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))
        self.assertEqual(self.site.errors, [])
        self.assertEqual([(v.owner, v.status) for v in self.site.versions if v.docname == "TR-1"],
            [(self.CAPTAIN, ("Confirmed", "Seated")), (self.OTHER_CAPTAIN, ("Seated", "Completed"))])

    def test_the_bill_close_moves_only_its_own_seated_party(self):
        self.booking("TR-1", "19:00", status="Seated")
        self.booking("TR-2", "21:30", table="T1")  # the table's next booking
        self.booking("TR-3", "19:00", table="T2", status="Seated")  # another table's party
        self.api.complete_seated_reservation_on_submit(self.bill())
        self.assertEqual(self.stored("TR-1"), ("Completed", "INV-1"))
        self.assertEqual((self.stored("TR-2"), self.stored("TR-3")), (("Confirmed", None), ("Seated", None)))
        # The closer gained nothing else: completing, cancelling or marking No Show by hand stays refused.
        for name, status in (("TR-3", "Completed"), ("TR-2", "Cancelled"), ("TR-2", "No Show")):
            with self.subTest(name=name, status=status):
                with self.assertRaises(Refused):
                    self.api.update_reservation_status(name, status, cancel_reason="Guest called")
        self.assertEqual((self.stored("TR-2"), self.stored("TR-3")), (("Confirmed", None), ("Seated", None)))


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

        for cleared in (0, "", -5):
            with self.subTest(turn=cleared), self.assertRaisesRegex(Invalid, "target turn time"):
                validate(custom_target_turn_time=cleared)
        validate(custom_target_turn_time=1)
        validate(custom_target_turn_time=0, custom_enable_reservation=0)
        validate(custom_target_turn_time=None)  # absent before migrate: read as the seed, not refused


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
            custom_unset_grace_policy="Refuse to Enable", custom_grace_period=0)
        doc.update(values)
        self.api.validate_branch_reservation_settings(doc)

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
        for unset in (None, "", 0):
            with self.subTest(grace=unset), self.assertRaisesRegex(Invalid, "grace period"):
                self.validate(custom_grace_period=unset)
        self.validate(custom_grace_period=10)
        self.validate(custom_enable_reservation=0)
        self.validate(custom_unset_grace_policy="Use Default Grace Period")
        self.validate(custom_unset_grace_policy=None)  # an unset policy takes the seeded one
        self.seed("custom_unset_grace_policy", "Refuse to Enable")
        with self.assertRaisesRegex(Invalid, "grace period"):
            self.validate(custom_unset_grace_policy=None)

    def test_a_refusing_branch_saved_without_a_grace_is_still_never_zero(self):
        # Reached only by a write that skipped Branch validate: the booking is still held.
        self.branch(custom_unset_grace_policy="Refuse to Enable", custom_grace_period=0)
        self.assertEqual(self.settings()["grace_period"], 15)

    def test_the_policy_runs_on_every_branch_save(self):
        self.assertIn("ury.ury.api.table_reservation.validate_branch_reservation_settings",
            self.hooked("Branch", "validate"))


if __name__ == "__main__":
    unittest.main()
