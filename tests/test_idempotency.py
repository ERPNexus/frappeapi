import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from frappeapi import errors, idempotency


def reference_hash(payload) -> str:  # the existing request_hash scheme body_hash must match
	return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def test_equals_reference_hash_without_images():
	p = {"b": [1, {"z": Decimal("10.50"), "a": date(2026, 10, 13)}], "a": "عميل", "n": None}
	assert idempotency.body_hash(p) == reference_hash(p)


def test_images_hash_by_content_and_order_does_not_matter():
	img = "data:image/jpeg;base64," + "A" * 5000
	a = idempotency.body_hash({"photo": img, "x": 1})
	assert a == idempotency.body_hash({"x": 1, "photo": img})
	assert a != idempotency.body_hash({"x": 1, "photo": img + "B"})


def test_replay():
	h = idempotency.body_hash({"x": 1})
	assert idempotency.replay(h, {"ok": True}, {"x": 1}) == {"ok": True}
	with pytest.raises(errors.IdempotencyConflict):
		idempotency.replay(h, {"ok": True}, {"x": 2})
