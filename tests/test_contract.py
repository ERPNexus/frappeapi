# tests/test_contract.py
import json

import frappe
import pytest
from conftest import release  # pytest puts tests/ on sys.path (no __init__.py)
from pydantic import BaseModel
from werkzeug.test import EnvironBuilder

from frappeapi import FrappeAPI, contract, errors
from frappeapi.responses import JSONResponse

REL = {"environment": "test", "clients": {"client-app": {"current": "2.4.0", "minimum": "2.3.0"}}}
GOOD = {"X-Client-App": "client-app", "X-Client-Version": "2.4.0", "X-Client-Contract": "2"}
OLD = {**GOOD, "X-Client-Version": "2.2.9"}


def _call(app, path, headers):
	route = next(r for r in app.router.routes if r.user_defined_fastapi_path_segment == path)
	frappe.local.request = EnvironBuilder(path="/api" + path, headers=headers).get_request()
	frappe.local.request.path_params = {}
	return route.handle_request()


def _app(name, contract_app):
	app = FrappeAPI(title=name, fastapi_path_format=True, contract_app=contract_app)

	@app.get(f"/{name}/v1/ok")
	def ok() -> dict:
		return {"a": 1}

	@app.get(f"/{name}/v1/conflict")
	def conflict() -> dict:
		raise errors.IdempotencyConflict("different body")

	@app.get(f"/{name}/v1/text")
	def text() -> dict:
		raise KeyError("unhandled")  # frappeapi's own {"detail": ...} 500

	return app


errors.register("legacy_app", legacy_codes={"idempotency_conflict": "old_conflict"})
legacy_app = _app("cm", "legacy_app")
plain = _app("cp", None)


@pytest.mark.parametrize(
	"headers",
	[
		{},
		{k: v for k, v in GOOD.items() if k != "X-Client-Contract"},
		{**GOOD, "X-Client-App": "unknown-app"},
		{**GOOD, "X-Client-Version": "2.4"},
		{**GOOD, "X-Client-Version": "2.4.123456"},
		{**GOOD, "X-Client-Contract": "3"},
	],
)
def test_unidentified_is_legacy_never_426(monkeypatch, headers):
	release(monkeypatch, **REL, enforce_min_versions=True, legacy_codes=False)
	assert contract.identify(headers, contract.release()) is None
	r = _call(legacy_app, "/cm/v1/conflict", headers)
	assert (r.status_code, r.get_json()["error_code"]) == (409, "old_conflict")


def test_matrix(monkeypatch):
	release(monkeypatch, **REL)
	assert contract.policy(contract.identify(GOOD, contract.release()), contract.release()) == "canonical"
	assert contract.policy(contract.identify(OLD, contract.release()), contract.release()) == "legacy"
	release(monkeypatch, **REL, legacy_codes=False)
	assert contract.policy(contract.identify(OLD, contract.release()), contract.release()) == "canonical"
	release(monkeypatch, **REL, enforce_min_versions=True)
	assert contract.policy(contract.identify(OLD, contract.release()), contract.release()) == "upgrade"
	assert contract.policy(contract.identify(GOOD, contract.release()), contract.release()) == "canonical"


def test_canonical_for_identified_client(monkeypatch):
	release(monkeypatch, **REL)
	r = _call(legacy_app, "/cm/v1/conflict", GOOD)
	assert (r.status_code, r.get_json()["error_code"]) == (409, "idempotency_conflict")
	assert r.headers["X-Server-Contract"] == "2"


def test_upgrade_required_only_when_enforced(monkeypatch):
	release(monkeypatch, **REL, enforce_min_versions=True)
	r = _call(legacy_app, "/cm/v1/ok", OLD)
	assert r.status_code == 426
	assert r.get_json()["error_code"] == "upgrade_required"
	assert r.get_json()["data"] == {"minimum_version": "2.3.0"}


def test_request_id_echoed_or_replaced(monkeypatch):
	release(monkeypatch, **REL)
	assert _call(legacy_app, "/cm/v1/ok", {"X-Request-ID": "abc.DEF-123_x"}).headers["X-Request-ID"] == "abc.DEF-123_x"
	for bad in ("short", "a" * 65, "<script>alert(1)</script>", "bad id with spaces"):
		rid = _call(legacy_app, "/cm/v1/ok", {"X-Request-ID": bad}).headers["X-Request-ID"]
		assert rid != bad
		assert len(rid) == 32


def test_opt_out_is_untouched(monkeypatch):
	release(monkeypatch, **REL)
	r = _call(plain, "/cp/v1/ok", GOOD)
	assert "X-Request-ID" not in r.headers
	assert "X-Server-Contract" not in r.headers
	assert r.get_data() == b'{"a": 1}'


def test_legacy_rewrite_ignores_non_json(monkeypatch):
	release(monkeypatch, **REL)
	r = _call(legacy_app, "/cm/v1/text", {})
	assert r.status_code == 500
	assert b"detail" in r.get_data()
	assert contract._to_legacy("legacy_app", JSONResponse(content=[1, 2], status_code=400)) is None


_ran = []


class _Body(BaseModel):
	x: int


