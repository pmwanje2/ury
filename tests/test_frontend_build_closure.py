"""Source build contracts: every frontend installs from a committed, complete npm v3 lock.

Ported from the version-2 fork (9df80663, f6c00c41, ac6dfa07, b2ce8b35) onto version-16.
The root lock now also covers the `serve` workspace.
"""
import json
import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/frontend-build-baseline-v16.json").read_text())
QUALIFIED_JSRSASIGN_FLOOR = (11, 1, 3)
SOURCE_SUFFIXES = {".js", ".ts", ".vue", ".css", ".scss", ".html"}


def _json(path):
    return json.loads((ROOT / path).read_text())


def _legacy_sources(name):
    paths = [p for p in (ROOT / name / "src").rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES]
    paths += [p for p in (ROOT / name).glob("*config.*") if p.is_file()]
    paths += [ROOT / name / "proxyOptions.js", ROOT / name / "index.html"]
    return paths


def _version(text):
    return tuple(int(part) for part in text.split("."))


class FrontendBuildClosure(unittest.TestCase):
    def assert_complete_v3_lock(self, directory, project):
        path = ROOT / directory / "package-lock.json"
        self.assertTrue(path.is_file(), f"Missing committed npm lock: {directory or '.'}")
        self.assertFalse((ROOT / directory / "yarn.lock").exists(), f"Stale Yarn lock: {directory or '.'}")
        lock = json.loads(path.read_text())
        self.assertEqual(lock["lockfileVersion"], 3)
        for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "workspaces"):
            self.assertEqual(lock["packages"][""].get(field, {}), project.get(field, {}), field)
        self.assertFalse(any("syncfusion" in key.lower() for key in lock["packages"]))
        for key, entry in lock["packages"].items():
            if "node_modules/" not in key or entry.get("link"):
                continue
            self.assertTrue(entry.get("version"), key)
            self.assertRegex(entry.get("integrity", ""), r"^sha(?:256|384|512)-", key)
            self.assertTrue(entry.get("resolved", "").startswith("https://registry.npmjs.org/"), key)
        return lock

    def test_build_scripts_and_workspaces_are_upstream_version_16(self):
        project = _json("package.json")
        self.assertEqual(project["workspaces"], BASELINE["workspaces"])
        self.assertEqual({k: project["scripts"][k] for k in BASELINE["build_scripts"]}, BASELINE["build_scripts"])
        for name, baseline in BASELINE["legacy"].items():
            self.assertEqual(_json(f"{name}/package.json")["scripts"], baseline["scripts"])

    def test_root_lock_is_complete_and_covers_every_workspace(self):
        project = _json("package.json")
        lock = self.assert_complete_v3_lock("", project)
        workspaces = sorted(str(p.relative_to(ROOT)) for pattern in project["workspaces"]
                            for p in ROOT.glob(pattern) if (p / "package.json").is_file())
        self.assertIn("serve", workspaces)
        for workspace in workspaces:
            with self.subTest(workspace=workspace):
                name = _json(f"{workspace}/package.json")["name"]
                self.assertIn(workspace, lock["packages"])
                self.assertEqual(lock["packages"][f"node_modules/{name}"],
                                 {"resolved": workspace, "link": True})
                member = _json(f"{workspace}/package.json")
                for group in ("dependencies", "devDependencies"):
                    self.assertEqual(lock["packages"][workspace].get(group, {}), member.get(group, {}))

    def test_both_legacy_frontends_have_complete_matching_npm_v3_locks(self):
        for name in BASELINE["legacy"]:
            with self.subTest(frontend=name):
                self.assert_complete_v3_lock(name, _json(f"{name}/package.json"))

    def test_unused_restricted_calendar_is_absent_from_the_legacy_closure(self):
        marker = re.compile(r"syncfusion|ejs-calendar|ejs-datepicker|ejs-datetimepicker", re.I)
        for name in BASELINE["legacy"]:
            with self.subTest(frontend=name):
                self.assertNotIn("@syncfusion/ej2-vue-calendars", _json(f"{name}/package.json")["dependencies"])
                sources = _legacy_sources(name)
                self.assertTrue(sources)
                self.assertEqual([], [str(p.relative_to(ROOT)) for p in sources if marker.search(p.read_text())])

    def test_unused_legacy_package_manager_and_mosaic_wrapper_are_absent(self):
        for name in BASELINE["legacy"]:
            project = _json(f"{name}/package.json")
            forbidden = ["pnpm"] + (["flowbite-vue"] if name == "mosaic" else [])
            for marker in forbidden:
                with self.subTest(frontend=name, marker=marker):
                    self.assertNotIn(marker, project["dependencies"])
                    self.assertFalse(any(marker in p.read_text() for p in _legacy_sources(name)))
                    self.assertFalse(any(marker in command for command in project["scripts"].values()))

    def test_direct_dependency_ranges_only_have_reviewed_closure_changes(self):
        jsrsasign = _json("urypos/package.json")["dependencies"]["jsrsasign"]
        for name, baseline in BASELINE["legacy"].items():
            project = _json(f"{name}/package.json")
            for group in ("dependencies", "devDependencies"):
                expected = dict(baseline[group])
                if name == "urypos" and group == "dependencies":
                    expected.pop("@syncfusion/ej2-vue-calendars")
                    expected.pop("pnpm")
                    expected["jsrsasign"] = jsrsasign
                if name == "mosaic" and group == "dependencies":
                    expected.pop("flowbite-vue")
                with self.subTest(frontend=name, group=group):
                    self.assertEqual(project[group], expected)

    def test_legacy_rsa_signing_uses_the_root_lock_security_version(self):
        declared = _json("urypos/package.json")["dependencies"]["jsrsasign"]
        self.assertRegex(declared, r"^\d+\.\d+\.\d+$", "legacy jsrsasign must be an exact version")
        self.assertGreaterEqual(_version(declared), QUALIFIED_JSRSASIGN_FLOOR)
        modern = _json("package-lock.json")
        self.assertEqual(modern["packages"]["node_modules/jsrsasign"]["version"], declared)
        legacy = _json("urypos/package-lock.json")
        self.assertEqual(legacy["packages"]["node_modules/jsrsasign"]["version"], declared)

    def test_root_closure_entrypoint_runs_both_guard_harnesses(self):
        scripts = _json("package.json")["scripts"]
        self.assertEqual(scripts.get("test:closure"),
                         "python3 -m unittest discover -s tests -p test_frontend_build_closure.py && "
                         "node --test tests/qz-signing-compatibility.test.cjs")

    def test_native_install_aliases_use_frozen_legacy_project_locks(self):
        scripts = _json("package.json")["scripts"]
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
