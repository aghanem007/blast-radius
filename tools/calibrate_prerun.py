"""Measure what prerun.py pays besides the tests' own time, per test module.

Run inside the submission image (same Python, packages and sqlglot), with the
kit's tests.json at /data/tests.json:
    docker run --rm -v <repo>/maps:/out -v <this file>:/c.py \
        -v <kit>/tests.json:/data/tests.json blast-radius-entry python /c.py /out/prerun_costs.json

Writes {"import_ms": {module: ms}, "first_touch_ms": {module: ms}}:
  import_ms       CPU to import the test module in the parent (after sqlglot), paid once
  first_touch_ms  extra CPU a forked child pays the first time it runs a test of that
                  module: class setup plus everything sqlglot builds lazily on first use
                  (optimizer rules, type tables, dialect instances), measured by forking
                  and running the module's cheapest test, minus that test's recorded time
The selector adds these to the tests' recorded times when deciding whether running
them ourselves beats scheduling them.

Everything that touches sqlglot or duckdb runs in the children: forking a process
that has started threads (duckdb does) can deadlock the child.
"""

from __future__ import annotations

import importlib
import json
import os
import resource
import select
import signal
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")


def cpu_self() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def cpu_children() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main(out_path: str) -> None:
    import prerun

    runner = prerun.Runner("/app/src")
    runner.warm()
    tests = json.load(open("/data/tests.json"))["tests"]
    secs = {t["id"]: t["seconds"] for t in tests}
    files = sorted({t["file"] for t in tests})
    import_ms, first_touch_ms = {}, {}
    for path in files:
        t0 = cpu_self()
        try:
            importlib.import_module(path[:-3].replace("/", "."))
        except Exception as e:
            print(f"{path}: import failed {e!r}", file=sys.stderr)
            continue
        import_ms[path] = round((cpu_self() - t0) * 1000)

    # Per test: CPU of a forked child running just that test, from a cold child.
    # This is what running it ourselves really costs. It can be far above the
    # recorded time: tests that hand work to a process pool were timed by wall
    # clock while their workers ran in parallel, and we run that work in-process.
    child_ms = {}
    cheap = [t["id"] for t in tests if t["seconds"] <= CHEAP_S and runner.runnable(t["id"]) and t["file"] in import_ms]
    for i, nodeid in enumerate(cheap):
        classes = runner.import_tests([nodeid])
        before = cpu_children()
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            try:
                prerun._run_unittests([nodeid], classes)
            finally:
                os.write(w, b"done")
                os._exit(0)
        os.close(w)
        ready, _, _ = select.select([r], [], [], 60)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            print(f"{nodeid}: child timed out; never prerun it", file=sys.stderr)
        os.waitpid(pid, 0)
        os.close(r)
        child_ms[nodeid] = round((cpu_children() - before) * 1000) if ready else 10**6
        if i % 200 == 0:
            print(f"  {i}/{len(cheap)}", file=sys.stderr, flush=True)

    json.dump({"import_ms": import_ms, "child_ms": child_ms}, open(out_path, "w"), indent=1, sort_keys=True)
    ratio = sorted(((child_ms[n] / max(1, secs[n] * 1000), n) for n in child_ms), reverse=True)[:5]
    print(f"modules {len(import_ms)}, tests {len(child_ms)}; worst CPU/recorded: {ratio}", file=sys.stderr)


CHEAP_S = 1.5


if __name__ == "__main__":
    main(sys.argv[1])
