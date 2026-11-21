"""Build maps/attr.json.gz: for every class attribute of sqlglot, the tests that read it.

Inputs (both recorded with tools/build_map/attrrec.py at the pinned commit):
  <warm dir>/attr.*.json   the whole suite in a few processes
  <isolated.json>          every test alone in a fresh process (attr_isolated.py)
The map is the union. The isolated run is what makes it complete (no warm caches);
the warm run adds anything a test only reads when others ran before it.

Crediting in the warm run: a read during a test's call is that test's; a read during
setup is credited to every test of the same class (class fixtures run inside the first
test's setup); a read while a test file is collected is credited to the whole file.

    python tools/build_map/build_attr_map.py <warm dir> <isolated.json> <tests.json> maps/attr.json.gz

Output: {"version", "tests": [nodeid], "wrapped": [key], "readers": {key: set id},
"sets": [[test index]], "always": [test index]}. `wrapped` is every attribute the
recorder could observe; an attribute not in it must not be reasoned about. `always`
are tests that did not pass under the isolated recording: they run for every
class-level change.
"""

from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import sys

warm_dir, iso_path, tests_path, out_path = sys.argv[1:5]
tests = [t["id"] for t in json.load(open(tests_path))["tests"]]
index = {t: i for i, t in enumerate(tests)}
by_class, by_file = collections.defaultdict(list), collections.defaultdict(list)
for t in tests:
    by_file[t.split("::", 1)[0]].append(index[t])
    by_class[t.rsplit("::", 1)[0]].append(index[t])

readers: dict[str, set[int]] = collections.defaultdict(set)
wrapped: set[str] = set()
warm_files = glob.glob(os.path.join(warm_dir, "attr.*.json"))
for path in warm_files:
    raw = json.load(open(path))
    wrapped |= set(raw["wrapped"])
    for ctx, keys in raw["reads"].items():
        if ctx.startswith("collect|"):
            who = by_file.get(ctx.split("|", 1)[1], [])
        else:
            nodeid, phase = ctx.rsplit("|", 1)
            if nodeid not in index:
                continue
            who = by_class[nodeid.rsplit("::", 1)[0]] if phase == "setup" else [index[nodeid]]
        for k in keys:
            readers[k].update(who)

iso = json.load(open(iso_path))
always = sorted(index[n] for n, row in iso.items() if not row["ok"] and n in index)
missing = [t for t in tests if t not in iso]
for nodeid, row in iso.items():
    if nodeid in index:
        for k in row["reads"]:
            readers[k].add(index[nodeid])

unknown = sorted(k for k in readers if k not in wrapped)
set_ids: dict[tuple, int] = {}
sets: list[list[int]] = []
out_readers = {}
for k in sorted(readers):
    key = tuple(sorted(readers[k]))
    if key not in set_ids:
        set_ids[key] = len(sets)
        sets.append(list(key))
    out_readers[k] = set_ids[key]
payload = {
    "version": 1, "tests": tests, "wrapped": sorted(wrapped | set(readers)),
    "readers": out_readers, "sets": sets, "always": always,
}
with gzip.open(out_path, "wt", encoding="utf-8") as f:
    json.dump(payload, f, separators=(",", ":"))
print(json.dumps({
    "tests": len(tests), "warm_files": len(warm_files), "isolated_tests": len(iso), "isolated_missing": len(missing),
    "wrapped": len(payload["wrapped"]), "attributes_with_readers": len(out_readers), "distinct_reader_sets": len(sets),
    "always": [tests[i] for i in always][:10], "always_count": len(always),
    "read_but_not_wrapped": len(unknown), "bytes": os.path.getsize(out_path),
}))
