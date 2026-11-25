"""G40 Serving Periods / Dayparts and G41 Outlet Timezone Stamping.

Native-first daypart scheduling and outlet clock stamping:
- Reuses ``URY Branch Operating Schedule.timezone`` via ``zoneinfo.ZoneInfo`` (never raw server UTC).
- Stamps ``custom_serving_period``, zero-padded ``HH:MM:SS`` ``custom_transaction_local_time``,
  and ``Mon``..``Sun`` ``custom_transaction_weekday`` on POS Invoice (honoring ``ury_offline_captured_at``
  on offline replays) so native ERPNext ``Pricing Rule`` conditions (``sales-time-pricing@1``) evaluate
  accurately in the outlet's local timezone.
- Enforces non-overlapping equal-priority Serving Periods (including midnight wraps like 22:00..02:00).
- Resolves Menu & Price List in strict 5-tier order preserving #143 invariants:
  1. Aggregators (``Aggregator Settings.price_list``)
  2. ``room_wise_menu``
  3. Active ``URY Restaurant Serving Period`` (``menu`` / ``price_list_override``)
  4. ``order_type_wise_menu`` (non-table orders)
  5. ``URY Restaurant.active_menu``
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import frappe
    from frappe import _
except ModuleNotFoundError:  # site-less test double injects frappe onto caller module
    frappe = None  # type: ignore[assignment]

    def _(msg, *args):
        return msg


def _cint(value) -> int:
    fn = getattr(getattr(frappe, "utils", None), "cint", None)
    if callable(fn):
        return fn(value)
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _flt(value, precision=None) -> float:
    fn = getattr(getattr(frappe, "utils", None), "flt", None)
    if callable(fn):
        return fn(value, precision) if precision is not None else fn(value)
    val = float(value or 0)
    return round(val, int(precision)) if precision is not None else val


def _getdate(value=None) -> date:
    fn = getattr(getattr(frappe, "utils", None), "getdate", None)
    if callable(fn):
        return fn(value)
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _get_datetime(value=None) -> datetime:
    fn = getattr(getattr(frappe, "utils", None), "get_datetime", None)
    if callable(fn):
        return fn(value)
    if isinstance(value, datetime):
        return value
    if value is None:
        return datetime.now()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _get_system_timezone() -> str | None:
    fn = getattr(getattr(frappe, "utils", None), "get_system_timezone", None)
    if callable(fn):
        return fn()
    return None


cint = _cint
flt = _flt
getdate = _getdate
get_datetime = _get_datetime
get_system_timezone = _get_system_timezone

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_LOOKUP = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_DAY_SECONDS = 86400


def _parse_time_seconds(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        if total < 0 or total >= _DAY_SECONDS:
            total = total % _DAY_SECONDS
        return total
    if isinstance(value, datetime):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second

    raw = str(value).strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        frappe.throw(_("Invalid time value: {0}. Use HH:MM or HH:MM:SS.").format(value), frappe.ValidationError)
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(float(parts[2])) if len(parts) == 3 else 0
    except ValueError:
        frappe.throw(_("Invalid time value: {0}. Use HH:MM or HH:MM:SS.").format(value), frappe.ValidationError)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        frappe.throw(_("Time value out of range: {0}.").format(value), frappe.ValidationError)
    return hours * 3600 + minutes * 60 + seconds


def _format_hhmm(seconds: int) -> str:
    hours, rem = divmod(seconds % _DAY_SECONDS, 3600)
    minutes = rem // 60
    return f"{hours:02d}:{minutes:02d}"


def _parse_weekdays(raw) -> set[int]:
    if not raw or not str(raw).strip():
        return set(range(7))
    tokens = [tok.strip() for tok in str(raw).replace("\n", ",").split(",") if tok.strip()]
    days = set()
    for token in tokens:
        idx = _WEEKDAY_LOOKUP.get(token.lower())
        if idx is None:
            frappe.throw(
                _("Invalid weekday '{0}' in Serving Period. Use Mon, Tue, Wed, Thu, Fri, Sat, Sun.").format(token),
                frappe.ValidationError,
            )
        days.add(idx)
    return days


def _parse_order_types(raw) -> set[str]:
    if not raw or not str(raw).strip():
        return set()
    return {tok.strip() for tok in str(raw).replace("\n", ",").split(",") if tok.strip()}


def _weekly_segments(days: set[int], start_sec: int, end_sec: int) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    for day in sorted(days):
        day_start = day * _DAY_SECONDS
        if start_sec < end_sec:
            segments.append((day_start + start_sec, day_start + end_sec))
        else:
            # Midnight wrap: [start_sec, 24:00) on `day` plus [00:00, end_sec) on `(day + 1) % 7`
            segments.append((day_start + start_sec, day_start + _DAY_SECONDS))
            next_day = (day + 1) % 7
            if end_sec > 0:
                segments.append((next_day * _DAY_SECONDS, next_day * _DAY_SECONDS + end_sec))
    return segments


def _segments_overlap(segs_a: list[tuple[int, int]], segs_b: list[tuple[int, int]]) -> bool:
    for s1, e1 in segs_a:
        for s2, e2 in segs_b:
            if s1 < e2 and s2 < e1:
                return True
    return False


def validate_serving_periods(rows) -> None:
    """Validate child table rows on URY Restaurant.custom_serving_periods.

    Rejects zero-length windows, invalid weekday tokens, and any overlapping
    enabled serving periods that share the same priority and intersect on order
    types (including overnight midnight-wrapping carryover segments).
    """
    parsed = []
    for index, row in enumerate(rows or [], start=1):
        if not cint(row.get("enabled", 1)):
            continue
        period_name = (row.get("period_name") or "").strip()
        if not period_name:
            frappe.throw(_("Row #{0}: Serving Period Name is required.").format(index), frappe.ValidationError)
        start_sec = _parse_time_seconds(row.get("start_time"))
        end_sec = _parse_time_seconds(row.get("end_time"))
        if start_sec is None or end_sec is None:
            frappe.throw(
                _("Serving Period '{0}' requires both Start Time and End Time.").format(period_name),
                frappe.ValidationError,
            )
        if start_sec == end_sec:
            frappe.throw(
                _("Serving Period '{0}' cannot have identical Start Time and End Time.").format(period_name),
                frappe.ValidationError,
            )
        days = _parse_weekdays(row.get("weekdays"))
        order_types = _parse_order_types(row.get("order_types"))
        priority = cint(row.get("priority") if row.get("priority") is not None else 10)
        segments = _weekly_segments(days, start_sec, end_sec)
        parsed.append(
            {
                "name": period_name,
                "priority": priority,
                "order_types": order_types,
                "segments": segments,
            }
        )

    for i in range(len(parsed)):
        a = parsed[i]
        for j in range(i + 1, len(parsed)):
            b = parsed[j]
            if a["priority"] != b["priority"]:
                continue
            ot_intersects = (not a["order_types"]) or (not b["order_types"]) or bool(a["order_types"] & b["order_types"])
            if not ot_intersects:
                continue
            if _segments_overlap(a["segments"], b["segments"]):
                frappe.throw(
                    _(
                        "Serving Periods '{0}' and '{1}' overlap at the same priority ({2}). "
                        "Use distinct priorities or non-overlapping windows."
                    ).format(a["name"], b["name"], a["priority"]),
                    frappe.ValidationError,
                )


def _valid_zoneinfo(tz_name: str | None) -> ZoneInfo | None:
    if not tz_name or not str(tz_name).strip():
        return None
    try:
        return ZoneInfo(str(tz_name).strip())
    except ZoneInfoNotFoundError:
        return None


def get_outlet_timezone(branch: str | None = None) -> str:
    """Resolve outlet IANA timezone from URY Branch Operating Schedule.timezone."""
    if branch:
        try:
            configured = frappe.db.get_value(
                "URY Branch Operating Schedule", {"branch": branch}, "timezone"
            )
        except Exception:
            configured = None
        if _valid_zoneinfo(configured):
            return str(configured).strip()

    try:
        sys_tz = get_system_timezone()
    except Exception:
        sys_tz = None
    if _valid_zoneinfo(sys_tz):
        return str(sys_tz).strip()
    return "UTC"


def resolve_outlet_local_datetime(
    branch: str | None = None, at=None
) -> tuple[datetime, str, str]:
    """Return ``(local_dt, 'HH:MM:SS', 'Mon'..'Sun')`` in the outlet's timezone."""
    tz_name = get_outlet_timezone(branch)
    outlet_tz = ZoneInfo(tz_name)

    if at is None:
        local_dt = datetime.now(timezone.utc).astimezone(outlet_tz)
    else:
        dt = at
        if isinstance(dt, str):
            raw = dt.strip()
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                dt = get_datetime(raw)
        elif isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day)

        if dt.tzinfo is None:
            sys_tz = _valid_zoneinfo(get_system_timezone()) or timezone.utc
            dt = dt.replace(tzinfo=sys_tz)
        local_dt = dt.astimezone(outlet_tz)

    local_time_str = local_dt.strftime("%H:%M:%S")
    weekday = WEEKDAY_NAMES[local_dt.weekday()]
    return local_dt, local_time_str, weekday


