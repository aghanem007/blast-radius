"""Record which class-level data of sqlglot test_execute_tpcds reads OUTSIDE its front half.

Output: maps/tpcds_back_attrs.json.gz, read by selector.BackAttrs.

The early-cutoff check (earlycut.py) re-runs the test's front half on the patched code
and compares what it hands on. For a patch that only changes class attributes (settings,
tables: state made while sqlglot is imported), that comparison decides the test if the
rest of the test never looks at a changed attribute: with the same inputs and the same
data, the rest does exactly what it did at the pinned commit.

"The rest" is what tools/build_map/record_stage.py calls setup and back:
  setup   building the fixture Tables as setUpClass does and pickling them for the pool
  back    per executed query: ensure_tables with the real tables, PythonExecutor.execute,
          pickling the result, reading its rows

How a read is seen: as in attrrec.py (the attribute map's recorder), every plain class
attribute is replaced by a descriptor that notes "module.Class:NAME" when it is read.
The same wrapping code is used, so the keys are the attribute map's keys.

Which reads happen can depend on what earlier work left in sqlglot's caches. The test's
pool workers run queries in no fixed grouping, so two extremes are recorded and united:
  - one process, every query in the test's order (warm caches, as late in a worker);
  - one fresh process per executed query (cold caches, as a worker's first task).

Runs in the map-builder image (pinned sqlglot at /sqlglot, the scorer's interpreter):
    python record_back_attrs.py all <sqlglot root> <out .json.gz> [--jobs N]
    python record_back_attrs.py one <sqlglot root> <out .json> <query number | all>
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))


def record_one(root: str, out_path: str, which: str) -> int:
    import attrrec  # the attribute map's recorder: same wrapping, same keys
    import earlycut

    sys.path.insert(0, root)
    attrrec._import_everything()
    import sqlglot.executor  # noqa: F401
    from tests.helpers import TPCDS_SCHEMA

    attrrec._wrap_loaded()
    reads: dict[str, set[str]] = {"setup": set(), "back": set()}

    def stage(name: str | None) -> None:
        attrrec._wrap_loaded()  # classes from modules imported since the last look
        attrrec._cur = reads[name] if name is not None else None

    back = earlycut.BackHalf(root)
    stage("setup")
    back.load_all()
    pickle.loads(pickle.dumps((TPCDS_SCHEMA, back.tables)))
    stage(None)

    executed: list[int] = []

    class Hooks:
        def back(self, i, names, plan):
            stage("back")
            try:
                back.run(names, plan)
            finally:
                stage(None)
            executed.append(i)

    only = None if which == "all" else {int(which)}
    items = earlycut.front_half(root, hooks=Hooks(), only=only)
    stage(None)
    bad = [k for k, v in items.items() if v.startswith("!")]
    if bad:
        print(f"front half raised at the pinned commit: {bad[:5]}", file=sys.stderr)
        return 1
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "setup": sorted(reads["setup"]), "back": sorted(reads["back"]), "executed": executed,
            "wrapped": sorted(attrrec._wrapped),
        }, f)
    return 0


def record_all(root: str, out_path: str, jobs: int) -> int:
    import earlycut

    tmp = tempfile.mkdtemp()

    def run(which: str) -> dict:
        part = os.path.join(tmp, f"{which}.json")
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "one", root, part, which], capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"recording {which} failed: {p.stderr[-400:]}")
        with open(part, encoding="utf-8") as f:
            return json.load(f)

    warm = run("all")
    queries = warm["executed"]
    print(f"warm run: {len(queries)} executed queries, {len(warm['back'])} keys read in the back half, {len(warm['setup'])} in setup", file=sys.stderr, flush=True)
    keys = set(warm["setup"]) | set(warm["back"])
    cold_only: set[str] = set()
    with ThreadPoolExecutor(jobs) as pool:
        for q, row in zip(queries, pool.map(lambda q: run(str(q)), queries)):
            if row["executed"] != [q] or row["wrapped"] != warm["wrapped"]:
                raise SystemExit(f"query {q}: the fresh process executed {row['executed']} or wrapped other attributes")
            got = set(row["setup"]) | set(row["back"])
            cold_only |= got - keys
            keys |= got
    payload = {
        "version": earlycut.VERSION, "keys": sorted(keys), "wrapped": warm["wrapped"],
        "info": {
            "executed_queries": len(queries), "keys": len(keys), "wrapped": len(warm["wrapped"]),
            "warm_keys": len(set(warm["setup"]) | set(warm["back"])), "seen_only_in_a_fresh_process": sorted(cold_only),
        },
    }
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
    print(json.dumps(payload["info"]))
    return 0


if __name__ == "__main__":
    mode, root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    if mode == "one":
        sys.exit(record_one(root, out, sys.argv[4]))
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 4
    sys.exit(record_all(root, out, jobs))
