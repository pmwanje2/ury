import os

import frappe
from werkzeug.wrappers import Response


class OfflineServiceWorkerPage:
	"""Serve the POS service worker from /sw.js for the /pos scope only.

	A worker under /assets/ury/pos/ could not control /pos pages. Served from
	the root, Service-Worker-Allowed pins the maximum scope to /pos so it can
	never control Desk, login or other apps. Controlled /pos pages still route
	their /api and /assets/ury/pos/ fetches through it.
	"""

	def __init__(self, path, http_status_code=None):
		self.path = path
		self.http_status_code = http_status_code or 200

	def can_render(self):
		return self.path == "sw.js"

	def render(self):
		script = frappe.get_app_path("ury", "public", "pos", "sw.js")
		if not os.path.isfile(script):
			return Response("Not Found", status=404, mimetype="text/plain")
		with open(script, "rb") as handle:
			body = handle.read()
		response = Response(body, status=200, mimetype="application/javascript")
		response.headers["Service-Worker-Allowed"] = "/pos"
		response.headers["Cache-Control"] = "no-cache"
		return response
