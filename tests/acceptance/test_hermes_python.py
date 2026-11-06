"""Offline tests for HERMES_PYTHON selection (stub launcher scripts; no Hermes, no network, no live calls).

    python -m unittest discover -s tests/acceptance
"""
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hermes_python as hp  # noqa: E402
import host_acceptance as ha  # noqa: E402


def make_exe(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ud-hp-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.calls = self.tmp / "launcher-calls.log"
        self.flag_py = make_exe(self.tmp / "flag" / "python", "exit 0\n")
        self.env_py = make_exe(self.tmp / "env" / "python", "exit 0\n")
        self.path_py = make_exe(self.tmp / "bin" / "python3", "exit 0\n")
        # a committed-environment venv: pyvenv.cfg + its own interpreter, as Hermes activates it
        self.venv = self.tmp / "envs" / "abc" / "venv"
        (self.venv / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
        (self.venv / "pyvenv.cfg").write_text("home = /nowhere\n")
        self.venv_py = make_exe(self.venv / "bin" / "python3", "exit 0\n")
        self.bare_py = make_exe(self.tmp / "tools" / "bin" / "python3", "exit 0\n")   # what the launcher's sys.executable is
        self.which = lambda name: self.path_py if name == "python3" else None

    def stub_launcher(self, probe=None, *, exit_code=0, noise=True):
        """A launcher stub: records its argv, then (like ``hermes --run-module timeit``) prints noise + the probe line."""
        lines = [f'echo "$@" >> {self.calls}']
        if noise:
            lines.append('echo "1 loop, best of 1: 4.9 usec per loop"')
        if probe is not None:
            lines.append(f"echo '{hp.MARKER}{json.dumps(probe)}'")
        lines.append(f"exit {exit_code}")
        return make_exe(self.tmp / "launcher" / "hermes", "\n".join(lines) + "\n")

    def launcher_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def good_probe(self):
        return {"executable": self.bare_py,
                "site_packages": [str(self.tmp / "tools" / "lib" / "python3.14" / "site-packages"),
                                  str(self.venv / "lib" / "python3.14" / "site-packages")],
                "agent_root": "/stub/hermes-agent"}


class SelectionOrder(Base):
    def test_flag_wins_over_everything_and_launcher_is_not_asked(self):
        launcher = self.stub_launcher(self.good_probe())
        r = hp.resolve_hermes_python(self.flag_py, {"HERMES_PYTHON": self.env_py}, launcher, which=self.which)
        self.assertEqual((r.python, r.source), (self.flag_py, "--hermes-python"))
        self.assertEqual(self.launcher_calls(), [])

    def test_env_wins_over_launcher_and_path(self):
        launcher = self.stub_launcher(self.good_probe())
        r = hp.resolve_hermes_python(None, {"HERMES_PYTHON": self.env_py}, launcher, which=self.which)
        self.assertEqual((r.python, r.source), (self.env_py, "$HERMES_PYTHON"))
        self.assertEqual(self.launcher_calls(), [])

    def test_launcher_wins_over_path_and_yields_the_activated_venv_interpreter(self):
        launcher = self.stub_launcher(self.good_probe())
        r = hp.resolve_hermes_python(None, {}, launcher, which=self.which)
        self.assertEqual((r.python, r.source), (self.venv_py, "hermes launcher"))
        self.assertEqual(r.agent_root, "/stub/hermes-agent")
        self.assertNotEqual(r.python, self.path_py)
        self.assertEqual(len(self.launcher_calls()), 1)               # resolved ONCE
        self.assertTrue(self.launcher_calls()[0].startswith("--run-module timeit"))

    def test_launcher_without_a_venv_falls_back_to_its_sys_executable(self):
        probe = dict(self.good_probe(), site_packages=[str(self.tmp / "tools" / "lib" / "python3.14" / "site-packages")])
        r = hp.resolve_hermes_python(None, {}, self.stub_launcher(probe), which=self.which)
        self.assertEqual((r.python, r.source), (self.bare_py, "hermes launcher"))

    def test_path_python3_only_when_the_launcher_probe_fails(self):
        r = hp.resolve_hermes_python(None, {}, self.stub_launcher(None, exit_code=3), which=self.which)
        self.assertEqual((r.python, r.source), (self.path_py, "PATH python3"))
        self.assertTrue(any("launcher probe failed" in n for n in r.notes), r.notes)

    def test_path_python3_when_launcher_is_missing(self):
        r = hp.resolve_hermes_python(None, {}, str(self.tmp / "nope" / "hermes"), which=self.which)
        self.assertEqual((r.python, r.source), (self.path_py, "PATH python3"))
        self.assertTrue(any("not found" in n for n in r.notes), r.notes)

    def test_launcher_reporting_an_unusable_interpreter_falls_through(self):
        probe = {"executable": str(self.tmp / "gone" / "python3"), "site_packages": [], "agent_root": ""}
        r = hp.resolve_hermes_python(None, {}, self.stub_launcher(probe), which=self.which)
        self.assertEqual(r.source, "PATH python3")

    def test_last_resort_is_the_harness_interpreter_when_path_has_no_python3(self):
        r = hp.resolve_hermes_python(None, {}, str(self.tmp / "nope"), which=lambda n: None)
        self.assertEqual((r.python, r.source), (sys.executable, "sys.executable"))

    def test_explicit_choices_are_never_silently_replaced(self):
        launcher = self.stub_launcher(self.good_probe())
        with self.assertRaises(hp.HermesPythonError):
            hp.resolve_hermes_python(str(self.tmp / "missing"), {"HERMES_PYTHON": self.env_py}, launcher, which=self.which)
        with self.assertRaises(hp.HermesPythonError):
            hp.resolve_hermes_python(None, {"HERMES_PYTHON": str(self.tmp / "missing")}, launcher, which=self.which)
        self.assertEqual(self.launcher_calls(), [])

    def test_bare_command_names_resolve_through_path(self):
        r = hp.resolve_hermes_python("mypy3", {}, "hermes", which=lambda n: self.flag_py if n == "mypy3" else None)
        self.assertEqual(r.python, self.flag_py)

    def test_probe_is_a_pure_stdlib_statement_run_inside_the_launcher(self):
        self.assertEqual(hp.PROBE_ARGV[:2], ("--run-module", "timeit"))
        stmt = hp.PROBE_ARGV[-1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(stmt, {})                                               # noqa: S102 - our own constant
        line = next(x for x in out.getvalue().splitlines() if x.startswith(hp.MARKER))
        data = json.loads(line[len(hp.MARKER):])
        self.assertEqual(data["executable"], sys.executable)
        self.assertEqual(set(data), {"executable", "site_packages", "agent_root"})


class HarnessWiring(Base):
    SRC = (HERE / "host_acceptance.py").read_text()

    def test_no_subprocess_uses_the_ambient_interpreter(self):
        self.assertIsNone(re.search(r"\[\s*sys\.executable", self.SRC))
        self.assertIsNone(re.search(r"\[\s*[\"']python3?[\"']", self.SRC))
        for needle in ("self.args.hermes_python, str(REPO", "ctx.args.hermes_python, str(probe)"):
            self.assertIn(needle, self.SRC)

    def test_help_documents_resolution_order(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            ha.parse_args(["--help"])
        text = out.getvalue()
        self.assertIn("--hermes-python PATH", text)
        self.assertLess(text.index("1. --hermes-python PATH"), text.index("2. $HERMES_PYTHON"))
        self.assertLess(text.index("2. $HERMES_PYTHON"), text.index("3. auto-detect"))
        self.assertLess(text.index("3. auto-detect"), text.index("4. python3 on PATH"))

    def run_main(self, *argv, hermes):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = ha.main(["--hermes", hermes, "--workdir", str(self.tmp), "--only", "isolation", *argv])
        return rc, err.getvalue()

    def test_main_refuses_an_unusable_explicit_interpreter(self):
        rc, err = self.run_main("--hermes-python", str(self.tmp / "missing"), hermes=self.stub_launcher(self.good_probe()))
        self.assertEqual(rc, 2)
        self.assertIn("--hermes-python", err)

    def test_main_refuses_an_interpreter_without_the_hermes_closure(self):
        broken = make_exe(self.tmp / "broken" / "python", "echo \"ModuleNotFoundError: No module named 'ruamel'\" >&2\nexit 1\n")
        rc, err = self.run_main("--hermes-python", broken, "--hermes-agent", str(self.tmp), hermes=self.stub_launcher(self.good_probe()))
        self.assertEqual(rc, 2)
        self.assertIn("ruamel", err)
        self.assertIn("cannot import the Hermes modules", err)

    def test_resolve_runtime_fills_interpreter_and_agent_from_the_launcher(self):
        args = ha.parse_args(["--hermes", self.stub_launcher(self.good_probe())])
        old = os.environ.pop("HERMES_PYTHON", None)
        try:
            res = ha.resolve_runtime(args)
        finally:
            if old is not None:
                os.environ["HERMES_PYTHON"] = old
        self.assertEqual((args.hermes_python, args.hermes_agent), (self.venv_py, "/stub/hermes-agent"))
        self.assertEqual(res.source, "hermes launcher")


class ClosureCheck(Base):
    def test_reports_the_last_error_line_and_passes_clean_interpreters(self):
        bad = make_exe(self.tmp / "bad" / "python", "echo 'Traceback'; echo \"ModuleNotFoundError: No module named 'ruamel'\" >&2; exit 1\n")
        self.assertIn("ruamel", str(hp.check_closure(bad, "/x")))
        self.assertIsNone(hp.check_closure(self.flag_py, "/x"))


if __name__ == "__main__":
    unittest.main()
