"""G51: no user-visible assistant vendor name ("HUF"); the product is Maximus.

Site-less: reads the shipped sources only. What a user can see from the backend is
the translation source column, message strings (frappe.throw, returned reasons, Error
Log titles) and DocType labels/descriptions. Docstrings and comments are developer
text and stay. "HUF App" is the vendor app's DocType name, an identifier passed to
frappe.db, never shown, so it is the one string allowed.
"""

import ast
import csv
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = re.compile(r"\bHUF\b")
IDENTIFIERS = {"HUF App"}


def _docstring_nodes(tree):
	nodes = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
			first = node.body[0]
			if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
				nodes.add(id(first.value))
	return nodes


class AssistantVendorName(unittest.TestCase):
	def test_python_message_strings_never_name_the_vendor(self):
		offenders = []
		for path in sorted(ROOT.rglob("*.py")):
			if "tests" in path.parts or path.name.startswith("test_"):
				continue
			tree = ast.parse(path.read_text(encoding="utf-8"))
			docs = _docstring_nodes(tree)
			for node in ast.walk(tree):
				if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
						and node.value not in IDENTIFIERS and VENDOR.search(node.value)):
					offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
		self.assertGreater(len(list(ROOT.rglob("*.py"))), 50)
		self.assertEqual(offenders, [])

	def test_translation_sources_never_name_the_vendor(self):
		files = sorted((ROOT / "translations").glob("*.csv"))
		self.assertTrue(files)
		offenders = []
		for path in files:
			with path.open(encoding="utf-8", newline="") as handle:
				for number, row in enumerate(csv.reader(handle), 1):
					if row and any(VENDOR.search(cell) for cell in row[:2]):
						offenders.append(f"{path.name}:{number}")
		self.assertEqual(offenders, [])

	def test_doctype_labels_and_descriptions_never_name_the_vendor(self):
		offenders = []
		for path in sorted(ROOT.rglob("doctype/*/*.json")):
			meta = json.loads(path.read_text(encoding="utf-8"))
			for field in meta.get("fields", []) if isinstance(meta, dict) else []:
				for key in ("label", "description"):
					if VENDOR.search(field.get(key) or ""):
						offenders.append(f"{path.relative_to(ROOT)}:{field.get('fieldname')}.{key}")
		self.assertEqual(offenders, [])


if __name__ == "__main__":
	unittest.main()