def get_serving_periods(restaurant: str | None) -> list:
    if not restaurant:
        return []
    rows = frappe.get_all(
        "URY Restaurant Serving Period",
        filters={"parent": restaurant, "parenttype": "URY Restaurant", "enabled": 1},
        fields=[
            "period_name",
            "enabled",
            "start_time",
            "end_time",
            "weekdays",
            "order_types",
            "menu",
            "price_list_override",
            "priority",
            "idx",
        ],
        order_by="priority desc, idx asc",
    )
    return list(rows) if isinstance(rows, (list, tuple)) else []


def _period_matches(row, local_dt: datetime, order_type: str | None = None) -> bool:
    if not cint(row.get("enabled", 1)):
        return False
    allowed_order_types = _parse_order_types(row.get("order_types"))
    if allowed_order_types:
        if not order_type or order_type not in allowed_order_types:
            return False

    start_sec = _parse_time_seconds(row.get("start_time"))
    end_sec = _parse_time_seconds(row.get("end_time"))
    if start_sec is None or end_sec is None or start_sec == end_sec:
        return False

    now_sec = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
    days = _parse_weekdays(row.get("weekdays"))
    today_wd = local_dt.weekday()
    yesterday_wd = (today_wd - 1) % 7

    if start_sec < end_sec:
        return today_wd in days and start_sec <= now_sec < end_sec
    return (today_wd in days and now_sec >= start_sec) or (yesterday_wd in days and now_sec < end_sec)


