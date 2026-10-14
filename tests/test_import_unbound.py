import subprocess
import sys
import textwrap


def test_import_and_install_patch_with_no_site_bound():
	"""`bench generate-pot-file` imports app modules with no site bound; frappe.flags
	is then an unbound LocalProxy and must not be read unguarded at import."""
	code = textwrap.dedent(
		"""
		import frappe
		import frappe.api
		from frappeapi.fast_routes import _install_patch

		try:
			frappe.flags.in_migrate
			raise SystemExit("precondition: a site is bound")
		except RuntimeError:
			pass
		if not getattr(frappe, "_fastapi_path_patch_done", False):
			_install_patch()
		assert frappe._fastapi_path_patch_done is True
		"""
	)
	r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
	assert r.returncode == 0, r.stderr
