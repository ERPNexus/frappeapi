"""Client contract and request IDs (integration spec §6.2, §6.3).

Only apps constructed with FrappeAPI(contract_app=...) pass through run().
The policy depends on frappe.conf.frappeapi_release; with no release config,
every client is unidentified and gets legacy codes."""

import contextlib
import json
import re
import uuid

import frappe

from frappeapi import errors
from frappeapi.exception_handler import http_exception_handler, request_validation_exception_handler
from frappeapi.exceptions import HTTPException, RequestValidationError

CONTRACT = errors.REGISTRY["contract"]
_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


_logged: set[str] = set()  # malformed-config reasons already logged by this process


def _malformed(reason: str) -> None:
	"""Log a misconfigured frappeapi_release once per process; never fail the request."""
	if reason in _logged:
		return
	_logged.add(reason)
	with contextlib.suppress(Exception):  # logging must not turn a config typo into a 500
		frappe.log_error(title="frappeapi_release is malformed", message=reason)


def release() -> dict:
	"""frappe.conf.frappeapi_release; a malformed value is treated as absent."""
	rel = frappe.conf.get("frappeapi_release") or {}
	if not isinstance(rel, dict):
		_malformed(f"frappeapi_release must be a dict, got {type(rel).__name__}")
		return {}
	return rel


def _version(text: str) -> tuple[int, int, int]:
	return tuple(int(p) for p in text.split("."))


def request_id(headers) -> str:
	"""The client's id when it is safe to log, else a fresh one. A rejected value
	is never logged (log injection)."""
	rid = headers.get("X-Request-ID") or ""
	return rid if _REQUEST_ID.fullmatch(rid) else uuid.uuid4().hex


def current_request_id() -> str | None:
	return getattr(frappe.local, "request_id", None)


def identify(headers, rel: dict):
	app = headers.get("X-Client-App")
	version = headers.get("X-Client-Version") or ""
	if headers.get("X-Client-Contract") != str(CONTRACT) or not _VERSION.fullmatch(version):
		return None
	clients = rel.get("clients") or {}
	if not isinstance(clients, dict):
		_malformed(f"frappeapi_release.clients must be a dict, got {type(clients).__name__}")
		return None
	if app not in clients:
		return None
	if clients[app] is not None and not isinstance(clients[app], dict):
		_malformed(f"frappeapi_release.clients.{app} must be a dict, got {type(clients[app]).__name__}")
		return None
	return app, _version(version)


def policy(client, rel: dict) -> str:
	if client is None:
		return "legacy"
	minimum = (rel["clients"][client[0]] or {}).get("minimum")
	if not isinstance(minimum, str) or not _VERSION.fullmatch(minimum):
		return "canonical"  # a malformed minimum is no minimum
	if client[1] < _version(minimum):
		if rel.get("enforce_min_versions"):
			return "upgrade"
		return "legacy" if rel.get("legacy_codes", True) else "canonical"
	return "canonical"


def _to_legacy(app: str, response) -> None:
	if response.mimetype != "application/json":
		return None
	try:
		body = json.loads(response.get_data())
	except ValueError:
		return None
	code = body.get("error_code") if isinstance(body, dict) else None
	if isinstance(code, str) and code and errors.legacy(app, code) != code:
		body["error_code"] = errors.legacy(app, code)
		response.set_data(json.dumps(body))
	return None


def run(app: str, call, handlers=None):
	headers = frappe.request.headers
	rid = frappe.local.request_id = request_id(headers)
	rel = release()
	client = identify(headers, rel)
	mode = policy(client, rel)
	if mode == "upgrade":
		minimum = rel["clients"][client[0]]["minimum"]
		response = errors.envelope("upgrade_required", "Please update the app", 426, {"minimum_version": minimum})
	else:
		try:
			response = call()
		except (HTTPException, RequestValidationError) as exc:
			# raised outside the route's handled try (body parsing); render it here
			# so it still carries the contract headers
			fn = (handlers or {}).get(type(exc))
			if fn is None:
				fn = http_exception_handler if isinstance(exc, HTTPException) else request_validation_exception_handler
			response = fn(frappe.request, exc)
		if mode == "legacy" and response.status_code >= 400:
			_to_legacy(app, response)
	response.headers["X-Request-ID"] = rid
	response.headers["X-Server-Contract"] = str(CONTRACT)
	return response
