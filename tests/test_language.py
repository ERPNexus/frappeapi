# tests/test_language.py
"""Accept-Language sets frappe.local.lang for FrappeAPI routes, logged-in requests included."""

import json

import frappe
import pytest
from conftest import release  # pytest puts tests/ on sys.path (no __init__.py)
from frappe import _
from frappe.core.doctype.translation.translation import clear_user_translation_cache
from werkzeug.test import EnvironBuilder

from frappeapi import FrappeAPI, errors
from frappeapi.exceptions import RequestValidationError
from frappeapi.responses import JSONResponse

# Self-contained: the Arabic comes from Translation docs this file inserts (and rolls back),
# not from any app's translations/ar.csv.
MSG = "frappeapi language test: too many requests"
AR = "اختبار لغة frappeapi: طلبات كثيرة"
UPDATE = "Please update the app"  # contract.py's 426 message
AR_UPDATE = "اختبار frappeapi: يرجى تحديث التطبيق"
REL = {"environment": "test", "clients": {"client-app": {"current": "2.4.0", "minimum": "2.3.0"}}}
OLD = {"X-Client-App": "client-app", "X-Client-Version": "2.2.9", "X-Client-Contract": "2"}

def _app(name, contract_app):
	app = FrappeAPI(title=name, fastapi_path_format=True, contract_app=contract_app)
	# An app's own validation envelope (frappeapi's built-in one is pydantic's English).
	app.exception_handler(RequestValidationError)(
		lambda req, exc: JSONResponse(content={"message": _(MSG)}, status_code=422)
	)

	@app.get(f"/{name}/v1/limited")
	def limited() -> dict:
		raise errors.RateLimited(_(MSG))

	@app.get(f"/{name}/v1/echo")
	def echo() -> dict:
		return {"lang": frappe.local.lang, "message": _(MSG)}

	@app.get(f"/{name}/v1/vary")
	def vary() -> JSONResponse:
		return JSONResponse(content={}, headers={"Vary": "Origin"})

	@app.get(f"/{name}/v1/typed")
	def typed(n: int) -> dict:
		return {"n": n}

	return app


app = _app("lang", None)
contract = _app("langc", "lang_contract")  # merchant/modar are contract apps; mandoob is not


def _raw(path, headers=None, query_string=None, api=app):
	route = next(r for r in api.router.routes if r.user_defined_fastapi_path_segment == path)
	frappe.local.request = EnvironBuilder(
		path="/api" + path, headers=headers or {}, query_string=query_string
	).get_request()
	frappe.local.request.path_params = {}
	return route.handle_request()


def _call(path, headers=None, query_string=None, api=app):
	return json.loads(_raw(path, headers, query_string, api).get_data())


@pytest.fixture(autouse=True)
def logged_in_en():
	# What Frappe picks for a session whose User.language is en: Accept-Language is ignored.
	lang, form_dict = frappe.local.lang, frappe.local.form_dict
	frappe.set_user("Administrator")
	frappe.local.lang = "en"
	frappe.local.form_dict = frappe._dict()
	for source, translated in [(MSG, AR), (UPDATE, AR_UPDATE)]:
		# on_update drops merged_translations["ar"], so the next _() rebuilds it with these rows
		frappe.get_doc(
			doctype="Translation", language="ar", source_text=source, translated_text=translated
		).insert(ignore_permissions=True)
	yield
	frappe.local.lang, frappe.local.form_dict = lang, form_dict
	frappe.set_user("Administrator")
	frappe.db.rollback()
	clear_user_translation_cache("ar")  # the rollback does not touch the Redis-cached merge


@pytest.mark.parametrize("name", ["lang", "langc"])
def test_logged_in_user_gets_header_language_in_route_and_error_envelope(name):
	api = {"lang": app, "langc": contract}[name]
	assert frappe.session.user != "Guest"
	body = _call(f"/{name}/v1/limited", {"Accept-Language": "ar"}, api=api)
	assert body["message"] == AR  # error_code may be a legacy spelling for an unidentified contract client
	assert _call(f"/{name}/v1/echo", {"Accept-Language": "ar-EG"}, api=api) == {"lang": "ar", "message": AR}
	assert _call(f"/{name}/v1/typed", {"Accept-Language": "ar"}, "n=abc", api=api) == {"message": AR}


