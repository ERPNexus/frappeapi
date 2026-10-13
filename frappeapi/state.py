"""Redis for OTP, rate limits and the user lock (integration spec §6.5).

A plain redis.Redis, not frappe.cache(): RedisWrapper prefixes and pickles
hset/hget, which would break the Lua scripts. Every failure is 503.

Never redis_cache: it is allkeys-lru, so a held lock or an OTP key could be
evicted and fail-closed would silently fail open. Use frappeapi_redis, else
the bench's redis_queue (noeviction)."""

import hashlib
from contextlib import contextmanager
from functools import lru_cache

import frappe
from redis import Redis
from redis.exceptions import RedisError

from frappeapi.errors import ServiceUnavailable


@lru_cache(maxsize=4)
def _client(url: str) -> Redis:
	return Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)


def redis() -> Redis:
	url = frappe.conf.get("frappeapi_redis") or frappe.conf.get("redis_queue")
	if not url:
		raise ServiceUnavailable("No Redis configured for frappeapi state")
	return _client(url)


def key(purpose: str, ident: str) -> str:
	from frappeapi.contract import release  # a malformed frappeapi_release is treated as absent

	env = release().get("environment", "default")
	return f"{frappe.local.site}:{env}:{purpose}:{hashlib.sha256(ident.encode()).hexdigest()}"


@contextmanager
def guarded():
	try:
		yield
	except RedisError as e:
		raise ServiceUnavailable("The service is temporarily unavailable") from e
