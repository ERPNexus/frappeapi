import random
from unittest.mock import patch

import frappe
import pytest
from conftest import redis_up  # pytest puts tests/ on sys.path (no __init__.py)
from redis.exceptions import ConnectionError as RedisDown

from frappeapi import errors, identity, state

if not redis_up():
	pytest.skip("Redis is unreachable (spec §8: no fake Redis)", allow_module_level=True)


@pytest.fixture(autouse=True)
def egypt(monkeypatch):
	"""These tests use Egyptian numbers; the site default is deployment config."""
	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", "20")


def _free_block():
	"""A random unused block inside +2010999xxxxx so concurrent runs don't contend."""
	while True:
		n = random.randint(1000, 9999)
		ms = [f"+2010999{n}{i}" for i in range(8)]
		if not frappe.db.exists("User", {"mobile_no": ["like", f"+2010999{n}%"]}) and not any(
			state.redis().exists(state.key("lock:user", m)) for m in ms
		):
			return n


N = _free_block()
M = [f"+2010999{N}{i}" for i in range(8)]
LOCAL = ["0" + m[3:] for m in M]  # 010999NNNNi
MOBILE = M[1]
RUN = f"{N}"


def _user(email, mobile, enabled=1):
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "T",
			"mobile_no": mobile,
			"enabled": enabled,
			"send_welcome_email": 0,
		}
	).insert(ignore_permissions=True)


def _forms():
	loc = LOCAL[1]
	ar = loc.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
	fw = loc.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
	dv = loc.translate(str.maketrans("0123456789", "०१२३४५६७८९"))
	spaced = f"({loc[:3]}) {loc[3:7]}-{loc[7:]}"
	return [loc, f"+20 {loc[1:4]} {loc[4:7]} {loc[7:]}", "00" + MOBILE[1:], MOBILE[1:], ar, fw, dv, spaced]


@pytest.mark.parametrize("i", range(8))
def test_normalize(i):
	raw = _forms()[i]
	out = identity.normalize_mobile(raw)
	assert out == MOBILE
	assert out.isascii()


@pytest.mark.parametrize("raw", ["", "12345", "010-ABC-0001", "+20+1099900001", "010999000011234567"])
def test_normalize_rejects(raw):
	with pytest.raises(ValueError, match="invalid mobile number|required"):
		identity.normalize_mobile(raw)


def test_country_code_from_conf(monkeypatch):
	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", "966")
	assert identity.normalize_mobile("0501234567") == "+966501234567"
	assert identity.normalize_mobile("501234567") == "+966501234567"
	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", 20)  # an int in site_config.json
	assert identity.normalize_mobile("01099900001") == "+201099900001"


def test_explicit_country_code_wins_over_conf():
	assert identity.normalize_mobile("0501234567", default_country_code="966") == "+966501234567"
	assert identity.normalize_mobile("0501234567", "+966") == "+966501234567"


@pytest.mark.parametrize("raw", ["01099900001", "1099900001", "201099900001", "0501234567"])
def test_local_forms_rejected_without_country_code(monkeypatch, raw):
	monkeypatch.delitem(frappe.local.conf, "frappeapi_default_country_code", raising=False)
	monkeypatch.setattr(identity, "_site_country_code", lambda: None)
	with pytest.raises(ValueError, match="invalid mobile number|required"):
		identity.normalize_mobile(raw)
	assert identity.user_by_mobile(raw) is None


@pytest.mark.parametrize(
	("raw", "out"),
	[
		("+20 109 990 0001", "+201099900001"),
		("00201099900001", "+201099900001"),
		("+966501234567", "+966501234567"),
		("00966501234567", "+966501234567"),
		("+١٢٣٤٥٦٧٨٩٠١", "+12345678901"),
	],
)
def test_international_always_accepted(monkeypatch, raw, out):
	monkeypatch.delitem(frappe.local.conf, "frappeapi_default_country_code", raising=False)
	assert identity.normalize_mobile(raw) == out
	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", "966")
	assert identity.normalize_mobile(raw) == out


@pytest.mark.parametrize("cc", ["abc", "1234", "", "+"])
def test_malformed_country_code_refused(cc):
	with pytest.raises(ValueError, match="invalid default country code"):
		identity.normalize_mobile("0501234567", default_country_code=cc)


def test_country_code_falls_back_to_the_site_country(monkeypatch):
	monkeypatch.delitem(frappe.local.conf, "frappeapi_default_country_code", raising=False)
	monkeypatch.setattr(frappe.db, "get_single_value", lambda dt, f: "Saudi Arabia")
	assert identity.normalize_mobile("0501234567") == "+966501234567"
	monkeypatch.setitem(frappe.local.conf, "frappeapi_default_country_code", "20")  # conf overrides the site country
	assert identity.normalize_mobile("01099900001") == "+201099900001"