def test_no_header_leaves_frappe_choice():
	assert _call("/lang/v1/echo") == {"lang": "en", "message": MSG}


@pytest.mark.parametrize("header", ["xx-YY", "garbage;;;q=x", "*", "ar;q=0", "", "ar;q=abc"])
def test_unsupported_or_garbage_header_is_ignored(header):
	assert _call("/lang/v1/echo", {"Accept-Language": header})["lang"] == "en"


@pytest.mark.parametrize(
	("header", "lang"),
	[
		("en;q=0.1, ar", "ar"),
		("ar;q=0.5, en;q=0.9", "en"),
		("fr;q=0, ar-EG;q=0.8, *", "ar"),  # q=0 means "not acceptable"; '*' is skipped
		("xx;q=1, AR;q=0.2", "ar"),  # first enabled one wins; case-insensitive
	],
)
def test_q_values_are_respected(header, lang):
	assert _call("/lang/v1/echo", {"Accept-Language": header})["lang"] == lang


def test_switching_languages_between_requests():
	# Translations are cached per language (Redis hash merged_translations[lang]): no bleed.
	for header, msg in [("ar", AR), ("en", MSG), ("ar", AR), ("en", MSG)]:
		assert _call("/lang/v1/limited", {"Accept-Language": header})["message"] == msg


def test_explicit_lang_param_still_wins():
	frappe.local.form_dict = frappe._dict(_lang="en")
	assert _call("/lang/v1/echo", {"Accept-Language": "ar"})["lang"] == "en"


@pytest.mark.parametrize(("name", "api"), [("lang", app), ("langc", contract)])
def test_every_response_varies_on_accept_language(name, api):
	# Success, ApiError envelope and validation handler, with or without the header.
	for path, qs in [("echo", None), ("limited", None), ("typed", "n=abc")]:
		for headers in ({}, {"Accept-Language": "ar"}):
			assert _raw(f"/{name}/v1/{path}", headers, qs, api).headers["Vary"] == "Accept-Language"
	# An existing Vary is appended to, not overwritten.
	assert _raw(f"/{name}/v1/vary", {}, None, api).headers["Vary"] == "Origin, Accept-Language"


def test_contract_426_is_translated_and_varies(monkeypatch):
	release(monkeypatch, **REL, enforce_min_versions=True)
	r = _raw("/langc/v1/echo", {**OLD, "Accept-Language": "ar"}, api=contract)
	assert r.status_code == 426
	assert json.loads(r.get_data())["message"] == AR_UPDATE
	assert r.headers["Vary"] == "Accept-Language"
	frappe.local.lang = "en"  # a new request starts from Frappe's choice again
	assert json.loads(_raw("/langc/v1/echo", OLD, api=contract).get_data())["message"] == UPDATE


def test_language_lookup_failure_never_raises(monkeypatch):
	def boom():
		raise RuntimeError("cold cache, DB down")

	monkeypatch.setattr("frappe.translate.get_all_languages", boom)
	r = _raw("/lang/v1/echo", {"Accept-Language": "ar"})
	assert r.status_code == 200
	assert json.loads(r.get_data()) == {"lang": "en", "message": MSG}
	assert r.headers["Vary"] == "Accept-Language"


def test_rejected_lang_param_still_blocks_the_header():
	frappe.local.form_dict = frappe._dict(_lang="xx")  # Frappe rejects it; documented trade-off
	assert _call("/lang/v1/echo", {"Accept-Language": "ar"})["lang"] == "en"


def test_guest_header_beats_preferred_language_cookie():
	frappe.set_user("Guest")
	headers = {"Accept-Language": "ar", "Cookie": "preferred_language=en"}
	assert _call("/lang/v1/echo", headers) == {"lang": "ar", "message": AR}
