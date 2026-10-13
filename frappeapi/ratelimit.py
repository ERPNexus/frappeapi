"""Redis buckets per identifier and per IP (integration spec §6.5).

Fixed window: the TTL is set when the bucket is created and never refreshed, so
slow traffic cannot accumulate into a permanent block."""

import frappe

from frappeapi import state
from frappeapi.errors import RateLimited

# INCR, then arm the expiry only on a new bucket (or one that lost its TTL).
# Lua because EXPIRE NX needs Redis 7 and INCR + EXPIRE must be atomic.
_INCR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 or redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


def client_ip() -> str:
	return getattr(frappe.local, "request_ip", "") or ""


def fail(purpose: str, ident: str, seconds: int) -> int:
	k = state.key(f"rl:{purpose}", ident)
	with state.guarded():
		return int(state.redis().eval(_INCR, 1, k, seconds))


def hit(purpose: str, ident: str, limit: int, seconds: int) -> None:
	if fail(purpose, ident, seconds) > limit:
		raise RateLimited("Too many requests. Please try again later.")


def check(purpose: str, ident: str, limit: int) -> None:
	with state.guarded():
		count = int(state.redis().get(state.key(f"rl:{purpose}", ident)) or 0)
	if count >= limit:
		raise RateLimited("Too many attempts. Please try again later.")


def clear(purpose: str, ident: str) -> None:
	with state.guarded():
		state.redis().delete(state.key(f"rl:{purpose}", ident))