def test_country_code_threads_through_callers(monkeypatch):
	monkeypatch.delitem(frappe.local.conf, "frappeapi_default_country_code", raising=False)
	monkeypatch.setattr(identity, "_site_country_code", lambda: None)
	_user(f"fa-cc-{RUN}@example.com", M[4])
	assert identity.user_by_mobile(LOCAL[4]) is None
	assert identity.user_by_mobile(LOCAL[4], default_country_code="20") == f"fa-cc-{RUN}@example.com"
	with pytest.raises(frappe.DuplicateEntryError):
		identity.assert_unique_mobile("someone-else@example.com", LOCAL[4], default_country_code="20")
	identity.lock_mobile(LOCAL[5], default_country_code="20")
	assert state.redis().exists(state.key("lock:user", M[5]))
	assert identity.find_or_create_user(LOCAL[4], "x@example.com", "X", [], default_country_code="20") == (
		f"fa-cc-{RUN}@example.com",
		False,
	)


def test_lookup_and_uniqueness():
	_user(f"fa-id-{RUN}@example.com", MOBILE)
	assert identity.user_by_mobile(LOCAL[1]) == f"fa-id-{RUN}@example.com"
	assert identity.user_by_mobile("not a number") is None
	identity.assert_unique_mobile(f"fa-id-{RUN}@example.com", MOBILE)  # itself: fine
	with pytest.raises(frappe.DuplicateEntryError):
		identity.assert_unique_mobile("someone-else@example.com", LOCAL[1])


def test_find_or_create_keeps_existing_and_adds_roles():
	user, created = identity.find_or_create_user(LOCAL[2], f"fa-new-{RUN}@example.com", "New", ["Blogger"])
	assert created
	assert frappe.db.get_value("User", user, "mobile_no") == M[2]
	again, created = identity.find_or_create_user(M[2], f"other-{RUN}@example.com", "X", ["Blogger", "Website Manager"])
	assert (again, created) == (user, False)
	assert {"Blogger", "Website Manager"} <= set(frappe.get_roles(user))


def test_disabled_holder_refused():
	_user(f"fa-off-{RUN}@example.com", M[3], enabled=0)
	with pytest.raises(frappe.ValidationError, match="disabled"):
		identity.find_or_create_user(LOCAL[3], f"fa-new3-{RUN}@example.com", "N", [])


def test_lock_is_reentrant_exclusive_and_released_by_rollback():
	identity.lock_mobile(MOBILE)
	identity.lock_mobile(MOBILE)  # same transaction: no wait
	held = frappe.local._frappeapi_mobile_locks
	frappe.local._frappeapi_mobile_locks = {}  # pretend to be another request
	with pytest.raises(errors.ServiceUnavailable):
		identity.lock_mobile(MOBILE, wait=0.2)
	frappe.local._frappeapi_mobile_locks = held
	frappe.db.rollback()  # runs after_rollback -> release
	assert not state.redis().exists(state.key("lock:user", MOBILE))


def test_redis_down_fails_closed():
	with patch.object(state, "redis", side_effect=RedisDown("down")), pytest.raises(errors.ServiceUnavailable):
		identity.lock_mobile(M[4])


def test_release_survives_redis_down():
	identity.lock_mobile(M[5])
	with patch.object(state, "redis", side_effect=RedisDown("down")):
		frappe.db.rollback()  # must not raise; PX expiry frees the key
	state.redis().delete(state.key("lock:user", M[5]))


def test_lock_normalizes_so_forms_share_a_key():
	identity.lock_mobile(LOCAL[1])
	identity.lock_mobile(MOBILE)  # same key: re-entrant, no wait
	assert len(frappe.local._frappeapi_mobile_locks) == 1


def test_lock_reacquired_when_expired():
	identity.lock_mobile(MOBILE)
	state.redis().delete(state.key("lock:user", MOBILE))  # simulate the 15s expiry
	identity.lock_mobile(MOBILE)
	assert state.redis().exists(state.key("lock:user", MOBILE))


def test_existing_user_roles_added_as_guest():
	user, _c = identity.find_or_create_user(LOCAL[2], f"fa-g-{RUN}@example.com", "G", [])
	frappe.set_user("Guest")
	try:
		assert identity.find_or_create_user(M[2], "x@example.com", "G", ["Blogger"]) == (user, False)
	finally:
		frappe.set_user("Administrator")
	assert "Blogger" in frappe.get_roles(user)


def test_find_sees_user_committed_after_snapshot():
	# locking read: a holder row is found even if the plain snapshot would miss it
	_user(f"fa-snap-{RUN}@example.com", M[0])
	assert identity.find_or_create_user(LOCAL[0], "n@example.com", "N", []) == (f"fa-snap-{RUN}@example.com", False)


