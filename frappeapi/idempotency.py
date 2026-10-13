"""Idempotency hashing (integration spec §6.4).

body_hash is sha256(json.dumps(body, sort_keys=True, default=str)), except that
data-URI image strings are first replaced by their own sha256. So for payloads
without data-URI images it equals that plain canonical-JSON hash; an app that
stored exactly that hash keeps matching retries queued before an upgrade."""

import hashlib
import json


def _strip_images(value):
	if isinstance(value, dict):
		return {k: _strip_images(v) for k, v in value.items()}
	if isinstance(value, list):
		return [_strip_images(v) for v in value]
	if isinstance(value, str) and value.startswith("data:") and ";base64," in value[:100]:
		return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
	return value


def body_hash(body) -> str:
	return hashlib.sha256(json.dumps(_strip_images(body), sort_keys=True, default=str).encode()).hexdigest()


def replay(stored_hash: str, stored_response, body):
	"""Call when a record for this client_operation_id already exists."""
	from frappeapi.errors import IdempotencyConflict

	if stored_hash != body_hash(body):
		raise IdempotencyConflict("This operation id was already used with a different payload")
	return stored_response
