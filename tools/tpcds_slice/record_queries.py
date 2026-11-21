"""Per-query coverage and CPU for test_execute_tpcds / test_execute_tpch.

Experiment (branch exp/tpcds-slice): can we run only the TPC-DS queries that reach
an edited line, instead of scheduling the whole 21.5 s test?

Each query's work happens in three places: parse_one(sql) in the test process, the
pool task that executes it, and subtestHelper(i) that compares against duckdb. All
three are credited to that query ("q<i>" for TPC-DS, "h<i>" for TPC-H). sqlglot's
per-process caches are reset before every query in the coverage pass (the C3
lesson), so a query is credited with the cached code it depends on.

Run inside the map-builder image:
    python /x/record_queries.py cov /out     -> /out/q.cov (coverage contexts)
    python /x/record_queries.py cpu /out     -> /out/q_cpu.json (CPU per query, warm)
"""

from __future__ import annotations

import json
import os
import resource
import sys
import unittest

sys.path[:0] = ["/sqlglot", "/t"]
os.environ.setdefault("SKIP_INTEGRATION", "0")

import recorder  # noqa: E402  (map recorder: in-process pool, cache reset)

recorder._patch_process_pools()


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def main(mode: str, out: str) -> None:
    cov = None
    t_import = cpu()
    recorder._import_everything()
    import tests.test_executor as te

    import_ms = round((cpu() - t_import) * 1000)
    if mode == "cov":
        import coverage

        cov = coverage.Coverage(data_file=os.path.join(out, "q.cov"), source=["/sqlglot/sqlglot"], config_file=False)
        cov.start()

    state = {"prefix": "", "k": -1, "ctx": None, "t": cpu()}
    spent: dict[str, float] = {}

    def switch(ctx: str | None) -> None:
        now = cpu()
        if state["ctx"] is not None:
            spent[state["ctx"]] = spent.get(state["ctx"], 0.0) + (now - state["t"])
        state["ctx"], state["t"] = ctx, now
        if cov is not None:
            if ctx is not None and ctx.startswith(("q", "h")):
                recorder._cold_caches()
            cov.switch_context(ctx or "")

    real_parse = te.parse_one

    def parse_wrap(sql, *args, **kwargs):
        state["k"] += 1
        switch(f"{state['prefix']}{state['k']}")
        return real_parse(sql, *args, **kwargs)

    te.parse_one = parse_wrap
    real_helper = te.TestExecutor.subtestHelper

    def helper_wrap(self, i, table, tpch=True):
        switch(f"{'h' if tpch else 'q'}{i}")
        try:
            return real_helper(self, i, table, tpch=tpch)
        finally:
            switch("between")

    te.TestExecutor.subtestHelper = helper_wrap

    switch("setup")
    te.TestExecutor.setUpClass()
    switch("between")
    results = {}
    for name, prefix in (("test_execute_tpcds", "q"), ("test_execute_tpch", "h")):
        state["prefix"], state["k"] = prefix, -1
        result = unittest.TestResult()
        te.TestExecutor(name).run(result)
        results[name] = {"failures": len(result.failures), "errors": len(result.errors)}
        switch("between")
    switch(None)
    if cov is not None:
        cov.stop()
        cov.save()
    meta = {
        "import_ms": import_ms,
        "setup_ms": round(spent.get("setup", 0) * 1000),
        "query_ms": {k: round(v * 1000) for k, v in spent.items() if k[:1] in "qh"},
        "results": results,
        "executed_queries": sorted(k for k in spent if k[:1] in "qh"),
    }
    with open(os.path.join(out, f"q_{mode}.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(json.dumps({k: v for k, v in meta.items() if k != "query_ms"} | {"queries_timed": len(meta["query_ms"])}), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
