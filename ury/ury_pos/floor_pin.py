"""Paired floor tablets use short PINs only while their till is open."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import unquote

import frappe
import frappe.sessions
from frappe.auth import LoginAttemptTracker
from frappe.core.doctype.activity_log.activity_log import add_authentication_log
from frappe.rate_limiter import rate_limit
from frappe.utils import get_fullname
from frappe.utils.password import get_encryption_key


def _key(purpose):
	return hmac.digest(get_encryption_key().encode(), purpose.encode(), "sha256")


def _digest(purpose, value):
	return hmac.new(_key(purpose), value.encode(), hashlib.sha256).hexdigest()


def _refuse(code, message):
	frappe.throw(f"{code}: {message}", frappe.AuthenticationError)


def _enabled(profile):
	return not profile.get("disabled") and profile.get("custom_floor_pin_login")


def _eligible(user, profile):
	roles = set(frappe.get_roles(user))
	return (
		user not in ("Administrator", "Guest")
		and "System Manager" not in roles
		and frappe.db.get_value("User", user, "enabled") == 1
		and bool(roles & {r.role for r in profile.get("custom_floor_pin_roles", [])})
		and not roles & {r.role for r in profile.get("custom_floor_stay_signed_in_roles", [])}
		and frappe.db.exists("URY User", {"parent": profile.branch, "parenttype": "Branch", "user": user})
	)


def _device():
	try:
		token = unquote((frappe.request.cookies if frappe.request else {}).get("ury_floor_device", ""))
		payload, signature = token.rsplit("|", 1)
		if not hmac.compare_digest(_digest("ury-floor-device-v1", payload), signature):
			return None
		version, name, device_id, epoch, label = payload.split("|")
		if version != "v1" or not re.fullmatch(r"[0-9a-f]{16}", device_id):
			return None
		if not frappe.db.exists("POS Profile", name):
			return None
		profile = frappe.get_doc("POS Profile", name)
		if _enabled(profile) and int(epoch) == int(profile.get("custom_floor_device_epoch") or 0):
			return profile, device_id, label
	except (ValueError, TypeError):
		pass
	return None


def _till_open(profile):
	return bool(frappe.db.exists("POS Opening Entry", {
		"pos_profile": profile.name, "status": "Open", "docstatus": 1,
	}))


def get_floor_pad():
	device = _device()
	return {"paired": bool(device), "till_open": bool(device and _till_open(device[0]))}


@frappe.whitelist(methods=["POST"])
def pair_device(pos_profile, label):
	profile = frappe.get_doc("POS Profile", pos_profile)
	label = (label or "").strip()
	if (not _enabled(profile) or not frappe.has_permission("POS Profile", "write", profile)
		or not label or len(label) > 80 or any(c in label + pos_profile for c in "|\r\n")):
		_refuse("URY_FLOOR_PIN_NOT_PERMITTED", "This tablet cannot be paired.")
	payload = f"v1|{profile.name}|{secrets.token_hex(8)}|{int(profile.get('custom_floor_device_epoch') or 0)}|{label}"
	frappe.local.cookie_manager.set_cookie(
		"ury_floor_device", f"{payload}|{_digest('ury-floor-device-v1', payload)}",
		httponly=True, secure=True, samesite="Strict", max_age=400 * 86400,
	)
	# CookieManager uses Werkzeug's default Path=/; it does not accept a path argument.
	profile.add_comment("Info", f"Floor tablet '{escape(label)}' paired by {escape(frappe.session.user)}")


def _managed_profile(user=None):
	for row in frappe.get_all("POS Profile", filters={"custom_floor_pin_login": 1, "disabled": 0}, fields=["name"]):
		profile = frappe.get_doc("POS Profile", row.name)
		if (_enabled(profile) and frappe.has_permission("POS Profile", "write", profile)
			and (user is None or _eligible(user, profile))):
			return profile
	_refuse("URY_FLOOR_PIN_NOT_PERMITTED", "A manager must issue a PIN to eligible floor staff.")


@frappe.whitelist(methods=["POST"])
def issue_pin(user):
	_managed_profile(user)
	previous = frappe.db.get_value("User", user, "custom_floor_pin_hash")
	for _ in range(20):
		pin = f"{secrets.randbelow(10000):04d}"
		pin_hash = _digest("ury-floor-pin-v1", pin)
		if pin_hash == previous:
			continue
		try:
			frappe.db.set_value("User", user, "custom_floor_pin_hash", pin_hash, update_modified=False)
		except Exception as error:
			if isinstance(error, frappe.UniqueValidationError) or frappe.db.is_unique_key_violation(error):
				continue
			raise
		add_authentication_log(f"Floor PIN issued for {user} by {frappe.session.user}", frappe.session.user)
		return {"pin": pin}
	_refuse("URY_FLOOR_PIN_NOT_PERMITTED", "A PIN could not be issued. Try again.")


@frappe.whitelist(methods=["GET"])
def pin_staff():
	profile = _managed_profile()
	return [
		{
			"user": user,
			"full_name": get_fullname(user),
			"has_pin": bool(frappe.db.get_value("User", user, "custom_floor_pin_hash")),
		}
		for user in frappe.get_all("User", filters={"enabled": 1}, pluck="name")
		if _eligible(user, profile)
	]


@frappe.whitelist(allow_guest=True, methods=["POST"])
def sign_in(pin):
	# Scrub before rate_limit too: even a rate-limited request must not log its PIN.
	frappe.form_dict.pop("pin", None)
	return _sign_in(pin)


@rate_limit(limit=60, seconds=60)
def _sign_in(pin):
	device = _device()
	if not device:
		_refuse("URY_FLOOR_DEVICE_NOT_PAIRED", "Sign in with username to pair this tablet.")
	profile, device_id, label = device
	if not _till_open(profile):
		_refuse("URY_FLOOR_TILL_CLOSED", "Sign in with username while the till is closed.")
	short = LoginAttemptTracker(f"ury-floor-short:{device_id}", max_consecutive_login_attempts=4, lock_interval=300)
	day = LoginAttemptTracker(f"ury-floor-day:{device_id}", max_consecutive_login_attempts=19, lock_interval=86400)
	if not short.is_user_allowed() or not day.is_user_allowed():
		_refuse("URY_FLOOR_PIN_LOCKED", "PIN sign-in is paused on this tablet.")
	user = None
	if isinstance(pin, str) and re.fullmatch(r"[0-9]{4}", pin):
		user = frappe.db.get_value("User", {"custom_floor_pin_hash": _digest("ury-floor-pin-v1", pin), "enabled": 1}, "name")
	if not user or not _eligible(user, profile):
		short.add_failure_attempt()
		day.add_failure_attempt()
		add_authentication_log(f"Floor PIN refused on tablet '{escape(label)}'", "Guest", status="Failed")
		frappe.db.commit()  # As LoginManager.fail: retain the audit across the refusal rollback.
		_refuse("URY_FLOOR_PIN_REFUSED", "That PIN didn't work.")
	short.add_success_attempt()
	frappe.local.login_manager.login_as(user, session_end=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat())
	frappe.sessions.clear_sessions(user, keep_current=True)
	return {"full_name": get_fullname(user), "csrf_token": frappe.sessions.get_csrf_token()}
