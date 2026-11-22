"""Run every test alone, in its own fresh process, with the cmprec plugin.

A fresh process has no warm caches and nothing imported lazily by an earlier test, so each test
itself evaluates every comparison its outcome depends on (same reason as attr_isolated.py).

Runs INSIDE the map-builder image (br-mapper), from the sqlglot checkout:
    PYTHONPYCACHEPREFIX=<empty shared folder> python /t/cmp_isolated.py <tests.json> <out dir> [--jobs N] [--only substring]
Writes <out dir>/parts/<tag>.json per test: {"nodeid", "ok", "sites": {site: [relation codes]}, "tail"}.
Resumable: finished tests are kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

tests_path, out_dir = sys.argv[1], sys.argv[2]
jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else os.cpu_count() or 4
only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
parts = os.path.join(out_dir, "parts")
os.makedirs(parts, exist_ok=True)
tests = json.load(open(tests_path))["tests"]
# slowest first, so the giants do not all end up at the tail
nodeids = [t["id"] for t in sorted(tests, key=lambda t: -t["seconds"]) if only is None or only in t["id"]]


def one(nodeid: str):
    tag = hashlib.sha1(nodeid.encode()).hexdigest()[:16]
    done = os.path.join(parts, tag + ".json")
    if os.path.exists(done):
        return nodeid, True
    work = os.path.join(parts, tag)
    os.makedirs(work, exist_ok=True)
    env = dict(os.environ, MAP_OUT=work, CMP_WHOLE=nodeid)
    env.pop("PYTHONDONTWRITEBYTECODE", None)  # the rewritten bytecode must be cached (PYTHONPYCACHEPREFIX)
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "cmprec", "-p", "no:cacheprovider", "--timeout", "3000"],
            env=env, capture_output=True, text=True, errors="replace", timeout=3600,
        )
        code, tail = p.returncode, ((p.stdout or "") + (p.stderr or ""))[-400:]
    except subprocess.TimeoutExpired:
        code, tail = -1, "timeout"
    sites = {}
    try:
        raw = json.load(open(os.path.join(work, "cmp.main.json")))
        for ctx, found in raw["ctx"].items():
            for site, codes in found.items():
                sites.setdefault(site, set()).update(codes)
    except Exception as e:  # noqa: BLE001
        code, tail = (code if code != 0 else -2), tail + f" | no cmp file: {e!r}"
    row = {"nodeid": nodeid, "ok": code == 0, "seconds": round(time.time() - t0, 1),
           "sites": {s: sorted(c) for s, c in sites.items()}, "tail": "" if code == 0 else tail}
    tmp = done + ".tmp"
    json.dump(row, open(tmp, "w"))
    os.replace(tmp, done)
    shutil.rmtree(work, ignore_errors=True)
    return nodeid, code == 0


bad = []
with ThreadPoolExecutor(jobs) as pool:
    for i, (nodeid, ok) in enumerate(pool.map(one, nodeids)):
        if not ok:
            bad.append(nodeid)
        if i % 100 == 0:
            print(f"{i}/{len(nodeids)}", file=sys.stderr, flush=True)
print(json.dumps({"tests": len(nodeids), "not_ok": len(bad), "first_not_ok": bad[:8]}))
