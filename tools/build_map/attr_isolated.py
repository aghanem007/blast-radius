"""Run every test alone, in its own fresh process, with the attrrec plugin.

A fresh process has no warm caches and nothing imported lazily by an earlier test, so
each test itself performs every class-attribute read its outcome depends on. That is
the property the attribute map needs (the warm full-suite run alone credits cached
work to whichever test came first).

Runs INSIDE the map-builder image (br-mapper):
    python /t/attr_isolated.py <tests.json> <out dir> [--jobs N]
Writes <out dir>/isolated.json: {nodeid: {"reads": [keys], "ok": bool}} and
<out dir>/wrapped.json. Resumable: finished tests are kept in <out dir>/parts/.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

tests_path, out_dir = sys.argv[1], sys.argv[2]
jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else os.cpu_count() or 4
parts = os.path.join(out_dir, "parts")
os.makedirs(parts, exist_ok=True)
nodeids = [t["id"] for t in json.load(open(tests_path))["tests"]]


def one(nodeid: str) -> tuple[str, dict]:
    tag = hashlib.sha1(nodeid.encode()).hexdigest()[:16]
    done = os.path.join(parts, tag + ".json")
    if os.path.exists(done):
        return nodeid, json.load(open(done))
    work = os.path.join(parts, tag)
    os.makedirs(work, exist_ok=True)
    env = dict(os.environ, MAP_OUT=work, ATTR_WHOLE=nodeid, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "attrrec", "-p", "no:cacheprovider", "--timeout", "900"],
            env=env, capture_output=True, text=True, errors="replace", timeout=1200,
        )
        code, tail = p.returncode, (p.stdout or "")[-300:]
    except subprocess.TimeoutExpired:
        code, tail = -1, "timeout"
    reads, wrapped = [], []
    try:
        raw = json.load(open(os.path.join(work, "attr.main.json")))
        reads = sorted({k for keys in raw["reads"].values() for k in keys})
        wrapped = raw["wrapped"]
    except Exception as e:  # noqa: BLE001
        tail += f" | no attr file: {e!r}"
    row = {"reads": reads, "ok": code == 0, "wrapped_n": len(wrapped), "tail": "" if code == 0 else tail}
    if wrapped and not os.path.exists(os.path.join(out_dir, "wrapped.json")):
        json.dump(wrapped, open(os.path.join(out_dir, "wrapped.json"), "w"))
    json.dump(row, open(done, "w"))
    return nodeid, row


out = {}
with ThreadPoolExecutor(jobs) as pool:
    for i, (nodeid, row) in enumerate(pool.map(one, nodeids)):
        out[nodeid] = row
        if i % 100 == 0:
            print(f"{i}/{len(nodeids)}", file=sys.stderr, flush=True)
json.dump(out, open(os.path.join(out_dir, "isolated.json"), "w"))
bad = [n for n, r in out.items() if not r["ok"]]
print(json.dumps({"tests": len(out), "not_ok": len(bad), "first_not_ok": bad[:5],
                  "median_reads": sorted(len(r["reads"]) for r in out.values())[len(out) // 2]}))
