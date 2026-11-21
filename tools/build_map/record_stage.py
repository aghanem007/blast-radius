"""Record which sqlglot code runs where in test_execute_tpcds at the pinned commit.

Output: maps/tpcds_stage.json.gz, read by earlycut.StageMap.

  front     code objects that start while earlycut.front_half runs. This is, by
            construction, exactly the code the early-cutoff check re-runs.
  setup     code objects that start while the fixture Tables are built and pickled for
            the pool (shared by every query).
  back[q]   code objects that start in executed query q's back half: ensure_tables with
            the real tables, PythonExecutor.execute, pickling the result, reading its rows.

The selector uses it like a coverage map, one level down: a patched function that no
stage ran cannot be decided; one that only the front half ran needs the front half
re-run; one that query q's back half ran needs query q re-executed.

Code objects, not lines: a line event does not fire for a body that sits on its def
line (`"GT": lambda a, b: a > b`), so a line map would miss one-line lambdas.

usage (Python 3.12+; needs python-dateutil and mypy_extensions as in the image):
    PYTHONHASHSEED=0 python tools/build_map/record_stage.py <sqlglot root> maps/tpcds_stage.json.gz
"""

import gzip
import json
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import earlycut  # noqa: E402

root, out_path = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, root)
PKG = os.path.join(root, "sqlglot") + os.sep

mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "record_stage")
spans: dict = {}
current = None


def on_code(code, offset):
    fn = code.co_filename
    if current is not None and fn.startswith(PKG):
        lines = [ln for _, _, ln in code.co_lines() if ln is not None]
        lines.append(code.co_firstlineno)
        spans.setdefault(current, set()).add(("sqlglot/" + fn[len(PKG):].replace(os.sep, "/"), min(lines), max(lines)))
    return mon.DISABLE


EVENTS = mon.events.PY_START | mon.events.PY_RESUME
mon.register_callback(TOOL, mon.events.PY_START, on_code)
mon.register_callback(TOOL, mon.events.PY_RESUME, on_code)


def stage(name):
    """Switch stage. Every code object must be seen again in the new stage."""
    global current
    current = name
    mon.restart_events()
    mon.set_events(TOOL, EVENTS if name is not None else 0)


# Import everything first, unrecorded: import-time code is never eligible anyway
# (the selector sends import-time changes down a different path).
import sqlglot.executor  # noqa: E402,F401
from tests.helpers import TPCDS_SCHEMA  # noqa: E402

back = earlycut.BackHalf(root)
cpu = {"back": 0.0}

# The fixture tables, loaded as the test's setUpClass loads them (DuckDB aside), and
# pickled as the pool's initializer arguments.
t0 = time.process_time()
stage("setup")
back.load_all()
pickle.loads(pickle.dumps((TPCDS_SCHEMA, back.tables)))
stage(None)
cpu["setup"] = time.process_time() - t0


class Hooks:
    executed = 0

    def back(self, i, names, plan):
        t = time.process_time()
        stage(("back", i))
        back.run(names, plan)
        stage("front")
        cpu["back"] += time.process_time() - t
        Hooks.executed += 1
        print(f"  executed {Hooks.executed}", file=sys.stderr, flush=True)


t0 = time.process_time()
stage("front")
items = earlycut.front_half(root, hooks=Hooks())
stage(None)
total = time.process_time() - t0

bad = [k for k, v in items.items() if v.startswith("!")]
if bad:
    sys.exit(f"front half raised at the pinned commit: {bad[:5]}")


def by_file(pairs):
    out = {}
    for fn, a, b in sorted(pairs):
        out.setdefault(fn, []).append([a, b])
    return out


back_union = set().union(*(v for k, v in spans.items() if isinstance(k, tuple)))
payload = {
    "version": earlycut.VERSION,
    "front": by_file(spans.get("front", ())),
    "setup": by_file(spans.get("setup", ())),
    "back": {str(k[1]): by_file(v) for k, v in spans.items() if isinstance(k, tuple)},
    "info": {
        "executed_queries": Hooks.executed, "fingerprints": len(items),
        "front_code_objects": len(spans.get("front", ())), "setup_code_objects": len(spans.get("setup", ())),
        "back_code_objects": len(back_union),
        "cpu_s": {"front": round(total - cpu["back"], 2), "back": round(cpu["back"], 2), "setup": round(cpu["setup"], 2)},
    },
}
with gzip.open(out_path, "wt", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
print(json.dumps(payload["info"]))