@legacy_app.post("/cm/v1/post")
def _post(body: _Body) -> dict:
	_ran.append(1)
	return {}


@legacy_app.get("/cm/v1/side")
def _side() -> dict:
	_ran.append(1)
	return {}


def _post_call(path, data, headers=None):
	route = next(r for r in legacy_app.router.routes if r.user_defined_fastapi_path_segment == path)
	frappe.local.request = EnvironBuilder(
		path="/api" + path, method="POST", data=data, content_type="application/json", headers=headers or {}
	).get_request()
	frappe.local.request.path_params = {}
	return route.handle_request()


def _stamped(r):
	assert r.headers["X-Request-ID"]
	assert r.headers["X-Server-Contract"] == "2"


def test_body_parse_errors_carry_headers(monkeypatch):
	release(monkeypatch, **REL)
	r = _post_call("/cm/v1/post", b"{not json")
	assert r.status_code >= 400
	_stamped(r)
	r = _post_call("/cm/v1/post", b'{"x": "nope"}', GOOD)
	assert r.status_code == 422
	_stamped(r)


def test_escaped_exception_uses_registered_handler(monkeypatch):
	release(monkeypatch, **REL)
	from frappeapi.exceptions import HTTPException

	def boom():
		raise HTTPException(status_code=418, detail="x")

	r = contract.run(
		"legacy_app", boom, {HTTPException: lambda req, exc: JSONResponse(content={"h": 1}, status_code=401)}
	)
	assert r.status_code == 401
	_stamped(r)


def test_426_and_500_carry_headers_and_skip_endpoint(monkeypatch):
	release(monkeypatch, **REL, enforce_min_versions=True)
	_ran.clear()
	r = _call(legacy_app, "/cm/v1/side", OLD)
	assert r.status_code == 426
	assert not _ran
	_stamped(r)
	r = _call(legacy_app, "/cm/v1/text", {})
	assert r.status_code == 500
	_stamped(r)


def test_identified_below_minimum_defaults_to_legacy(monkeypatch):
	release(monkeypatch, **REL)
	r = _call(legacy_app, "/cm/v1/conflict", OLD)
	assert r.get_json()["error_code"] == "old_conflict"


@pytest.mark.parametrize("minimum", ["2.3", "2.3.x", "", None, "٢.٣.٠"])
def test_malformed_minimum_is_no_minimum(monkeypatch, minimum):
	release(
		monkeypatch,
		environment="test",
		enforce_min_versions=True,
		clients={"client-app": {"current": "2.4.0", "minimum": minimum}},
	)
	assert _call(legacy_app, "/cm/v1/ok", OLD).status_code == 200


def test_arabic_digits_not_identified():
	assert contract.identify({**GOOD, "X-Client-Version": "٢.٤.٠"}, REL) is None


def test_non_string_error_code_untouched():
	r = JSONResponse(content={"error_code": ["a"]}, status_code=400)
	assert contract._to_legacy("legacy_app", r) is None


def test_current_request_id_set_during_run(monkeypatch):
	release(monkeypatch, **REL)
	seen = []
	frappe.local.request = EnvironBuilder(headers={"X-Request-ID": "abcdefgh1"}).get_request()
	contract.run("legacy_app", lambda: (seen.append(contract.current_request_id()), JSONResponse(content={}))[1])
	assert seen == ["abcdefgh1"]


# --- ApiError on every app (I2) ---


class TenantSuspended(errors.ApiError):
	code, status = "tenant_suspended", 403


@legacy_app.get("/cm/v1/suspended")
def _suspended() -> dict:
	raise TenantSuspended("This tenant is suspended")


@legacy_app.get("/cm/v1/upgrade")
def _upgrade() -> dict:
	raise errors.UpgradeRequired("Please update the app")


def test_app_defined_api_error_maps_to_its_status(monkeypatch):
	release(monkeypatch, **REL)
	r = _call(legacy_app, "/cm/v1/suspended", GOOD)
	assert r.status_code == 403
	assert r.get_json() == {
		"success": False,
		"message": "This tenant is suspended",
		"error_code": "tenant_suspended",
		"data": None,
	}
	_stamped(r)
	r = _call(legacy_app, "/cm/v1/upgrade", {})
	assert (r.status_code, r.get_json()["error_code"]) == (426, "upgrade_required")
	_stamped(r)


opt_out = FrappeAPI(title="co", fastapi_path_format=True)


class _Q(BaseModel):
	x: int


@opt_out.get("/co/v1/down")
def _down() -> dict:
	raise errors.ServiceUnavailable("SMS is down")


@opt_out.get("/co/v1/http")
def _http() -> dict:
	from frappeapi.exceptions import HTTPException

	raise HTTPException(status_code=404, detail="nope")


@opt_out.get("/co/v1/key")
def _key() -> dict:
	raise KeyError("x")


@opt_out.get("/co/v1/q")
def _q(n: int) -> dict:
	return {}


@opt_out.post("/co/v1/post")
def _opt_post(body: _Q) -> dict:
	return {}


