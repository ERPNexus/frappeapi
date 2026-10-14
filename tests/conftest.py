# tests/conftest.py
"""Tests run against a real bench site and its Redis (spec §8: no fake Redis).

Run from frappe-bench/sites:  ../env/bin/python -m pytest /home/ubuntu/frappeapi/tests -q
FRAPPEAPI_TEST_SITE picks the site and is required: never a site with real data. Tests roll back;
the few that commit through a second connection clean up in finally; Redis keys use
the purpose prefix 'test:' and are deleted."""

import os

import frappe
import pytest

SITE = os.environ.get("FRAPPEAPI_TEST_SITE")
if not SITE:
	raise SystemExit("set FRAPPEAPI_TEST_SITE to a test site (e.g. modarclean.localhost)")

# At import, not in a fixture: test modules import frappeapi during collection,
# and its import-time patch reads frappe.flags ("object is not bound" otherwise).
if not os.path.isdir(SITE):
	raise SystemExit(f"run from frappe-bench/sites; site {SITE} not found")
frappe.init(site=SITE, sites_path=".")
frappe.connect()
frappe.set_user("Administrator")


@pytest.fixture(autouse=True, scope="session")
def site():
	yield
	frappe.destroy()


@pytest.fixture(autouse=True)
def rollback(site):
	yield
	frappe.db.rollback()  # also releases any mobile lock a test took (after_rollback)


def redis_up() -> bool:
	"""Spec §8: Redis-dependent modules skip, not error, when Redis is unreachable."""
	from frappeapi import state

	try:
		return bool(state.redis().ping())
	except Exception:
		return False


def release(monkeypatch, **cfg):
	monkeypatch.setitem(frappe.local.conf, "frappeapi_release", cfg)
