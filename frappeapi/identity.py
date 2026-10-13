"""One person, one User (integration spec §5.1)."""

import contextlib
import re
import secrets
import time
import unicodedata

import frappe
from frappe import _

from frappeapi import state
from frappeapi.errors import ServiceUnavailable

LOCK_TTL_MS = 15_000
_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


def _site_country_code() -> str | None:
	"""The dial code of the site's country (System Settings > Country)."""
	from frappe.geo.country_info import get_country_info

	country = frappe.db.get_single_value("System Settings", "country")
	return (get_country_info(country) or {}).get("isd") if country else None


def _country_code(default_country_code) -> str | None:
	cc = default_country_code
	if cc is None:
		cc = frappe.conf.get("frappeapi_default_country_code")
	if cc is None:
		cc = _site_country_code()
	if cc is None:
		return None
	cc = str(cc).strip().removeprefix("+")
	if not re.fullmatch(r"[1-9][0-9]{0,2}", cc):
		raise ValueError("invalid default country code")
	return cc


def normalize_mobile(value: str, default_country_code: str | None = None) -> str:
	"""Return +<country code><number>. International forms (+CC... or 00CC...) are
	always accepted. National forms (0..., or the bare number with or without its
	country code) need a country code: the argument (e.g. the Company's country),
	else frappe.conf.frappeapi_default_country_code, else the dial code of
	System Settings > Country; with none of them they are refused."""
	if not isinstance(value, str):
		raise ValueError("mobile number is required")
	raw = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value).strip()
	if not raw or not re.fullmatch(r"[+\d\s().-]+", raw):
		raise ValueError("invalid mobile number")
	if raw.count("+") > 1 or ("+" in raw and not raw.startswith("+")):
		raise ValueError("invalid mobile number")
	raw = re.sub(r"[\s().-]", "", raw)
	if raw.startswith("00"):
		raw = "+" + raw[2:]
	if raw.startswith("+"):
		digits = raw[1:]
	else:
		cc = _country_code(default_country_code)
		if cc is None:
			raise ValueError("invalid mobile number: use the international format (+country code)")
		if raw.startswith("0"):
			digits = cc + raw[1:]
		elif raw.startswith(cc):
			digits = raw
		else:
			digits = cc + raw
	if not digits.isdigit() or not 10 <= len(digits) <= 15:
		raise ValueError("invalid mobile number")
	return "+" + digits


def user_by_mobile(mobile: str, *, default_country_code: str | None = None) -> str | None:
	try:
		mobile = normalize_mobile(mobile, default_country_code)
	except ValueError:
		return None
	users = frappe.get_all("User", filters={"mobile_no": mobile, "enabled": 1}, pluck="name", limit=2)
	if len(users) > 1:
		raise frappe.DuplicateEntryError(_("More than one user has this mobile number"))
	return users[0] if users else None


def assert_unique_mobile(user: str, mobile: str, *, default_country_code: str | None = None) -> None:
	mobile = normalize_mobile(mobile, default_country_code)
	if frappe.db.exists("User", {"mobile_no": mobile, "enabled": 1, "name": ["!=", user]}):
		raise frappe.DuplicateEntryError(_("Another user already uses this mobile number"))


def _held() -> dict:
	if not hasattr(frappe.local, "_frappeapi_mobile_locks"):
		frappe.local._frappeapi_mobile_locks = {}
	return frappe.local._frappeapi_mobile_locks


def _release(k: str, token: str) -> None:
	_held().pop(k, None)
	with contextlib.suppress(Exception):  # ponytail: PX expiry frees it; never break a commit callback
		state.redis().eval(_RELEASE, 1, k, token)


