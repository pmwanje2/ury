"""/ury-pos-sw.js serves the POS worker with a /pos ceiling.

It never answers for the v16 Serve worker or for another app's /sw.js (POS Awesome
registers /sw.js at scope /).
"""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _page_class():
	try:
		return importlib.import_module("ury.offline_sw").OfflineServiceWorkerPage
	except ModuleNotFoundError:
		return None


class OfflineServiceWorkerPageTest(unittest.TestCase):
	def page(self, path):
		cls = _page_class()
		self.assertIsNotNone(cls, "ury.offline_sw.OfflineServiceWorkerPage is not implemented")
		return cls(path)

	def test_hooks_register_the_page_renderer(self):
		hooks = importlib.import_module("ury.hooks")
		self.assertIn("ury.offline_sw.OfflineServiceWorkerPage", getattr(hooks, "page_renderer", []))

	def test_only_the_ury_pos_worker_name_is_claimed(self):
		self.assertTrue(self.page("ury-pos-sw.js").can_render())
		for path in ("ury/serve/sw.js", "serve-sw.js", "serve-sw", "pos", "pos/sw.js", "sw.json", "", "pos/ury-pos-sw.js", "ury-pos-sw"):
			with self.subTest(path=path):
				self.assertFalse(self.page(path).can_render())

	def test_another_apps_root_sw_js_is_left_to_that_app(self):
		# /sw.js belongs to whichever app ships www/sw.js (POS Awesome does, with
		# scope /). Custom page renderers run before StaticPage, so claiming it
		# would serve the URY worker with a /pos ceiling and break that app.
		self.assertFalse(self.page("sw.js").can_render())

	def test_response_pins_the_worker_to_pos(self):
		with tempfile.TemporaryDirectory() as root:
			script = Path(root) / "sw.js"
			script.write_text("self.addEventListener('fetch', () => {});")
			with patch("frappe.get_app_path", return_value=str(script)):
				response = self.page("ury-pos-sw.js").render()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.mimetype, "application/javascript")
		self.assertEqual(response.headers["Service-Worker-Allowed"], "/pos")
		self.assertEqual(response.headers["Cache-Control"], "no-cache")

	def test_missing_build_is_a_plain_404_not_html(self):
		with tempfile.TemporaryDirectory() as root:
			with patch("frappe.get_app_path", return_value=str(Path(root) / "missing.js")):
				response = self.page("ury-pos-sw.js").render()
		self.assertEqual(response.status_code, 404)
		self.assertNotIn("Service-Worker-Allowed", response.headers)

	def test_serve_after_request_leaves_the_pos_worker_untouched(self):
		from types import SimpleNamespace

		from ury.ury.controllers.serve_pwa import after_request

		response = SimpleNamespace(status_code=200, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/pos"})
		after_request(response=response, request=SimpleNamespace(path="/ury-pos-sw.js"))
		self.assertEqual(response.headers["Service-Worker-Allowed"], "/pos")


if __name__ == "__main__":
	unittest.main()
