# tests/test_otp.py
from unittest.mock import patch

import pytest
from conftest import redis_up  # pytest puts tests/ on sys.path (no __init__.py)
from redis.exceptions import ConnectionError as RedisDown

from frappeapi import errors, otp, state

if not redis_up():
	pytest.skip("Redis is unreachable (spec §8: no fake Redis)", allow_module_level=True)

MOBILE = "+201099900010"


@pytest.fixture(autouse=True)
def clean():
	yield
	state.redis().delete(state.key("otp:test", MOBILE))


def _issue() -> str:
	sent = []
	otp.issue(MOBILE, "test", sent.append)
	return sent[0]


def test_round_trip_single_use_and_hashed():
	code = _issue()
	assert len(code) == 6
	assert code.isdigit()
	assert code.encode() not in b"".join(state.redis().hgetall(state.key("otp:test", MOBILE)).values())
	assert otp.verify(MOBILE, "test", code)
	assert not otp.verify(MOBILE, "test", code)


def test_attempts_exhaust_the_code():
	code = _issue()
	for _ in range(5):
		assert not otp.verify(MOBILE, "test", "999999" if code != "999999" else "000000")
	assert not otp.verify(MOBILE, "test", code)


def test_reissue_replaces_and_counts_sends():
	first = _issue()
	second = _issue()
	assert int(state.redis().hget(state.key("otp:test", MOBILE), "sent")) == 2
	if first != second:
		assert not otp.verify(MOBILE, "test", first)
	assert otp.verify(MOBILE, "test", second)


def test_ttl_is_set():
	_issue()
	assert 0 < state.redis().ttl(state.key("otp:test", MOBILE)) <= 300


def test_send_failure_is_503_and_leaves_no_code():
	def boom(code):
		raise RuntimeError("gateway down")

	with pytest.raises(errors.ServiceUnavailable):
		otp.issue(MOBILE, "test", boom)
	assert not state.redis().exists(state.key("otp:test", MOBILE))


def test_redis_down_fails_closed():
	with patch.object(state, "redis", side_effect=RedisDown("down")):
		with pytest.raises(errors.ServiceUnavailable):
			otp.issue(MOBILE, "test", lambda c: None)
		with pytest.raises(errors.ServiceUnavailable):
			otp.verify(MOBILE, "test", "123456")


def test_key_uses_normalized_mobile(monkeypatch):
	import frappe

	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", "20")
	sent = []
	otp.issue("٠١٠ 9990-0010", "test", sent.append)
	assert otp.verify("+201099900010", "test", sent[0])


def test_failed_send_keeps_a_concurrent_issues_code():
	sent = []

	def racing(code):
		otp.issue(MOBILE, "test", sent.append)  # another request issues meanwhile
		raise RuntimeError("gateway down")

	with pytest.raises(errors.ServiceUnavailable):
		otp.issue(MOBILE, "test", racing)
	assert otp.verify(MOBILE, "test", sent[0])


def test_send_failure_does_not_chain_the_gateway_error():
	def boom(code):
		raise RuntimeError(f"bad sms body {code}")

	with pytest.raises(errors.ServiceUnavailable) as ei:
		otp.issue(MOBILE, "test", boom)
	assert ei.value.__cause__ is None
	assert ei.value.__suppress_context__


@pytest.mark.parametrize("bad", [None, 123456, b"123456", ["1"]])
def test_non_string_code_is_a_wrong_code(bad):
	code = _issue()
	assert not otp.verify(MOBILE, "test", bad)
	assert otp.verify(MOBILE, "test", code)


def test_country_code_threads_through(monkeypatch):
	import frappe

	from frappeapi import identity

	monkeypatch.delitem(frappe.local.conf, "frappeapi_default_country_code", raising=False)
	monkeypatch.setattr(identity, "_site_country_code", lambda: None)
	local = "0" + MOBILE[3:]
	with pytest.raises(ValueError, match="international format"):
		otp.issue(local, "test", lambda c: None)
	sent = []
	otp.issue(local, "test", sent.append, default_country_code="20")
	assert otp.verify(local, "test", sent[0], default_country_code="20")
