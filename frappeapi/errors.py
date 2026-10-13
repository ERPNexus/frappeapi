"""The core error registry (integration spec §6.1). App-neutral: frappeapi knows
only the core codes. Each app registers its own extension codes and legacy
spellings at import; the contract layer rewrites canonical codes back to an
app's legacy spellings for clients that get legacy codes (§6.2)."""

import json
from importlib.resources import files

from frappeapi.responses import JSONResponse

REGISTRY = json.loads(files("frappeapi").joinpath("errors.json").read_text())
CORE: dict[str, int] = REGISTRY["core"]
LEGACY: dict[str, dict[str, str]] = {}  # app -> core code -> old spelling
_registered: dict[str, tuple[str, int]] = {}  # extension code -> (first app, status)


class RegistryError(Exception):
	pass


def legacy(app: str, code: str) -> str:
	return LEGACY.get(app, {}).get(code, code)


def status(code: str) -> int:
	if code in CORE:
		return CORE[code]
	if code in _registered:
		return _registered[code][1]
	for table in LEGACY.values():
		for canon, old in table.items():
			if old == code:
				return CORE[canon]
	raise KeyError(code)


def _claim(app: str, code: str, http_status: int) -> None:
	if code in CORE:
		raise RegistryError(f"{app}: {code} is a core code")
	first = _registered.get(code)
	if first and first[1] != http_status:
		raise RegistryError(f"{code}: {first[0]} uses {first[1]}, {app} uses {http_status}")
	for other, table in LEGACY.items():
		for canon, old in table.items():
			if old == code and CORE[canon] != http_status:
				raise RegistryError(f"{code}: {other} spells {canon} ({CORE[canon]}) with it, {app} uses {http_status}")


def register(app: str, codes: dict[str, int] | None = None, legacy_codes: dict[str, str] | None = None) -> None:
	"""Called by each app at import.

	codes: the app's extension codes -> HTTP status.
	legacy_codes: core code -> the old spelling the app's legacy clients expect
	(the old spelling keeps the core code's status).

	Refuses redefining a core code, legacy keys that are not core codes, one code
	getting two statuses across apps, and one old spelling naming two core codes
	(across apps, or within this call). Nothing is recorded unless the whole call is valid."""
	for code, http_status in (codes or {}).items():
		_claim(app, code, http_status)
	known = {c: s for c, (_, s) in _registered.items()} | (codes or {})
	spelled = {old: (other, canon) for other, table in LEGACY.items() for canon, old in table.items()}
	for canon, old in (legacy_codes or {}).items():
		if canon not in CORE:
			raise RegistryError(f"{app}: legacy keys must be core codes: {canon}")
		if old in known and known[old] != CORE[canon]:
			raise RegistryError(f"{app}: {old} is registered with {known[old]}, {canon} is {CORE[canon]}")
		if old in CORE:
			raise RegistryError(f"{app}: {old} is a core code")
		first = spelled.setdefault(old, (app, canon))
		if first[1] != canon:
			raise RegistryError(f"{old}: {first[0]} spells {first[1]} with it, {app} spells {canon}")
	for code, http_status in (codes or {}).items():
		_registered.setdefault(code, (app, http_status))
	if legacy_codes:
		LEGACY.setdefault(app, {}).update(legacy_codes)


class ApiError(Exception):
	"""Base for errors that render as the envelope. Every FrappeAPI app maps an
	ApiError (or subclass) to {code, status}; an app may subclass it with its own.
	http_status_code is the status Frappe's own handler uses for plain
	@frappe.whitelist callers."""

	code = "internal_error"
	status = 500
	http_status_code = 500

	def __init_subclass__(cls, **kwargs):
		super().__init_subclass__(**kwargs)
		cls.http_status_code = cls.status


class ServiceUnavailable(ApiError):
	code, status = "service_unavailable", 503


class RateLimited(ApiError):
	code, status = "rate_limited", 429


class IdempotencyConflict(ApiError):
	code, status = "idempotency_conflict", 409


class UpgradeRequired(ApiError):
	code, status = "upgrade_required", 426


def envelope(code: str, message: str, status: int, data=None) -> JSONResponse:
	return JSONResponse(
		content={"success": False, "message": message, "error_code": code, "data": data}, status_code=status
	)


def handle(request, exc: ApiError) -> JSONResponse:
	return envelope(exc.code, str(exc) or exc.code, exc.status)