def resolve_active_serving_period(
    restaurant: str | None,
    order_type: str | None = None,
    local_dt: datetime | None = None,
    branch: str | None = None,
    at=None,
) -> dict | None:
    """Resolve the highest-priority active Serving Period for a restaurant at outlet local time."""
    if not restaurant:
        return None
    if local_dt is None:
        local_dt, _, _ = resolve_outlet_local_datetime(branch, at=at)

    rows = get_serving_periods(restaurant)
    if not rows:
        return None

    matches = [row for row in rows if _period_matches(row, local_dt, order_type=order_type)]
    if not matches:
        return None

    matches.sort(key=lambda r: (-cint(r.get("priority") or 0), cint(r.get("idx") or 0)))
    winner = matches[0]
    start_sec = _parse_time_seconds(winner.get("start_time")) or 0
    end_sec = _parse_time_seconds(winner.get("end_time")) or 0
    return {
        "period_name": winner.get("period_name"),
        "start_time": _format_hhmm(start_sec),
        "end_time": _format_hhmm(end_sec),
        "menu": winner.get("menu") or None,
        "price_list_override": winner.get("price_list_override") or None,
        "priority": cint(winner.get("priority") or 0),
    }


def resolve_menu_and_price_list(
    branch: str | None,
    restaurant: str | None = None,
    table: str | None = None,
    room: str | None = None,
    order_type: str | None = None,
    customer: str | None = None,
    local_dt: datetime | None = None,
    at=None,
    cashier: bool = True,
) -> dict:
    """Resolve menu and selling Price List in exact 5-tier priority order:
    1. Aggregators (Aggregator Settings.price_list)
    2. room_wise_menu
    3. Active URY Restaurant Serving Period (menu / price_list_override)
    4. order_type_wise_menu (counter orders only; table checks ignore order_type_wise_menu per #143)
    5. URY Restaurant.active_menu
    """
    tz_name = get_outlet_timezone(branch)
    if local_dt is not None:
        local_time_str = local_dt.strftime("%H:%M:%S")
        weekday = WEEKDAY_NAMES[local_dt.weekday()]
    else:
        local_dt, local_time_str, weekday = resolve_outlet_local_datetime(branch, at=at)

    base = {
        "source": "active_menu",
        "menu": None,
        "price_list": None,
        "serving_period": None,
        "local_dt": local_dt,
        "local_time": local_time_str,
        "weekday": weekday,
        "timezone": tz_name,
    }

    # Tier 1: Aggregators always wins
    if order_type == "Aggregators":
        agg_pl = frappe.db.get_value(
            "Aggregator Settings",
            {"customer": customer, "parent": branch, "parenttype": "Branch"},
            "price_list",
        )
        return {**base, "source": "aggregator", "price_list": agg_pl}

    if not restaurant and branch:
        restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")
    if not restaurant:
        return base

    active_period = resolve_active_serving_period(
        restaurant, order_type=order_type, local_dt=local_dt, branch=branch
    )

    # Tier 2: room_wise_menu
    effective_room = room
    if not effective_room and table:
        effective_room = frappe.db.get_value("URY Table", table, "restaurant_room")

    if effective_room and frappe.db.get_value("URY Restaurant", restaurant, "room_wise_menu"):
        room_menu = frappe.db.get_value(
            "Menu for Room", {"parent": restaurant, "room": effective_room}, "menu"
        )
        if room_menu:
            pl = frappe.db.get_value("Price List", {"restaurant_menu": room_menu, "enabled": 1}, "name")
            return {
                **base,
                "source": "room",
                "menu": room_menu,
                "price_list": pl,
                "serving_period": active_period,
            }

    # Tier 3: Active Serving Period
    if active_period and (active_period.get("menu") or active_period.get("price_list_override")):
        sp_menu = active_period.get("menu") or frappe.db.get_value("URY Restaurant", restaurant, "active_menu")
        sp_pl = active_period.get("price_list_override") or (
            frappe.db.get_value("Price List", {"restaurant_menu": sp_menu, "enabled": 1}, "name")
            if sp_menu
            else None
        )
        return {
            **base,
            "source": "serving_period",
            "menu": sp_menu,
            "price_list": sp_pl,
            "serving_period": active_period,
        }

    # Tier 4: order_type_wise_menu (counter orders only — table orders ignore per #143)
    if not table and cashier and order_type:
        if frappe.db.get_value("URY Restaurant", restaurant, "order_type_wise_menu"):
            ot_menu = frappe.db.get_value(
                "Order Type Menu", {"parent": restaurant, "order_type": order_type}, "menu"
            )
            if ot_menu:
                pl = frappe.db.get_value("Price List", {"restaurant_menu": ot_menu, "enabled": 1}, "name")
                return {
                    **base,
                    "source": "order_type",
                    "menu": ot_menu,
                    "price_list": pl,
                    "serving_period": active_period,
                }

    # Tier 5: Default active_menu
    default_menu = frappe.db.get_value("URY Restaurant", restaurant, "active_menu")
    default_pl = (
        frappe.db.get_value("Price List", {"restaurant_menu": default_menu, "enabled": 1}, "name")
        if default_menu
        else None
    )
    return {
        **base,
        "source": "active_menu",
        "menu": default_menu,
        "price_list": default_pl,
        "serving_period": active_period,
    }