def _opt_call(path, method="GET", data=None, qs=None):
	route = next(r for r in opt_out.router.routes if r.user_defined_fastapi_path_segment == path)
	frappe.local.request = EnvironBuilder(
		path="/api" + path, method=method, data=data, query_string=qs, content_type="application/json" if data else None
	).get_request()
	frappe.local.request.path_params = {}
	r = route.handle_request()
	return r.status_code, sorted(r.headers.items()), r.get_data()


def test_opt_out_api_error_is_its_envelope(monkeypatch):
	release(monkeypatch, **REL)
	status, headers, body = _opt_call("/co/v1/down")
	assert status == 503
	assert json.loads(body) == {
		"success": False,
		"message": "SMS is down",
		"error_code": "service_unavailable",
		"data": None,
	}
	assert not {"X-Request-ID", "X-Server-Contract"} & {k for k, _v in headers}


def _json_headers(body):
	return [("Content-Length", str(len(body))), ("Content-Type", "application/json")]


_INT = "Input should be a valid integer, unable to parse string as an integer"
# Captured from the opt-out path before the ApiError handler was added (v0.5.0 final fix).
_BEFORE = [
	(("/co/v1/http",), 404, b'{"detail": "nope"}'),
	(("/co/v1/key",), 500, b'{"detail": "KeyError(\'x\')"}'),
	(
		("/co/v1/q", "GET", None, "n=abc"),
		422,
		(
			'{"detail": [{"type": "int_parsing", "loc": ["query", "n"], "msg": "' + _INT + '", "input": "abc"}]}'
		).encode(),
	),
	(
		("/co/v1/post", "POST", b'{"x":"no"}'),
		422,
		('{"detail": [{"type": "int_parsing", "loc": ["body", "x"], "msg": "' + _INT + '", "input": "no"}]}').encode(),
	),
	(
		("/co/v1/post", "POST", b"{bad"),
		422,
		b'{"detail": [{"type": "missing", "loc": ["body"], "msg": "Field required", "input": null}]}',
	),
]


@pytest.mark.parametrize(("args", "status", "body"), _BEFORE)
def test_opt_out_other_responses_byte_identical(monkeypatch, args, status, body):
	release(monkeypatch, **REL)
	assert _opt_call(*args) == (status, _json_headers(body), body)


# --- malformed frappeapi_release is treated as absent (Minor 2) ---


@pytest.mark.parametrize(
	"rel",
	[
		[1, 2],
		"release",
		7,
		{"clients": ["client-app"]},
		{"clients": "client-app"},
		{"clients": {"client-app": ["2.3.0"]}, "enforce_min_versions": True},
		{"clients": {"client-app": "2.3.0"}, "enforce_min_versions": True},
	],
)
def test_malformed_release_is_absent_and_logged(monkeypatch, rel):
	monkeypatch.setitem(frappe.local.conf, "frappeapi_release", rel)
	logged = []
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: logged.append((a, k)))
	monkeypatch.setattr(contract, "_logged", set())
	for _ in range(2):
		r = _call(legacy_app, "/cm/v1/conflict", OLD)
		assert (r.status_code, r.get_json()["error_code"]) == (409, "old_conflict")  # unidentified: legacy
		_stamped(r)
		assert _call(legacy_app, "/cm/v1/ok", OLD).status_code == 200  # never a 426
	assert contract.identify(OLD, contract.release()) is None
	assert len(logged) == 1  # once per process, not per request


def test_log_failure_never_breaks_the_request(monkeypatch):
	monkeypatch.setitem(frappe.local.conf, "frappeapi_release", [1])
	monkeypatch.setattr(contract, "_logged", set())

	def broken(*a, **k):
		raise RuntimeError("log table locked")

	monkeypatch.setattr(frappe, "log_error", broken)
	assert _call(legacy_app, "/cm/v1/ok", GOOD).status_code == 200


@pytest.mark.parametrize("rel", [[1], "release", 7])
def test_state_key_survives_malformed_release(monkeypatch, rel):
	from frappeapi import state

	monkeypatch.setitem(frappe.local.conf, "frappeapi_release", rel)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None)
	assert state.key("otp:test", "+201099900010").startswith(f"{frappe.local.site}:default:otp:test:")


class _Base(Exception):
	pass


def _mro_app(name, contract_app):
	app = FrappeAPI(title=name, fastapi_path_format=True, contract_app=contract_app)
	app.exception_handler(_Base)(lambda req, exc: JSONResponse(content={"base": str(exc)}, status_code=400))

	@app.get(f"/{name}/v1/late")
	def late() -> dict:
		class Late(_Base):  # defined after the handlers were registered, like a lazily imported module
			pass

		raise Late("late class")

	return app


mro_contract = _mro_app("mc", "legacy_app")
mro_plain = _mro_app("mp", None)


def test_contract_app_resolves_a_subclass_by_mro():
	r = _call(mro_contract, "/mc/v1/late", GOOD)
	assert (r.status_code, r.get_json()) == (400, {"base": "late class"})


def test_opt_out_app_keeps_exact_type_lookup():
	r = _call(mro_plain, "/mp/v1/late", {})
	assert r.status_code == 500
	assert r.get_json() == {"detail": "Late('late class')"}