def test_check_password_needs_login_manager():
	frappe.local.login_manager = None
	with pytest.raises(frappe.AuthenticationError):
		identity.check_password("Administrator", "x")


def test_state_refuses_without_redis_config(monkeypatch):
	monkeypatch.setitem(frappe.local.conf, "frappeapi_redis", None)
	monkeypatch.setitem(frappe.local.conf, "redis_queue", None)
	with pytest.raises(errors.ServiceUnavailable):
		state.redis()


_INSERT = (
	"insert into `tabUser` (name, email, first_name, mobile_no, enabled, user_type, creation, modified, owner,"
	" modified_by) values (%s, %s, 'T', %s, 1, 'Website User', now(), now(), 'Administrator', 'Administrator')"
)


def _second_conn():
	from frappe.database import get_db

	return get_db(
		socket=frappe.conf.db_socket,
		host=frappe.conf.db_host,
		port=frappe.conf.db_port,
		user=frappe.conf.get("db_user") or frappe.conf.db_name,
		password=frappe.conf.db_password,
		cur_db_name=frappe.conf.db_name,
	)


def _insert_on_second_conn(email, mobile):
	conn = _second_conn()
	conn.sql(
		_INSERT,
		(email, email, mobile),
	)
	conn.commit()
	return conn


def _drop_on_second_conn(conn, email):
	frappe.db.rollback()  # release the row lock find_or_create_user took
	conn.sql("delete from `tabHas Role` where parent=%s", email)
	conn.sql("delete from `tabUser` where name=%s", email)
	conn.commit()
	conn.close()


def test_snapshot_miss_then_committed_row_is_retryable():
	email = f"fa-race1-{RUN}@example.com"
	frappe.db.sql("select name from `tabUser` where mobile_no=%s", M[6])  # fix the snapshot: a miss
	conn = _insert_on_second_conn(email, M[6])
	try:
		frappe.local.message_log = []
		with pytest.raises(errors.ServiceUnavailable):
			identity.find_or_create_user(M[6], "n@example.com", "N", ["Blogger"])
		assert frappe.local.message_log == []
		frappe.db.rollback()  # fresh transaction, fresh snapshot: the normal path
		assert identity.find_or_create_user(M[6], "n@example.com", "N", ["Blogger"]) == (email, False)
		assert "Blogger" in frappe.get_roles(email)
	finally:
		_drop_on_second_conn(conn, email)


def test_duplicate_insert_between_read_and_insert_is_retryable(monkeypatch):
	email = f"fa-race2-{RUN}@example.com"
	conn = None
	real = identity._holders
	fired = []

	def racing(mobile, lock):
		nonlocal conn
		rows = real(mobile, lock)
		if not fired and not lock:
			fired.append(1)
			conn = _insert_on_second_conn(email, mobile)  # commits between the read and the insert
		return rows

	monkeypatch.setattr(identity, "_holders", racing)
	try:
		frappe.local.message_log = []
		with pytest.raises(errors.ServiceUnavailable):
			identity.find_or_create_user(M[7], "n@example.com", "N", ["Blogger"])
		assert frappe.local.message_log == []
		frappe.db.rollback()
		monkeypatch.setattr(identity, "_holders", real)
		assert identity.find_or_create_user(M[7], "n@example.com", "N", ["Blogger"]) == (email, False)
		assert "Blogger" in frappe.get_roles(email)
	finally:
		if conn:
			_drop_on_second_conn(conn, email)


def test_email_clash_reraises_and_takes_no_gap_lock():
	"""The duplicate is on the email: re-raise it, and leave the mobile index unlocked."""
	email = f"fa-mail-{RUN}@example.com"
	_user(email, M[1])
	frappe.local.message_log = []
	with pytest.raises(frappe.DuplicateEntryError):
		identity.find_or_create_user(M[2], email, "N", [])
	conn = _second_conn()
	other = f"fa-mail2-{RUN}@example.com"
	try:
		conn.sql("set session innodb_lock_wait_timeout=1")
		conn.sql(
			_INSERT,
			(other, other, M[2] + "9"),
		)
		conn.commit()
	finally:
		conn.sql("delete from `tabUser` where name=%s", other)
		conn.commit()
		conn.close()


def test_new_mobile_miss_takes_no_gap_lock():
	"""An uncommitted first signup must not block another insert in the same index gap."""
	email = f"fa-gap-{RUN}@example.com"
	assert identity.find_or_create_user(M[0], email, "G", []) == (email, True)  # left uncommitted
	conn = _second_conn()
	other = f"fa-gap2-{RUN}@example.com"
	try:
		conn.sql("set session innodb_lock_wait_timeout=1")
		conn.sql(
			_INSERT,
			(other, other, M[0] + "9"),
		)
		conn.commit()
	finally:
		conn.sql("delete from `tabUser` where name=%s", other)
		conn.commit()
		conn.close()