def evaluate_item_daypart(
    item_row,
    active_period: dict | None = None,
    local_dt: datetime | None = None,
    branch: str | None = None,
    at=None,
) -> dict:
    """Evaluate per-item daypart availability window on a URY Menu Item row."""
    from_raw = item_row.get("custom_available_from")
    to_raw = item_row.get("custom_available_to")
    required_period = (item_row.get("custom_serving_period") or "").strip()

    if from_raw and to_raw:
        start_sec = _parse_time_seconds(from_raw)
        end_sec = _parse_time_seconds(to_raw)
        if start_sec is not None and end_sec is not None and start_sec != end_sec:
            if local_dt is None:
                local_dt, _, _ = resolve_outlet_local_datetime(branch, at=at)
            now_sec = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
            if start_sec < end_sec:
                in_window = start_sec <= now_sec < end_sec
            else:
                in_window = now_sec >= start_sec or now_sec < end_sec
            if not in_window:
                return {
                    "unavailable_daypart": True,
                    "daypart_label": f"Available {_format_hhmm(start_sec)}\u2013{_format_hhmm(end_sec)}",
                }

    if required_period:
        current_name = (active_period or {}).get("period_name") or ""
        if current_name.strip().lower() != required_period.lower():
            return {
                "unavailable_daypart": True,
                "daypart_label": f"Available during {required_period}",
            }

    return {"unavailable_daypart": False, "daypart_label": None}


