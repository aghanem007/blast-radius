"""Is the map missing code that a test only runs when caches start empty?

The map was recorded with many tests per process, in file order. A test that reuses
something an earlier test computed and cached (a shared schema's lookup cache, a
lazily built table) never runs that code itself, so the map does not credit it, but
a patch there still breaks it. Here each test runs alone in a fresh process; any
sqlglot line it runs that the map does not credit to it is a potential miss.

Run inside the map-builder image, with the map and kit tests.json mounted:
    python /t/cold_check.py <test file> [<test file> ...]
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from coverage import CoverageData

REPO = "/sqlglot"


def run_one(i_nodeid):
    i, nodeid = i_nodeid
    out = f"/tmp/cold/{i}"
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ, MAP_OUT=out, PYTHONPATH="/sqlglot:/t")
    subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, "-p", "recorder", "-p", "no:cacheprovider", "-q", "-x"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    lines = {}
    for path in glob.glob(f"{out}/cov.*"):
        data = CoverageData(basename=path)
        data.read()
        for f in data.measured_files():
            rel = os.path.relpath(f, REPO)
            if not rel.startswith("sqlglot/"):
                continue
            for ln, ctxs in data.contexts_by_lineno(f).items():
                if any(c.endswith(("|setup", "|run", "|teardown")) for c in ctxs):
                    lines.setdefault(rel, set()).add(ln)
    return nodeid, lines


def main() -> None:
    m = json.load(gzip.open("/maps/map.json.gz", "rt"))
    sets = [set(s) for s in m["sets"]]
    idx = {t: i for i, t in enumerate(m["tests"])}
    files = sys.argv[1:]
    nodeids = [t for t in m["tests"] if t.split("::")[0] in files]
    print(f"{len(nodeids)} tests, each in a fresh process", file=sys.stderr, flush=True)
    by_file_line = {f: dict(pairs) for f, pairs in m["lines"].items()}
    import_lines = {f: set(v) for f, v in m["import_lines"].items()}
    gaps, counts = {}, {}
    with ThreadPoolExecutor(12) as pool:
        for nodeid, lines in pool.map(run_one, enumerate(nodeids)):
            i = idx[nodeid]
            missing = []
            for f, lns in lines.items():
                for ln in lns:
                    # Lines that run while a module loads are already "run everything"
                    # in the selector; a lone test merely triggers lazy imports later.
                    if ln in import_lines.get(f, ()):
                        continue
                    s = by_file_line.get(f, {}).get(ln)
                    if s is None or i not in sets[s]:
                        missing.append(f"{f}:{ln}")
            if missing:
                gaps[nodeid] = missing
                for g in missing:
                    counts[g] = counts.get(g, 0) + 1
    print(json.dumps({
        "tests": len(nodeids), "tests_with_gaps": len(gaps),
        "gap_lines_total": sum(len(v) for v in gaps.values()),
        "distinct_gap_lines": len(counts),
        "per_test": {k: len(v) for k, v in gaps.items()},
        "examples": {k: v[:25] for k, v in gaps.items()},
        "most_common": sorted(counts.items(), key=lambda kv: -kv[1])[:60],
    }, indent=1))


if __name__ == "__main__":
    main()
