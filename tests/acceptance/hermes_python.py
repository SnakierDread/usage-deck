"""Pick the interpreter that can import Hermes (stdlib only; shared by every harness subprocess that does).

Why: Hermes bootstraps its dependency closure (``ruamel.yaml``, ...) *inside the launcher process*
(``hermes_bootstrap.activate_dependencies`` adds the committed venv's site-packages to ``sys.path``). A plain
``python3`` on PATH never sees that closure, so a helper that does ``from hermes_state import SessionDB`` dies with
``ModuleNotFoundError``. Worse, the launcher's own ``sys.executable`` is the *bare* managed tool Python, which lacks
the closure too: the interpreter that has it is the committed environment's ``venv/bin/python`` (a symlink to that
tool Python with a ``pyvenv.cfg`` beside it), whose site-packages the launcher activates.

Resolution order, first hit wins, resolved ONCE per run (``resolve_hermes_python``):

1. ``--hermes-python PATH`` flag
2. ``$HERMES_PYTHON``
3. auto-detect through the hermes launcher: it is asked (``hermes --run-module timeit ...``, i.e. inside its own
   bootstrapped process) to report ``sys.executable``, the activated site-packages directories and the checkout
   holding ``hermes_constants``. If an activated site-packages belongs to a venv with its own interpreter, that venv
   interpreter is used; otherwise ``sys.executable`` is (external / Nix installs whose interpreter owns its path).
4. only then ``python3`` on PATH (last resort: ``sys.executable`` of the harness itself when PATH has none).

An explicit choice (1, 2) is never silently replaced: if it does not name an executable the run is refused. A failing
launcher probe falls through to 4 and says so. ``check_closure`` proves the choice can import Hermes before any
scenario runs.

Every subprocess that imports a Hermes module (session seeding, the ``hermes_constants`` isolation probe, the
core-seam runner behind ``USAGE_DECK_CORE_CHECKOUT``) must be launched with the resolved interpreter, never with
``sys.executable`` / bare ``python3``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "USAGE_DECK_HERMES_PROBE="

# Runs INSIDE the launcher's bootstrapped process (via ``--run-module timeit``: stdlib, executes one statement).
_PROBE_STATEMENT = (
    "import sys, os, json, importlib.util; "
    "s = importlib.util.find_spec('hermes_constants'); "
    "print('" + MARKER + "' + json.dumps({'executable': sys.executable, "
    "'site_packages': [p for p in sys.path if 'site-packages' in p], "
    "'agent_root': (os.path.dirname(s.origin) if s and s.origin else '')}))"
)
PROBE_ARGV = ("--run-module", "timeit", "-n", "1", "-r", "1", _PROBE_STATEMENT)

SOURCES = ("--hermes-python", "$HERMES_PYTHON", "hermes launcher", "PATH python3")


@dataclass
class Resolution:
    python: str
    source: str                       # one of SOURCES (or "sys.executable" when even PATH has no python3)
    agent_root: str = ""              # checkout the launcher reported (hermes_constants' directory), if probed
    notes: list = field(default_factory=list)   # why earlier sources were skipped (shown in the banner)

    def describe(self) -> str:
        return f"{self.python}  (from {self.source})" + "".join(f"\n    note: {n}" for n in self.notes)


class HermesPythonError(Exception):
    """An explicitly chosen interpreter is unusable (never silently replaced by a later source)."""


def _executable(candidate: str, which=shutil.which):
    """Absolute path of an executable named by path or bare command; None if it is neither."""
    if not candidate:
        return None
    found = which(candidate) if os.sep not in candidate else (candidate if os.access(candidate, os.X_OK) and os.path.isfile(candidate) else None)
    return os.path.abspath(found) if found else None


def probe_launcher(hermes: str, *, run=subprocess.run, timeout: int = 120, env=None):
    """Ask the launcher what it is running on. Returns ``(probe dict | None, error text)``."""
    try:
        proc = run([hermes, *PROBE_ARGV], capture_output=True, text=True, timeout=timeout,
                   stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith(MARKER):
            try:
                data = json.loads(line[len(MARKER):])
            except ValueError:
                break
            if isinstance(data, dict) and isinstance(data.get("executable"), str):
                return data, ""
    return None, f"no probe line from launcher (exit {proc.returncode}): {(proc.stderr or proc.stdout or '')[-200:].strip()}"


def venv_interpreter(site_packages: str):
    """``<venv>/lib/pythonX.Y/site-packages`` (or ``<venv>/Lib/site-packages``) -> that venv's own interpreter, if it has one."""
    p = Path(site_packages)
    for venv in (p.parents[2] if len(p.parents) > 2 else None, p.parents[1] if len(p.parents) > 1 else None):
        if venv is None or not (venv / "pyvenv.cfg").is_file():
            continue
        for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
            exe = venv / rel
            if exe.is_file() and os.access(exe, os.X_OK):
                return str(exe)
    return None


def pick_from_probe(probe: dict):
    """Interpreter to use given a launcher probe: the activated venv's interpreter, else the launcher's sys.executable."""
    for sp in probe.get("site_packages") or []:
        exe = venv_interpreter(sp)
        if exe:
            return exe
    return probe["executable"] if _executable(probe["executable"]) else None


def resolve_hermes_python(flag=None, environ=None, hermes="hermes", *, which=shutil.which, run=subprocess.run,
                          probe_timeout: int = 120) -> Resolution:
    environ = os.environ if environ is None else environ
    if flag:
        exe = _executable(flag, which)
        if not exe:
            raise HermesPythonError(f"--hermes-python {flag!r} is not an executable")
        return Resolution(exe, SOURCES[0])
    if environ.get("HERMES_PYTHON"):
        exe = _executable(environ["HERMES_PYTHON"], which)
        if not exe:
            raise HermesPythonError(f"$HERMES_PYTHON {environ['HERMES_PYTHON']!r} is not an executable")
        return Resolution(exe, SOURCES[1])
    notes = []
    launcher = _executable(hermes, which)
    if not launcher:
        notes.append(f"hermes launcher {hermes!r} not found; could not auto-detect")
    else:
        probe, error = probe_launcher(launcher, run=run, timeout=probe_timeout)
        if probe is None:
            notes.append(f"launcher probe failed ({error})")
        else:
            exe = pick_from_probe(probe)
            if exe:
                return Resolution(exe, SOURCES[2], agent_root=str(probe.get("agent_root") or ""), notes=notes)
            notes.append(f"launcher reported an unusable interpreter {probe['executable']!r}")
    fallback = which("python3")
    if fallback:
        return Resolution(os.path.abspath(fallback), SOURCES[3], notes=notes)
    notes.append("no python3 on PATH")
    return Resolution(sys.executable, "sys.executable", notes=notes)


def check_closure(python: str, agent_root: str, *, run=subprocess.run, timeout: int = 120):
    """None if ``python`` imports the Hermes modules the harness helpers need, else a one-line reason."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import hermes_constants, hermes_state"
    try:
        proc = run([python, "-c", code, agent_root], capture_output=True, text=True, timeout=timeout,
                   stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "")})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return None
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {proc.returncode}"