def stamp_invoice_outlet_time(doc, at=None, overwrite: bool = False) -> dict:
    """Stamp outlet-timezone local time, weekday, and active serving period onto POS Invoice.

    Honors ``doc.ury_offline_captured_at`` (or explicit ``at``) for offline replays so
    time-of-day ``Pricing Rule`` conditions evaluate at capture time rather than replay time.
    """
    effective_at = at if at is not None else doc.get("ury_offline_captured_at")
    branch = doc.get("branch")
    local_dt, local_time_str, weekday = resolve_outlet_local_datetime(branch, at=effective_at)

    if overwrite or not doc.get("custom_transaction_local_time"):
        doc.custom_transaction_local_time = local_time_str
    if overwrite or not doc.get("custom_transaction_weekday"):
        doc.custom_transaction_weekday = weekday

    restaurant = doc.get("restaurant")
    if not restaurant and branch:
        restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")

    active_period = resolve_active_serving_period(
        restaurant,
        order_type=doc.get("order_type"),
        local_dt=local_dt,
        branch=branch,
    )
    if overwrite:
        doc.custom_serving_period = active_period["period_name"] if active_period else None
    elif active_period and not doc.get("custom_serving_period"):
        doc.custom_serving_period = active_period["period_name"]

    return {
        "local_dt": local_dt,
        "local_time": local_time_str,
        "weekday": weekday,
        "serving_period": active_period,
    }


def serving_period_applies_to_order_type(
    restaurant: str | None, period_name: str | None, order_type: str | None
) -> bool:
    """Return True if ``period_name`` is an enabled Serving Period on ``restaurant`` valid for ``order_type``."""
    if not restaurant or not period_name:
        return False
    target = str(period_name).strip()
    ot = (order_type or "").strip()
    for row in get_serving_periods(restaurant):
        if (row.get("period_name") or "").strip() != target:
            continue
        allowed = _parse_order_types(row.get("order_types"))
        if not allowed or (ot and ot in allowed):
            return True
    return False


def allowed_price_lists_for_period(restaurant: str | None, period_name: str | None) -> set[str]:
    """Return every enabled Price List belonging to a stamped Serving Period on ``restaurant``."""
    if not restaurant or not period_name:
        return set()
    rows = get_serving_periods(restaurant)
    allowed: set[str] = set()
    for row in rows:
        if (row.get("period_name") or "").strip() != str(period_name).strip():
            continue
        if row.get("price_list_override"):
            allowed.add(row.get("price_list_override"))
        if row.get("menu"):
            pl = frappe.db.get_value("Price List", {"restaurant_menu": row.get("menu"), "enabled": 1}, "name")
            if pl:
                allowed.add(pl)
    return allowed


