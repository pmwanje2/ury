import os

import frappe
from werkzeug.wrappers import Response


class OfflineServiceWorkerPage:
	"""Serve the POS service worker from /sw.js so its scope can be the whole site.

	Files under /assets cannot send Service-Worker-Allowed. Without that header
	the worker cannot see /pos or /api, so the read cache would never run.
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
		response.headers["Service-Worker-Allowed"] = "/"
		response.headers["Cache-Control"] = "no-cache"
		return response
