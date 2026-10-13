# tests/test_ratelimit.py
from unittest.mock import patch

import pytest
from conftest import redis_up  # pytest puts tests/ on sys.path (no __init__.py)
from redis.exceptions import ConnectionError as RedisDown

from frappeapi import errors, ratelimit, state

if not redis_up():
	pytest.skip("Redis is unreachable (spec §8: no fake Redis)", allow_module_level=True)


@pytest.fixture(autouse=True)
def clean():
	yield
	for ident in ("a", "b"):
		state.redis().delete(state.key("rl:test", ident))


def test_hit_blocks_after_limit_per_ident():
	for _ in range(3):
		ratelimit.hit("test", "a", 3, 60)
	with pytest.raises(errors.RateLimited):
		ratelimit.hit("test", "a", 3, 60)
	ratelimit.hit("test", "b", 3, 60)  # another bucket is independent


def test_failure_counting_pattern():
	ratelimit.check("test", "a", 2)
	ratelimit.fail("test", "a", 60)
	ratelimit.fail("test", "a", 60)
	with pytest.raises(errors.RateLimited):
		ratelimit.check("test", "a", 2)
	ratelimit.clear("test", "a")
	ratelimit.check("test", "a", 2)


def test_window_expires():
	ratelimit.hit("test", "a", 5, 60)
	assert 0 < state.redis().ttl(state.key("rl:test", "a")) <= 60


def test_redis_down_fails_closed():
	with patch.object(state, "redis", side_effect=RedisDown("down")), pytest.raises(errors.ServiceUnavailable):
		ratelimit.hit("test", "a", 5, 60)


def test_later_hit_does_not_refresh_ttl():
	import time

	ratelimit.hit("test", "a", 5, 60)
	k = state.key("rl:test", "a")
	state.redis().expire(k, 30)  # age the window so a refresh would be visible
	time.sleep(1.1)
	ratelimit.hit("test", "a", 5, 60)
	assert state.redis().ttl(k) <= 29


@pytest.mark.parametrize(
	"call",
	[
		lambda: ratelimit.check("test", "a", 5),
		lambda: ratelimit.fail("test", "a", 60),
		lambda: ratelimit.clear("test", "a"),
	],
)
def test_redis_down_fails_closed_for_all(call):
	with patch.object(state, "redis", side_effect=RedisDown("down")), pytest.raises(errors.ServiceUnavailable):
		call()
