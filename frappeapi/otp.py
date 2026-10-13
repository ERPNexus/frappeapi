"""SMS one-time codes in Redis (integration spec §6.5). The record holds the
code's sha256, the failed-attempt count and resend metadata (sent count, last
sent), nothing else. Redis failures are 503: verification fails closed."""

import hashlib
import secrets
import time
from collections.abc import Callable

from frappeapi import state
from frappeapi.errors import ServiceUnavailable
from frappeapi.identity import normalize_mobile

# Atomic: a match consumes the code; a miss counts and burns the code at the limit.
_VERIFY = """
local h = redis.call('HGET', KEYS[1], 'hash')
if not h then return 0 end
if h == ARGV[1] then redis.call('DEL', KEYS[1]) return 1 end
if redis.call('HINCRBY', KEYS[1], 'attempts', 1) >= tonumber(ARGV[2]) then redis.call('DEL', KEYS[1]) end
return 0
"""

# Delete only if the stored hash is still ours (a concurrent issue() replaced it otherwise).
_DEL_IF_HASH = """
if redis.call('HGET', KEYS[1], 'hash') == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _hash(code: str) -> str:
	return hashlib.sha256(code.encode()).hexdigest()


def issue(
	mobile: str, purpose: str, send: Callable[[str], None], ttl: int = 300, *, default_country_code: str | None = None
) -> None:
	"""Store a fresh code (replacing any earlier one) and hand it to send(code).
	If send raises, this call's code is deleted and ServiceUnavailable is raised.

	Callers must call ratelimit.hit (per mobile and per IP) before issue():
	`sent` in the record is informational, not a cap. default_country_code is as in
	identity.normalize_mobile."""
	k = state.key(f"otp:{purpose}", normalize_mobile(mobile, default_country_code))
	code = f"{secrets.randbelow(10**6):06d}"
	h = _hash(code)
	with state.guarded():
		pipe = state.redis().pipeline(transaction=True)
		pipe.hset(k, mapping={"hash": h, "attempts": 0, "last_sent": int(time.time())})
		pipe.hincrby(k, "sent", 1)
		pipe.expire(k, ttl)
		pipe.execute()
	try:
		send(code)
	except Exception as e:
		with state.guarded():
			state.redis().eval(_DEL_IF_HASH, 1, k, h)
		if isinstance(e, ServiceUnavailable):
			raise
		raise ServiceUnavailable(
			"The SMS could not be sent"
		) from None  # the gateway error may echo the SMS body (the code)


def verify(
	mobile: str, purpose: str, code: str, max_attempts: int = 5, *, default_country_code: str | None = None
) -> bool:
	"""True once per issued code; a wrong code counts toward max_attempts."""
	k = state.key(f"otp:{purpose}", normalize_mobile(mobile, default_country_code))
	if not isinstance(code, str):
		code = ""
	with state.guarded():
		return bool(state.redis().eval(_VERIFY, 1, k, _hash(code), max_attempts))
