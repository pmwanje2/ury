"""Paired floor tablets use short PINs only while their till is open.

Refusal ids (closed, append-only):
- URY_FLOOR_PIN_NOT_PERMITTED (PermissionError): caller lacks POS Profile write permission.
- URY_FLOOR_TABLET_LABEL_INVALID (ValidationError): tablet label is blank, too long or has
  a reserved character.
- URY_FLOOR_PROFILE_NOT_PAIRABLE (ValidationError): the POS Profile name cannot be carried
  in the signed device cookie.
- URY_FLOOR_PIN_ISSUE_EXHAUSTED (ValidationError): no free PIN was found after retries.
- URY_FLOOR_PIN_LOGIN_DISABLED (ValidationError): the POS Profile has floor PIN login off or is
  disabled, or no POS Profile has floor PIN login on.
- URY_FLOOR_PIN_TARGET_INELIGIBLE (ValidationError): the user cannot hold a floor PIN (disabled,
  off-branch, no floor role, stay role, System Manager or Administrator).
- URY_FLOOR_DEVICE_NOT_PAIRED, URY_FLOOR_TILL_CLOSED, URY_FLOOR_PIN_LOCKED,
  URY_FLOOR_PIN_REFUSED (AuthenticationError): PIN sign-in refusals, as native login.
"""

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

DEVICE_COOKIE = "ury_floor_device"
MAX_LABEL_LENGTH = 80
RESERVED_CHARACTERS = "|\r\n"


def _key(purpose):
	return hmac.digest(get_encryption_key().encode(), purpose.encode(), "sha256")


def _digest(purpose, value):
	return hmac.new(_key(purpose), value.encode(), hashlib.sha256).hexdigest()


def _refuse(code, message, exc):
	frappe.throw(f"{code}: {frappe._(message)}", exc)


def _not_permitted(message):
	_refuse("URY_FLOOR_PIN_NOT_PERMITTED", message, frappe.PermissionError)


def _login_disabled(message):
	_refuse("URY_FLOOR_PIN_LOGIN_DISABLED", message, frappe.ValidationError)


def _sign_in_refused(code, message):
	_refuse(code, message, frappe.AuthenticationError)


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
		token = unquote((frappe.request.cookies if frappe.request else {}).get(DEVICE_COOKIE, ""))
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
	"""Page context for /pos: two booleans, never the device id or label."""
	device = _device()
	return {"paired": bool(device), "till_open": bool(device and _till_open(device[0]))}


@frappe.whitelist(methods=["POST"])
def pair_device(pos_profile, label):
	profile = frappe.get_doc("POS Profile", pos_profile)
	if not frappe.has_permission("POS Profile", "write", profile):
		_not_permitted("This tablet cannot be paired.")
	if not _enabled(profile):
		_login_disabled("Floor PIN sign-in is off for this POS Profile.")
	label = (label or "").strip()
	if not label or len(label) > MAX_LABEL_LENGTH or any(c in label for c in RESERVED_CHARACTERS):
		_refuse("URY_FLOOR_TABLET_LABEL_INVALID", "Enter a tablet label of up to 80 characters.", frappe.ValidationError)
	if any(c in profile.name for c in RESERVED_CHARACTERS):
		_refuse("URY_FLOOR_PROFILE_NOT_PAIRABLE", "This POS Profile name cannot be used for a floor tablet.", frappe.ValidationError)
	payload = f"v1|{profile.name}|{secrets.token_hex(8)}|{int(profile.get('custom_floor_device_epoch') or 0)}|{label}"
	# CookieManager uses Werkzeug's default Path=/; it does not accept a path argument.
	frappe.local.cookie_manager.set_cookie(
		DEVICE_COOKIE, f"{payload}|{_digest('ury-floor-device-v1', payload)}",
		httponly=True, secure=True, samesite="Strict", max_age=400 * 86400,
	)
	profile.add_comment("Info", f"Floor tablet '{escape(label)}' paired by {escape(frappe.session.user)}")


def _managed_profile(user=None):
	# Authorization first, so profile state is never disclosed to a caller without write.
	if not frappe.has_permission("POS Profile", "write"):
		_not_permitted("A manager must issue a PIN to eligible floor staff.")
	rows = frappe.get_all("POS Profile", filters={"custom_floor_pin_login": 1, "disabled": 0}, fields=["name"])
	profiles = [p for p in (frappe.get_doc("POS Profile", row.name) for row in rows) if _enabled(p)]
	if not profiles:
		_login_disabled("No POS Profile has floor PIN sign-in turned on.")
	profiles = [p for p in profiles if frappe.has_permission("POS Profile", "write", p)]
	if not profiles:
		_not_permitted("A manager must issue a PIN to eligible floor staff.")
	if user is None:
		return profiles[0]
	for profile in profiles:
		if _eligible(user, profile):
			return profile
	_refuse("URY_FLOOR_PIN_TARGET_INELIGIBLE", "This user cannot get a floor PIN.", frappe.ValidationError)


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
			# db.set_value raises the driver error; Document saves raise UniqueValidationError.
			if isinstance(error, frappe.UniqueValidationError) or frappe.db.is_unique_key_violation(error):
				continue
			raise
		add_authentication_log(f"Floor PIN issued for {user} by {frappe.session.user}", frappe.session.user)
		return {"pin": pin}
	_refuse("URY_FLOOR_PIN_ISSUE_EXHAUSTED", "A PIN could not be issued. Try again.", frappe.ValidationError)


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
		_sign_in_refused("URY_FLOOR_DEVICE_NOT_PAIRED", "Sign in with username to pair this tablet.")
	profile, device_id, label = device
	if not _till_open(profile):
		_sign_in_refused("URY_FLOOR_TILL_CLOSED", "Sign in with username while the till is closed.")
	short = LoginAttemptTracker(f"ury-floor-short:{device_id}", max_consecutive_login_attempts=4, lock_interval=300)
	day = LoginAttemptTracker(f"ury-floor-day:{device_id}", max_consecutive_login_attempts=19, lock_interval=86400)
	if not short.is_user_allowed() or not day.is_user_allowed():
		_sign_in_refused("URY_FLOOR_PIN_LOCKED", "PIN sign-in is paused on this tablet.")
	user = None
	if isinstance(pin, str) and re.fullmatch(r"[0-9]{4}", pin):
		user = frappe.db.get_value("User", {"custom_floor_pin_hash": _digest("ury-floor-pin-v1", pin), "enabled": 1}, "name")
	if not user or not _eligible(user, profile):
		short.add_failure_attempt()
		day.add_failure_attempt()
		add_authentication_log(f"Floor PIN refused on tablet '{escape(label)}'", "Guest", status="Failed")
		frappe.db.commit()  # As LoginManager.fail: retain the audit across the refusal rollback.
		_sign_in_refused("URY_FLOOR_PIN_REFUSED", "That PIN didn't work.")
	short.add_success_attempt()
	frappe.local.login_manager.login_as(user, session_end=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat())
	frappe.sessions.clear_sessions(user, keep_current=True)
	return {"full_name": get_fullname(user), "csrf_token": frappe.sessions.get_csrf_token()}
