"""How small would TPC-DS query slices be on our labeled patches?

Runs inside the map-builder image, with:
    /out   work/tpcds (q.cov from record_queries.py, q_cpu.json)
    /repo  this repo (selector.py, maps/)
    /e     a folder of evaluation sets, each with commits/, tests.json, answers.json

For every patch where the shipped selector schedules test_execute_tpcds:
  - if every edit is a plain function-body statement, the slice is the TPC-DS
    queries whose coverage touches any edited statement's lines;
  - anything else (class-level, import-time, fixtures, test files) cannot be sliced.
Prints slice size, the slice's CPU (queries + class setup + imports) against the
21.5 s + worker cost of scheduling the test, and whether the test really fails.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

sys.path[:0] = ["/repo"]
os.environ["BR_SRC"] = "/sqlglot"
os.environ["BR_MAP"] = "/repo/maps/map.json.gz"
import selector as sel  # noqa: E402
from coverage import CoverageData  # noqa: E402

TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def query_lines() -> dict[str, dict[str, set[int]]]:
    data = CoverageData(basename="/out/q.cov")
    data.read()
    per: dict[str, dict[str, set[int]]] = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, "/sqlglot")
        for ln, ctxs in data.contexts_by_lineno(f).items():
            for c in ctxs:
                if c[:1] == "q":
                    per.setdefault(c, {}).setdefault(rel, set()).add(ln)
    return per


def runtime_spans(patch_text: str, selector: sel.Selector):
    """[(path, start, end)] for every edited statement, or None if any edit is not a
    plain function-body statement in sqlglot/ (then the patch cannot be sliced)."""
    spans = []
    for fp in sel.parse_patch(patch_text):
        path = fp.path
        if not (path.startswith("sqlglot/") and path.endswith(".py")) or fp.old_path != fp.new_path:
            return None
        old = selector.source(path)
        old_lines = old.split("\n")
        new = "\n".join(sel.apply_hunks(old_lines, fp.hunks))
        if sel.same_tree(old, new):
            continue
        stmts = selector.statements(path, old)
        for old_start, body in fp.hunks:
            alone = "\n".join(sel.apply_hunks(old_lines, [(old_start, body)]))
            if sel.same_tree(old, alone):
                continue
            removed, after = sel.hunk_changes(old_start, body)
            for lo, hi in [(ln, ln) for ln in removed] + [(a, a + 1) for a in after]:
                s = sel.innermost(stmts, lo, hi)
                if s is None or s.func is None or s.node is s.func or sel.is_docstring(s):
                    return None
                if selector.m.ran_at_import(path, s.start, s.end):
                    return None
                spans.append((path, s.start, s.end))
    return spans


def main() -> None:
    per_query = query_lines()
    cpu = json.load(open("/out/q_cpu.json"))
    q_ms = {k: v for k, v in cpu["query_ms"].items() if k[:1] == "q"}
    fixed_ms = cpu["setup_ms"] + cpu["import_ms"]
    executed = [q for q in per_query]
    print(f"TPC-DS queries that execute: {len(executed)}; CPU all queries {sum(q_ms.values()) / 1000:.1f}s; "
          f"class setup + imports {fixed_ms / 1000:.1f}s", file=sys.stderr)

    m = sel.Map(Path("/repo/maps/map.json.gz"))
    selector = sel.Selector(m)
    rows = []
    for set_dir in sorted(Path("/e").iterdir()):
        tests = json.loads((set_dir / "tests.json").read_text())["tests"]
        number = {t["id"]: t["number"] for t in tests}
        tpcds = number[TPCDS_ID]
        answers = json.loads((set_dir / "answers.json").read_text())["commits"]
        idx = {t: i for i, t in enumerate(m.tests)}
        for patch in sorted((set_dir / "commits").glob("*.patch")):
            if patch.stem not in answers:
                continue
            text = patch.read_text(encoding="utf-8")
            chosen = selector.select(text)
            if chosen is not sel.ALL and idx[TPCDS_ID] not in chosen:
                continue
            truth_fail = tpcds in set(answers[patch.stem]["failing"])
            spans = None if chosen is sel.ALL else runtime_spans(text, selector)
            if spans is None:
                rows.append((set_dir.name, patch.stem, "cannot slice", None, None, truth_fail))
                continue
            sl = sorted(
                q for q, files in per_query.items()
                if any(files.get(p) and any(ln in files[p] for ln in range(a, b + 1)) for p, a, b in spans)
            )
            cost = (sum(q_ms.get(q, 0) for q in sl) + fixed_ms) / 1000 if sl else 0.0
            rows.append((set_dir.name, patch.stem, f"{len(sl)}/{len(executed)}", sl, cost, truth_fail))

    print(f"{'set':12s} {'patch':14s} {'slice':>12s} {'slice CPU':>10s}  tpcds really fails")
    for set_name, c, size, sl, cost, truth in rows:
        cost_s = "-" if cost is None else f"{cost:.1f}s"
        print(f"{set_name:12s} {c:14s} {size:>12s} {cost_s:>10s}  {truth}")
    json.dump([{"set": r[0], "patch": r[1], "slice": r[3], "cost_s": r[4], "truth_fail": r[5]} for r in rows],
              open("/out/slices.json", "w"), indent=1)


if __name__ == "__main__":
    main()
