# tests/test_errors.py
import pytest

from frappeapi import errors


def test_registry_is_core_only():
	assert set(errors.REGISTRY) == {"contract", "core"}
	assert errors.status("service_unavailable") == 503


def test_internal_error_is_a_core_code():
	# ApiError's default code: an unregistered default would make status() raise KeyError.
	assert errors.status("internal_error") == 500
	assert errors.status(errors.ApiError.code) == errors.ApiError.status


def test_legacy_rewrite_per_app():
	errors.register("app_a", {"old_conflict": 409}, {"idempotency_conflict": "old_conflict"})
	errors.register("app_b", legacy_codes={"unauthenticated": "old_auth"})
	assert errors.legacy("app_a", "idempotency_conflict") == "old_conflict"
	assert errors.legacy("app_b", "unauthenticated") == "old_auth"
	assert errors.legacy("app_b", "idempotency_conflict") == "idempotency_conflict"
	assert errors.legacy("unregistered", "not_found") == "not_found"
	assert errors.status("old_auth") == 401  # the old spelling keeps the core status


def test_register_refuses_core_bad_legacy_and_status_clash():
	with pytest.raises(errors.RegistryError):
		errors.register("app_c", {"not_found": 404})
	with pytest.raises(errors.RegistryError):
		errors.register("app_c", legacy_codes={"made_up": "x"})
	with pytest.raises(errors.RegistryError):
		errors.register("app_c", legacy_codes={"not_found": "permission_denied"})
	with pytest.raises(errors.RegistryError):  # spelling 409 as a 400 code in the same call
		errors.register("app_c", {"odd": 400}, {"idempotency_conflict": "odd"})
	errors.register("app_c", {"shared_code": 409})
	errors.register("app_d", {"shared_code": 409})  # shared code, same status: fine
	with pytest.raises(errors.RegistryError):
		errors.register("app_d", {"shared_code": 400})
	with pytest.raises(errors.RegistryError):  # old_auth is app_b's 401 spelling
		errors.register("app_d", {"old_auth": 400})
	with pytest.raises(KeyError):
		errors.status("never_registered")


def test_envelope_shape():
	r = errors.handle(None, errors.ServiceUnavailable("SMS is down"))
	assert r.status_code == 503
	assert r.get_json() == {
		"success": False,
		"message": "SMS is down",
		"error_code": "service_unavailable",
		"data": None,
	}


def test_one_legacy_spelling_cannot_name_two_core_codes():
	errors.register("app_e", legacy_codes={"not_found": "gone_e"})
	with pytest.raises(errors.RegistryError):  # across apps
		errors.register("app_f", legacy_codes={"unauthenticated": "gone_e"})
	with pytest.raises(errors.RegistryError):  # the same app, a later call
		errors.register("app_e", legacy_codes={"unauthenticated": "gone_e"})
	with pytest.raises(errors.RegistryError):  # within one call
		errors.register("app_g", legacy_codes={"not_found": "gone_g", "unauthenticated": "gone_g"})
	assert "app_g" not in errors.LEGACY
	assert errors.status("gone_e") == 404
	errors.register("app_h", legacy_codes={"not_found": "gone_e"})  # same spelling, same core code: fine
	assert errors.legacy("app_h", "not_found") == "gone_e"


def test_api_error_status_for_plain_whitelist_callers():
	class TenantSuspended(errors.ApiError):
		code, status = "tenant_suspended", 403

	assert errors.ApiError.http_status_code == 500
	assert errors.ServiceUnavailable.http_status_code == 503
	assert errors.RateLimited.http_status_code == 429
	assert TenantSuspended.http_status_code == 403


def test_contract_number_has_one_source():
	from frappeapi import contract

	assert errors.REGISTRY["contract"] == contract.CONTRACT