def active_item_promos(
    menu_items: list[dict],
    *,
    company: str | None = None,
    price_list: str | None = None,
    customer: str | None = None,
    local_dt: datetime | None = None,
    posting_date=None,
    order_type: str | None = None,
) -> dict[str, dict]:
    """Read-only lookup of active selling Pricing Rules (including time-of-day conditions)
    so MenuCard can show strike-through base price + promo badge without duplicating rule math."""
    if not menu_items or not company:
        return {}
    try:
        rules = frappe.get_all(
            "Pricing Rule",
            filters={"disable": 0, "selling": 1, "company": company},
            fields=[
                "name",
                "title",
                "apply_on",
                "rate_or_discount",
                "discount_percentage",
                "discount_amount",
                "rate",
                "valid_from",
                "valid_upto",
                "for_price_list",
                "condition",
                "priority",
            ],
            order_by="priority desc, creation desc",
        )
    except Exception:
        return {}
    if not isinstance(rules, (list, tuple)) or not rules:
        return {}

    today_date = getdate(posting_date) if posting_date else getdate()
    local_time_str = local_dt.strftime("%H:%M:%S") if local_dt else ""
    weekday = WEEKDAY_NAMES[local_dt.weekday()] if local_dt else ""
    eval_env = {
        "custom_transaction_local_time": local_time_str,
        "custom_transaction_weekday": weekday,
        "order_type": order_type or "",
        "selling_price_list": price_list or "",
        "customer": customer or "",
        "company": company,
    }

    item_codes = [row["item"] for row in menu_items if row.get("item")]
    item_groups: dict[str, str] = {}
    if any(r.get("apply_on") == "Item Group" for r in rules) and item_codes:
        try:
            for item_row in frappe.get_all(
                "Item", filters={"name": ["in", item_codes]}, fields=["name", "item_group"]
            ):
                item_groups[item_row["name"]] = item_row.get("item_group") or ""
        except Exception:
            pass

    promos: dict[str, dict] = {}
    for rule in rules:
        if rule.get("valid_from") and getdate(rule["valid_from"]) > today_date:
            continue
        if rule.get("valid_upto") and getdate(rule["valid_upto"]) < today_date:
            continue
        if rule.get("for_price_list") and price_list and rule["for_price_list"] != price_list:
            continue
        cond = (rule.get("condition") or "").strip()
        if cond:
            try:
                if not frappe.safe_eval(cond, None, eval_env):
                    continue
            except Exception:
                continue

        apply_on = rule.get("apply_on")
        matched_codes: set[str] = set()
        if apply_on == "Item Code":
            try:
                targets = frappe.get_all(
                    "Pricing Rule Item Code",
                    filters={"parent": rule["name"]},
                    pluck="item_code",
                )
                matched_codes = set(targets or []) & set(item_codes)
            except Exception:
                continue
        elif apply_on == "Item Group":
            try:
                target_groups = set(
                    frappe.get_all(
                        "Pricing Rule Item Group",
                        filters={"parent": rule["name"]},
                        pluck="item_group",
                    )
                    or []
                )
                matched_codes = {c for c in item_codes if item_groups.get(c) in target_groups}
            except Exception:
                continue
        else:
            continue

        title = (rule.get("title") or rule.get("name") or "").strip()
        rod = rule.get("rate_or_discount")
        for row in menu_items:
            code = row.get("item")
            if code not in matched_codes or code in promos:
                continue
            base_rate = flt(row.get("rate"))
            if base_rate <= 0:
                continue
            promo_rate = None
            label = title
            if rod == "Discount Percentage" and flt(rule.get("discount_percentage")) > 0:
                pct = flt(rule.get("discount_percentage"))
                promo_rate = round(base_rate * (1.0 - pct / 100.0), 4)
                pct_str = str(int(pct)) if pct == int(pct) else f"{pct:g}"
                if title and f"{pct_str}%" in title:
                    label = title
                else:
                    label = f"{title} -{pct_str}%" if title else f"-{pct_str}%"
            elif rod == "Discount Amount" and flt(rule.get("discount_amount")) > 0:
                promo_rate = max(0.0, round(base_rate - flt(rule.get("discount_amount")), 4))
            elif rod == "Rate" and flt(rule.get("rate")) > 0 and flt(rule.get("rate")) < base_rate:
                promo_rate = flt(rule.get("rate"))

            if promo_rate is not None and promo_rate < base_rate:
                promos[code] = {
                    "promo_rate": promo_rate,
                    "promo_label": label,
                    "pricing_rule": rule["name"],
                }

    return promos
