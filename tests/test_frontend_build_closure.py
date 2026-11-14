"""Source build contracts for the two separately installed legacy frontends."""
import hashlib
import json
import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/frontend-build-baseline-a31.json").read_text())


class FrontendBuildClosure(unittest.TestCase):
    def test_modern_root_lock_and_all_existing_build_scripts_are_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / "package-lock.json").read_bytes()).hexdigest(),
                         BASELINE["root_lock_sha256"])
        scripts = json.loads((ROOT / "package.json").read_text())["scripts"]
        self.assertEqual({k: scripts[k] for k in BASELINE["build_scripts"]}, BASELINE["build_scripts"])
        for name, baseline in BASELINE["legacy"].items():
            self.assertEqual(json.loads((ROOT / name / "package.json").read_text())["scripts"], baseline["scripts"])

    def test_unused_restricted_calendar_is_absent_from_the_legacy_closure(self):
        marker = re.compile(r"syncfusion|ejs-calendar|ejs-datepicker|ejs-datetimepicker", re.I)
        for name in BASELINE["legacy"]:
            with self.subTest(frontend=name):
                project = json.loads((ROOT / name / "package.json").read_text())
                self.assertNotIn("@syncfusion/ej2-vue-calendars", project["dependencies"])
                sources = [p for p in (ROOT / name / "src").rglob("*")
                           if p.is_file() and p.suffix in {".js", ".ts", ".vue", ".css", ".scss", ".html"}]
                sources += list((ROOT / name).glob("*.js"))
                self.assertTrue(sources)
                self.assertEqual([], [str(p.relative_to(ROOT)) for p in sources if marker.search(p.read_text())])

    def test_every_remaining_direct_dependency_range_is_preserved(self):
        for name, baseline in BASELINE["legacy"].items():
            project = json.loads((ROOT / name / "package.json").read_text())
            for group in ("dependencies", "devDependencies"):
                expected = dict(baseline[group])
                if name == "urypos" and group == "dependencies":
                    expected.pop("@syncfusion/ej2-vue-calendars")
                with self.subTest(frontend=name, group=group):
                    self.assertEqual(project[group], expected)

    def test_both_legacy_frontends_have_complete_matching_npm_v3_locks(self):
        for name in BASELINE["legacy"]:
            with self.subTest(frontend=name):
                path = ROOT / name / "package-lock.json"
                self.assertTrue(path.is_file(), f"Missing immutable legacy lock: {name}")
                lock = json.loads(path.read_text())
                project = json.loads((ROOT / name / "package.json").read_text())
                self.assertEqual(lock["lockfileVersion"], 3)
                for group in ("dependencies", "devDependencies"):
                    self.assertEqual(lock["packages"][""][group], project[group])
                self.assertFalse(any("syncfusion" in entry.lower() for entry in lock["packages"]))
                for key, entry in lock["packages"].items():
                    if not key or entry.get("link"):
                        continue
                    self.assertTrue(entry.get("version"), key)
                    self.assertRegex(entry.get("integrity", ""), r"^sha(?:256|384|512)-", key)
                    self.assertTrue(entry.get("resolved", "").startswith("https://"), key)

    def test_native_install_aliases_use_frozen_legacy_project_locks(self):
        scripts = json.loads((ROOT / "package.json").read_text())["scripts"]
        for name, alias in (("urypos", "ury-pos-install"), ("mosaic", "ury-mosaic-install")):
            with self.subTest(frontend=name):
                command = shlex.split(scripts[alias])
                self.assertEqual(command[:3], ["npm", "--prefix", name])
                self.assertIn("ci", command)
                self.assertIn("--workspaces=false", command)
                self.assertIn("--ignore-scripts", command)
                self.assertNotIn("--legacy-peer-deps", command)
                self.assertNotIn("--force", command)


if __name__ == "__main__":
    unittest.main()
