"""Subprocess half of ``test_core_seam.py``: drives the REAL Hermes core hook dispatch into the REAL plugin
loader and the UsageDeck observer, on a scratch home. Prints one JSON line.

argv: <core checkout> <plugin source dir> <scenario json>. The payload is a synthetic test callback (the core
``_dispatch`` function is called with an invented request provenance); no HTTP request is ever made and the
output is not a live Anthropic observation.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

core, plugin_src, scenario = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, core)

base = Path(tempfile.mkdtemp(prefix="usage-deck-seam-", dir="/tmp"))
root = base / "root"
home = root / "profiles" / "work" if scenario.get("home", "work") == "work" else root
home.mkdir(parents=True, exist_ok=True)
(root / "config.yaml").write_text("{}\n")
enable = "plugins:\n  enabled:\n    - usage-deck\n"
setting = scenario.get("setting")
(home / "config.yaml").write_text(
    enable + ("" if setting is None else f"usage_deck:\n  capture_anthropic_headers: {setting}\n"))
(home / "plugins").mkdir(exist_ok=True)
shutil.copytree(plugin_src, home / "plugins" / "usage-deck", ignore=shutil.ignore_patterns("__pycache__", "tests", "dashboard"))
os.environ["HERMES_HOME"] = str(home)
os.environ["HOME"] = str(base)

from hermes_cli import plugins as host  # noqa: E402

manager = host.get_plugin_manager()
manager.discover_and_load(force=True)
result = {"has_hook": host.has_hook("provider_ratelimit_headers")}

from agent.provider_ratelimit_headers import RequestProvenance, _dispatch, sanitize_headers  # noqa: E402

execution = (scenario.get("execution", "work"), scenario.get("home", "work") != "work")
holder = tuple(scenario.get("holder", [execution[0], execution[1]]))
provenance = RequestProvenance(str(root.resolve()), execution, holder, scenario.get("row", "ant-row-1"))
raw = {"anthropic-ratelimit-unified-5h-utilization": "0.25", "anthropic-ratelimit-unified-5h-reset": "4102444000",
       "anthropic-ratelimit-unified-7d-utilization": "0.5", "authorization": "Bearer must-not-persist", "set-cookie": "x=1"}
_dispatch(provenance, sanitize_headers(raw))
store = root / "plugin-data" / "usage-deck" / "anthropic-ratelimit.json"
result["store"] = json.loads(store.read_text()) if store.exists() else None
result["tree"] = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if "plugins" not in p.parts and p.is_file())
print("RESULT " + json.dumps(result))
shutil.rmtree(base, ignore_errors=True)
