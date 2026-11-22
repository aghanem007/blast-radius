"""Build maps/cmp.json.gz from the comparison recordings (cmprec.py).

Inputs: one or more recordings of every test alone (cmp_isolated.py) and any number of whole-suite runs with
the same plugin ("warm" runs: what a test evaluates once caches are filled, in some test order).

    python tools/build_map/build_cmp_map.py <out.json.gz> --iso <parts dir> [--iso ...] [--warm <dir> ...]

The map is the UNION of everything seen, per test and comparison site. On top of that, a site is FLAGGED,
and never used by the selector, when the recordings disagree about it:
  - two recordings of the same test alone differ there (values that change from run to run: hashes, memory
    addresses, the iteration order of a set), or
  - a whole-suite run shows, for some test, a relation that the test never shows alone (the comparison
    depends on what ran before: a cache, a lazily loaded module).
Recordings are a sample of the ways the suite can run; the flag is how that sample is allowed to say "this
comparison is not a property of the test".

Output (version 2):
  tests     node ids recorded alone and passing in EVERY isolated recording. Only these can be dropped.
  relsets   distinct sets of relation codes (see cmprec.py)
  testsets  distinct sets of indexes into tests, delta-encoded
  sites     {"sqlglot/x.py": {"line:col:end_line:end_col": [[relset index, testset index], ...]}}
  flagged   {"sqlglot/x.py": ["line:col:end_line:end_col", ...]}
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import sys

args = sys.argv[1:]
out_path = args[0]
iso_dirs = [args[i + 1] for i, a in enumerate(args) if a == "--iso"]
warm_dirs = [args[i + 1] for i, a in enumerate(args) if a == "--warm"]
if not iso_dirs:
    sys.exit(__doc__)


def load_iso(d):
    ok, bad = {}, []
    for f in glob.glob(os.path.join(d, "*.json")):
        row = json.load(open(f))
        if row["ok"]:
            ok[row["nodeid"]] = {s: frozenset(c) for s, c in row["sites"].items()}
        else:
            bad.append(row["nodeid"])
    return ok, bad


isos = []
for d in iso_dirs:
    ok, bad = load_iso(d)
    print(f"isolated {d}: {len(ok)} tests recorded, {len(bad)} not ok {bad[:3]}", file=sys.stderr)
    isos.append(ok)
recorded = set(isos[0])
for iso in isos[1:]:
    recorded &= set(iso)

flagged: set[str] = set()
per_test: dict[str, dict[str, set[int]]] = {}
for t in recorded:
    mine = per_test[t] = {}
    for s in set().union(*(iso[t].keys() for iso in isos)):
        seen = [iso[t].get(s) for iso in isos]
        if any(x != seen[0] for x in seen[1:]):
            flagged.add(s)  # not the same from one recording of this test to the next
        mine[s] = set().union(*(x for x in seen if x is not None))
print(f"unstable between isolated recordings: {len(flagged)} sites", file=sys.stderr)

by_class: dict[str, list[str]] = {}
for nodeid in per_test:
    by_class.setdefault("::".join(nodeid.split("::")[:-1]), []).append(nodeid)
order_dependent: set[str] = set()
for wd in warm_dirs:
    added = 0
    for f in glob.glob(os.path.join(wd, "cmp.*.json")):
        for ctx, found in json.load(open(f))["ctx"].items():
            if ctx.endswith("|call"):
                targets = [ctx[: -len("|call")]]
            elif ctx.startswith("class|"):
                targets = by_class.get(ctx[len("class|"):], [])  # a class's setup: every test of it
            else:
                continue
            for nodeid in targets:
                mine = per_test.get(nodeid)
                if mine is None:
                    continue
                for site, codes in found.items():
                    have = mine.setdefault(site, set())
                    new = set(codes) - have
                    if new:
                        order_dependent.add(site)
                        have |= new
                        added += len(new)
    print(f"warm {wd}: {added} relations a test never showed alone; order-dependent sites so far: {len(order_dependent)}", file=sys.stderr)
flagged |= order_dependent

tests = sorted(per_test)
index = {n: i for i, n in enumerate(tests)}
by_site: dict[str, dict[tuple, list[int]]] = {}
for nodeid, sites in per_test.items():
    i = index[nodeid]
    for site, codes in sites.items():
        by_site.setdefault(site, {}).setdefault(tuple(sorted(codes)), []).append(i)

relsets: dict[tuple, int] = {}
testsets: dict[tuple, int] = {}
out_sites: dict[str, dict[str, list]] = {}
for site, groups in by_site.items():
    path, rest = site.split(":", 1)
    rows = []
    for codes, members in groups.items():
        members = tuple(sorted(members))
        rows.append([relsets.setdefault(codes, len(relsets)), testsets.setdefault(members, len(testsets))])
    out_sites.setdefault(path, {})[rest] = sorted(rows)
out_flagged: dict[str, list[str]] = {}
for site in sorted(flagged):
    path, rest = site.split(":", 1)
    out_flagged.setdefault(path, []).append(rest)


def delta(members):
    out, prev = [], 0
    for x in members:
        out.append(x - prev)
        prev = x
    return out


payload = {
    "version": 2,
    "tests": tests,
    "relsets": [list(k) for k, _ in sorted(relsets.items(), key=lambda kv: kv[1])],
    "testsets": [delta(k) for k, _ in sorted(testsets.items(), key=lambda kv: kv[1])],
    "sites": out_sites,
    "flagged": out_flagged,
}
with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as f:
    json.dump(payload, f, separators=(",", ":"), sort_keys=True)
print(json.dumps({
    "tests": len(tests), "sites": len(by_site), "flagged": len(flagged), "flagged_sites": sorted(flagged)[:12],
    "relsets": len(relsets), "testsets": len(testsets), "bytes": os.path.getsize(out_path),
}))