def lock_mobile(mobile: str, wait: float = 3.0, *, default_country_code: str | None = None) -> None:
	"""Hold the per-mobile lock until this transaction commits or rolls back.

	Lock order (spec §4.3): take DB row locks first, then this. Never lock an
	invitation, link request or Customer while holding it."""
	k = state.key("lock:user", normalize_mobile(mobile, default_country_code))
	if k in _held():
		# ponytail: the lock has a 15s ceiling (LOCK_TTL_MS); if it expired, take it again
		with state.guarded():
			current = state.redis().get(k)
		if current is not None and current.decode() == _held()[k]:
			return
		_held().pop(k)
	token = secrets.token_hex(16)
	deadline = time.monotonic() + wait
	with state.guarded():
		while not state.redis().set(k, token, nx=True, px=LOCK_TTL_MS):
			if time.monotonic() > deadline:
				raise ServiceUnavailable(_("Another request is updating this account. Try again."))
			time.sleep(0.05)
	_held()[k] = token
	frappe.db.after_commit.add(lambda: _release(k, token))
	frappe.db.after_rollback.add(lambda: _release(k, token))


def _holders(mobile: str, lock: bool) -> list:
	return frappe.db.sql(
		"select name, enabled from `tabUser` where mobile_no=%s" + (" for update" if lock else ""), mobile, as_dict=True
	)


def _mobile_clash(exc: Exception) -> bool:
	"""True when the unique violation is on the mobile_no index (MariaDB names the key)."""
	text = " ".join(str(a) for a in exc.args) + " " + str(exc.__context__ or "")
	return "for key 'mobile_no'" in text


def find_or_create_user(
	mobile: str, email: str, first_name: str, roles: list[str], *, default_country_code: str | None = None
) -> tuple[str, bool]:
	"""The only way an app using frappeapi creates a human User. An existing User keeps
	their password; only missing roles are added.

	Lock order: the tabUser row lock is taken after the Redis mobile lock. That is a
	documented exception to lock_mobile's order; a cycle is bounded by the 3s Redis
	wait (503). A FOR UPDATE that misses would gap-lock the unique mobile_no index and
	deadlock concurrent first signups, so a miss never locks: the INSERT (in a
	savepoint) arbitrates. A unique violation means the row was committed after our
	snapshot and is invisible to plain reads, so it raises ServiceUnavailable (retryable).
	Any other unique violation (e.g. the email) is re-raised; which index clashed is read
	from the database error, so that path takes no lock either.

	Any matching User is returned, System Users included: whether to issue a session,
	and with what scope, is the caller's decision."""
	mobile = normalize_mobile(mobile, default_country_code)
	lock_mobile(mobile)
	holders = _holders(mobile, lock=False)
	if holders:
		holders = _holders(mobile, lock=True)  # record lock; also sees rows committed since the snapshot
	if not holders:
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"mobile_no": mobile,
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		)
		frappe.db.savepoint("frappeapi_user_insert")
		logged = len(frappe.local.message_log)
		try:
			doc.insert(ignore_permissions=True)
			return doc.name, True
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError) as e:
			frappe.db.rollback(save_point="frappeapi_user_insert")
			del frappe.local.message_log[logged:]  # the "must be unique" msgprint is ours, not the client's
			# MariaDB names the key in the error; Postgres has no gap locks, so a locking re-read is safe there
			clash = bool(_holders(mobile, lock=True)) if frappe.db.db_type == "postgres" else _mobile_clash(e)
			if not clash:
				raise  # the clash was on something other than the mobile number
			# Committed after our snapshot, so plain reads cannot see it. A retry takes the normal path.
			raise ServiceUnavailable(_("This account was just created elsewhere. Try again."))
	active = [h.name for h in holders if h.enabled]
	if len(active) > 1:
		raise frappe.DuplicateEntryError(_("More than one user has this mobile number"))
	if not active:
		raise frappe.ValidationError(_("A disabled user holds this mobile number. Re-enable that user instead."))
	user = active[0]
	missing = set(roles) - set(frappe.get_roles(user))
	if missing:
		doc = frappe.get_doc("User", user)
		doc.flags.ignore_permissions = True
		doc.add_roles(*sorted(missing))
	return user, False


def check_password(user: str, password: str) -> None:
	"""Frappe's LoginManager owns the lockout and the disabled check."""
	login_manager = getattr(frappe.local, "login_manager", None)
	if login_manager is None:
		raise frappe.AuthenticationError(_("No request context for authentication"))
	login_manager.authenticate(user=user, pwd=password)
