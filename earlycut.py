"""Early cutoff for tests/test_executor.py::TestExecutor::test_execute_tpcds (21.5 s).

The test has two halves. The FRONT half is cheap: parse 99 TPC-DS queries, send each
to a pool worker (pickle), and for the 57 it executes, optimize and plan it, plus
transpile the SQL to DuckDB for the expected answer. The BACK half is expensive: the
Python executor runs each plan over the fixture tables and the result is compared
with DuckDB's.

If a patch only touches code the back half never runs, and the front half hands the
back half byte-for-byte the same things as the pinned commit does (same pickled ASTs,
same DuckDB SQL, same plans), then the back half does exactly what it did before and
the test's outcome cannot change. That is the "early cutoff" of incremental builds:
unchanged intermediate result, downstream work not redone.

So this module re-runs the front half against the patched code, by calling the real
sqlglot functions the test calls (the real `sqlglot.executor.execute`, with only the
executor class replaced by a stub that captures the plan), and compares fingerprints
with the pinned commit's.

The same idea covers two more cases, by actually executing part of the back half:
  - a patch in code the back half runs: only the queries whose execution reaches the
    patched function are re-executed (the others cannot change), and each result is
    compared with the pinned commit's result for that query;
  - a front-half patch that changes a few queries' plans: those queries are executed,
    because a different plan often still gives the same rows.
A query whose result is identical gives the test the same DataFrame to compare with
DuckDB's, so its subtest passes exactly as before.

Anything unexpected (exception, timeout, any difference) means "keep the test".

Three roles:
  python earlycut.py baseline <src> <out.json>      fingerprints of the pinned commit
  python earlycut.py check <src> <base.json> <job>  child: patched code vs baseline
  import earlycut                                   used by selector.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"
VERSION = 3  # bump when the harness or the fingerprint changes: old baselines are then ignored


# --------------------------------------------------------------- patched imports


def install_overrides(src: str, overrides: dict[str, str]) -> None:
    """Make `import` use the patched source for the given files (paths relative to src).

    Nothing is written to disk: a path hook hands the patched text to the compiler for
    exactly those files, every other module loads normally."""
    if not overrides:
        return
    import importlib.machinery as mach

    patched = {os.path.normcase(os.path.abspath(os.path.join(src, p))): text for p, text in overrides.items()}

    class Loader(mach.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            text = patched.get(os.path.normcase(os.path.abspath(path)))
            if text is None:
                return super().get_code(fullname)
            return compile(text, path, "exec", dont_inherit=True)

        def get_source(self, fullname):
            text = patched.get(os.path.normcase(os.path.abspath(self.get_filename(fullname))))
            return text if text is not None else super().get_source(fullname)

    hook = mach.FileFinder.path_hook(
        (mach.ExtensionFileLoader, mach.EXTENSION_SUFFIXES),
        (Loader, mach.SOURCE_SUFFIXES),
        (mach.SourcelessFileLoader, mach.BYTECODE_SUFFIXES),
    )
    sys.path_hooks.insert(0, hook)
    sys.path_importer_cache.clear()


# ------------------------------------------------------------------ fingerprint


class Fingerprinter:
    """A deterministic text dump of an object graph, hashed.

    Two parts. (1) Values and structure: every object is written out in full wherever
    it is referenced; lists and dicts keep their order (order is behaviour); the
    elements of a set are written out one by one and their texts sorted, so the dump
    never depends on memory addresses. (2) Aliasing: per type, how many distinct
    objects there are and how many times they are reached. Code that later mutates a
    node behaves differently when two references are one object instead of two equal
    copies, and that changes the distinct count."""

    SKIP = frozenset(("parent", "_hash", "__dict__", "__weakref__"))
    # A plan step's `dependents` is the reverse of `dependencies`, which is dumped in
    # full. Following both directions revisits the whole plan from every step, so the
    # reverse edges are written as labels only.
    SHALLOW = frozenset(("dependents",))

    def __init__(self) -> None:
        import enum

        self.enum = enum.Enum
        self.fields: dict[type, list[str]] = {}
        self.ids: dict[type, set[int]] = {}
        self.visits: dict[type, int] = {}

    def attrs(self, obj) -> list[str]:
        t = type(obj)
        f = self.fields.get(t)
        if f is None:
            found = set()
            for klass in t.__mro__:
                slots = getattr(klass, "__slots__", ())
                found.update((slots,) if isinstance(slots, str) else slots)
            f = self.fields[t] = sorted(found - self.SKIP)
        d = getattr(obj, "__dict__", None)
        if not d:
            return f
        return sorted(set(f) | (set(d) - self.SKIP))

    def dump(self, obj, path: set[int], out: list[str]) -> None:
        t = type(obj)
        if obj is None or t in (bool, int, float, complex, str, bytes):
            out.append(f"{t.__name__}:{obj!r};")
            return
        if isinstance(obj, self.enum):
            out.append(f"<enum {t.__module__}.{t.__qualname__}.{obj.name}>")
            return
        if isinstance(obj, type):
            out.append(f"<type {obj.__module__}.{obj.__qualname__}>")
            return
        oid = id(obj)
        if oid in path:
            out.append("@cycle;")
            return
        seen = self.ids.get(t)
        if seen is None:
            seen = self.ids[t] = set()
        seen.add(oid)
        self.visits[t] = self.visits.get(t, 0) + 1
        path.add(oid)
        if isinstance(obj, (list, tuple)):
            out.append(f"{t.__name__}[")
            for x in obj:
                self.dump(x, path, out)
            out.append("]")
        elif isinstance(obj, dict):
            out.append(f"{t.__name__}{{")
            for k, v in obj.items():
                self.dump(k, path, out)
                self.dump(v, path, out)
            out.append("}")
        elif isinstance(obj, (set, frozenset)):
            texts = []
            for x in obj:
                sub: list[str] = []
                self.dump(x, path, sub)
                texts.append("".join(sub))
            out.append(f"{t.__name__}{{" + "|".join(sorted(texts)) + "}")
        elif callable(obj) and hasattr(obj, "__qualname__"):
            out.append(f"<callable {getattr(obj, '__module__', '')}.{obj.__qualname__} ")
            for cell in getattr(obj, "__closure__", None) or ():
                try:
                    self.dump(cell.cell_contents, path, out)
                except ValueError:
                    out.append("<empty cell>")
            self.dump(getattr(obj, "__defaults__", None), path, out)
            out.append(">")
        else:
            out.append(f"<{t.__module__}.{t.__qualname__} ")
            names = self.attrs(obj)
            if not names:
                # No fields we can list (a date, a Decimal, a range...): its repr is its
                # value. An object whose repr shows a memory address never matches the
                # baseline, which errs on the side of keeping the test.
                out.append(repr(obj))
            for n in names:
                try:
                    v = getattr(obj, n)
                except AttributeError:
                    continue
                out.append(n + "=")
                if n in self.SHALLOW and isinstance(v, (set, frozenset, list, tuple)):
                    labels = sorted(f"{type(x).__qualname__}:{getattr(x, 'name', None)!r}" for x in v)
                    out.append(f"{type(v).__name__}{labels!r};")
                else:
                    self.dump(v, path, out)
            out.append(">")
        path.discard(oid)

    def digest(self, obj) -> str:
        self.ids.clear()
        self.visits.clear()
        out: list[str] = []
        self.dump(obj, set(), out)
        aliasing = sorted(
            (f"{t.__module__}.{t.__qualname__}", len(ids), self.visits[t]) for t, ids in self.ids.items()
        )
        out.append(f"|aliasing:{aliasing!r}")
        return hashlib.sha1("".join(out).encode("utf-8", "surrogatepass")).hexdigest()


def sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------------- harness


def front_half(src: str, on_item=None, hooks=None, only=None) -> dict:
    """Run the front half of test_execute_tpcds exactly as the test does, in the
    test's order, and fingerprint what it hands to the back half.

    on_item(key, value) is called for each fingerprint as it is produced and may
    raise StopIteration to end early. hooks.back(i, table names, plan) is called for
    every executed query once its plan exists. only: restrict to these query numbers."""
    import pickle

    if src not in sys.path:
        sys.path.insert(0, src)

    import sqlglot.executor as executor_module
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    class Capture:
        """Stands in for PythonExecutor: the plan it is given is what the back half gets."""

        last = None

        def __init__(self, env=None, tables=None):
            pass

        def execute(self, plan):
            Capture.last = plan
            return None

    executor_module.PythonExecutor = Capture
    execute = executor_module.execute
    # The fixture tables' rows only matter to the back half; the front half passes the
    # Table objects through untouched.
    stub_tables = {name: Table(columns=columns, rows=[]) for name, columns in TPCDS_SCHEMA.items()}
    fingerprinter = Fingerprinter()
    items: dict[str, str] = {}

    def emit(key: str, value: str) -> None:
        items[key] = value
        if on_item is not None:
            on_item(key, value)

    def guarded(key: str, fn):
        """An exception is an outcome too: it becomes the fingerprint."""
        try:
            return fn()
        except StopIteration:
            raise
        except BaseException as e:  # noqa: BLE001 - SystemExit/KeyboardInterrupt included on purpose
            emit(key, f"!{type(e).__name__}: {e}"[:300])
            return None

    sqls = [(i, meta, sql) for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs(FIXTURE)) if only is None or i in only]
    try:
        # 1. the main process: parse every query and pickle it for the pool
        blobs: dict = {}
        for i, meta, sql in sqls:
            blob = guarded(f"{i}.ast", lambda: pickle.dumps(parse_one(sql)))
            blobs[i] = blob
            if blob is not None:
                emit(f"{i}.ast", sha(blob))

        # 2. the main process again: the SQL DuckDB runs for the expected answer. The
        #    test does this after the workers return, in another process than they run
        #    in, so its place here is free; it is cheap, and a difference found here
        #    saves the expensive step below. (The test skips it for a query whose
        #    worker raised; doing it for every executed query only checks more.)
        for i, meta, sql in sqls:
            if meta.get("execute"):
                duck = guarded(f"{i}.duck", lambda: transpile(sql, write="duckdb")[0])
                if duck is not None:
                    emit(f"{i}.duck", sha(duck))

        # 3. a pool worker: unpickle, and for the executed queries optimize and plan
        for i, meta, sql in sqls:
            if blobs[i] is None:
                continue
            expression = guarded(f"{i}.load", lambda: pickle.loads(blobs[i]))
            if expression is None:
                continue
            emit(f"{i}.load", "ok")
            if not meta.get("execute"):
                continue

            def worker():
                tables = {}
                for t in find_tables(expression):
                    name = t.name
                    tables[name] = stub_tables[name]
                Capture.last = None
                execute(expression, schema=TPCDS_SCHEMA, tables=tables)
                return list(tables), Capture.last

            got = guarded(f"{i}.plan", worker)
            if got is None:
                continue
            names, plan = got
            emit(f"{i}.tables", ",".join(names))
            digest = guarded(f"{i}.plan", lambda: fingerprinter.digest(plan))
            if digest is not None:
                emit(f"{i}.plan", digest)
            if hooks is not None:
                hooks.back(i, names, plan)
    except StopIteration:
        pass
    return items


class BackHalf:
    """The test's back half for one query: the real Python executor over the real
    fixture tables, and a fingerprint of the result the test would compare with DuckDB's.

    Tables are loaded the way the test's setUpClass loads them, one file at a time, on
    first use."""

    def __init__(self, src: str) -> None:
        if src not in sys.path:
            sys.path.insert(0, src)
        from sqlglot.executor.python import PythonExecutor  # the real class, not front_half's stub
        from sqlglot.executor.table import Table, ensure_tables
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA

        self.PythonExecutor, self.Table, self.ensure_tables = PythonExecutor, Table, ensure_tables
        self.dir, self.schema = os.path.join(FIXTURES_DIR, "optimizer", "tpc-ds"), TPCDS_SCHEMA
        self.tables: dict = {}
        self.fingerprinter = Fingerprinter()

    def table(self, name: str):
        found = self.tables.get(name)
        if found is None:
            import ast
            import csv
            import gzip

            columns = self.schema[name]
            with gzip.open(os.path.join(self.dir, f"{name}.csv.gz"), "rt", newline="") as f:
                reader = csv.reader(f, delimiter="|")
                next(reader)
                rows, ctypes = [], []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            found = self.tables[name] = self.Table(columns=columns, rows=rows)
        return found

    def load_all(self) -> None:
        for name in self.schema:
            self.table(name)

    def run(self, names, plan) -> str:
        import pickle

        tables_ = self.ensure_tables({name: self.table(name) for name in names}, dialect=None)
        result = self.PythonExecutor(tables=tables_).execute(plan)
        result = pickle.loads(pickle.dumps(result))  # the worker sends the Table back to the test
        # Exactly what the test builds its DataFrame from. Values are dumped with their
        # types, so 1, 1.0 and True are three different results.
        return self.fingerprinter.digest((tuple(result.columns), result.rows))


class _Plans:
    def __init__(self) -> None:
        self.plans: dict = {}

    def back(self, i, names, plan) -> None:
        self.plans[i] = (names, plan)


def cmd_baseline(src: str, out_path: str) -> int:
    cpu0 = time.process_time()
    hooks = _Plans()
    items = front_half(src, hooks=hooks)
    front_cpu = time.process_time() - cpu0
    bad = [k for k, v in items.items() if v.startswith("!")]
    if bad:
        print(f"baseline has errors: {bad[:5]}", file=sys.stderr)
        return 1
    payload = {
        "version": VERSION, "python": sys.version, "hashseed": os.environ.get("PYTHONHASHSEED"),
        "cpu_s": round(front_cpu, 3), "items": items,
    }
    # The back half, once, for the result fingerprints and each query's cost. If it
    # fails here the baseline still serves the front-half check.
    try:
        back = BackHalf(src)
        t = time.process_time()
        back.load_all()
        setup_cpu = time.process_time() - t
        results, back_cpu = {}, {}
        for i in sorted(hooks.plans):
            t = time.process_time()
            results[f"{i}.result"] = back.run(*hooks.plans[i])
            back_cpu[str(i)] = round(time.process_time() - t, 4)
        items.update(results)
        payload["back_cpu"], payload["setup_cpu"] = back_cpu, round(setup_cpu, 3)
    except BaseException as e:  # noqa: BLE001
        print(f"baseline: back half failed ({type(e).__name__}: {e}); front-half check only", file=sys.stderr)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
    os.replace(tmp, out_path)
    print(
        f"baseline: {len(items)} fingerprints, front {payload['cpu_s']} s cpu, "
        f"back {round(sum(payload.get('back_cpu', {}).values()), 2)} s cpu over {len(payload.get('back_cpu', {}))} queries",
        file=sys.stderr,
    )
    return 0


def cmd_check(src: str, base_path: str, job_path: str) -> int:
    """Child process. Prints one JSON line: {"same": bool, ...}.

    job: overrides {path: patched source}; front (bool): the patch touches front-half
    code, so the whole front half is re-run and compared; queries: executed queries
    whose back half reaches the patched code, re-executed and their results compared;
    max_plan_diffs / plan_diff_cpu_s: when a query's plan differs, its back half is
    re-executed too (a different plan can still give the same result), up to this many
    queries and this much baseline CPU."""
    real_stdout = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # nothing the patched code prints may reach our answer
    sys.stdout = sys.stderr
    verdict = {"same": False, "why": "crash"}
    try:
        with open(base_path, encoding="utf-8") as f:
            raw = json.load(f)
        base, back_cpu = raw["items"], raw.get("back_cpu", {})
        job = json.loads(sys.stdin.read()) if job_path == "-" else json.load(open(job_path, encoding="utf-8"))
        cpu_limit = job.get("cpu_limit_s")
        if cpu_limit:
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_CPU, (int(cpu_limit) + 1, int(cpu_limit) + 2))
            except Exception:  # noqa: BLE001 - no rlimit here: the parent's wall timeout still applies
                pass
        install_overrides(src, job["overrides"])
        queries = {int(q) for q in job.get("queries", ())}
        only = None if job.get("front", True) else queries
        max_diffs, diff_budget = int(job.get("max_plan_diffs", 0)), float(job.get("plan_diff_cpu_s", 0))
        if (queries or max_diffs) and not back_cpu:
            raise RuntimeError("baseline has no back-half results")
        first_diff: list = []
        plan_diffs: list[int] = []
        hooks = _Plans()

        def on_item(key: str, value: str) -> None:
            if base.get(key) == value:
                return
            number, kind = key.split(".")
            if kind == "plan" and not value.startswith("!") and len(plan_diffs) < max_diffs:
                spent = sum(back_cpu.get(str(q), 99.0) for q in plan_diffs) + back_cpu.get(number, 99.0)
                if spent <= diff_budget:
                    plan_diffs.append(int(number))  # decided by executing it, below
                    return
            first_diff.append((key, value[:120] if value.startswith("!") else "differs"))
            raise StopIteration

        items = front_half(src, on_item=on_item, hooks=hooks, only=only)
        expected = [
            k for k in base
            if not k.endswith(".result") and (only is None or int(k.split(".")[0]) in only)
        ]
        missing = [k for k in expected if k not in items]
        if first_diff:
            verdict = {"same": False, "why": f"{first_diff[0][0]}: {first_diff[0][1]}"}
        elif missing:
            verdict = {"same": False, "why": f"{len(missing)} fingerprints missing, first {missing[0]}"}
        else:
            verdict = {"same": True}
            todo = sorted(queries | set(plan_diffs))
            if todo:
                back = BackHalf(src)
                for q in todo:
                    if q not in hooks.plans or f"{q}.result" not in base:
                        verdict = {"same": False, "why": f"{q}: no plan or no baseline result"}
                        break
                    try:
                        got = back.run(*hooks.plans[q])
                    except BaseException as e:  # noqa: BLE001
                        got = f"!{type(e).__name__}: {e}"[:300]
                    if got != base[f"{q}.result"]:
                        verdict = {"same": False, "why": f"{q}.result: " + (got[:120] if got.startswith("!") else "differs")}
                        break
                verdict["executed"] = len(todo)
            if plan_diffs:
                verdict["plan_diffs"] = plan_diffs
    except BaseException as e:  # noqa: BLE001
        verdict = {"same": False, "why": f"crash {type(e).__name__}: {e}"[:200]}
    verdict["cpu_s"] = round(time.process_time(), 3)
    real_stdout.write(json.dumps(verdict) + "\n")
    real_stdout.flush()
    return 0


# ------------------------------------------------------------ selector's side


def _overlaps(spans, first: int, last: int) -> bool:
    return any(a <= last and first <= b for a, b in spans)


class StageMap:
    """Which sqlglot code objects run where in the test at the pinned commit
    (tools/build_map/record_stage.py). Spans are (first line, last line) per file.

      front          started while earlycut.front_half runs
      setup          started while the fixture Tables are built and pickled for the pool
      back[q]        started in executed query q's back half (ensure_tables, executor,
                     result pickling, reading rows)"""

    def __init__(self, path) -> None:
        import gzip

        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("version") != VERSION:
            raise ValueError("stage map is for another harness version")
        as_spans = lambda d: {f: [tuple(s) for s in spans] for f, spans in d.items()}  # noqa: E731
        self.front = as_spans(raw["front"])
        self.setup = as_spans(raw["setup"])
        self.back = {int(q): as_spans(d) for q, d in raw["back"].items()}

    def classify(self, path: str, func_first: int, func_last: int):
        """For a changed statement inside the (outermost) function at these lines:
        None if the early cutoff cannot decide it, else (front, queries): whether the
        front half runs code of that function, and which queries' back halves do."""
        if _overlaps(self.setup.get(path, ()), func_first, func_last):
            return None  # the shared fixture setup runs it: every query could change
        front = _overlaps(self.front.get(path, ()), func_first, func_last)
        queries = {q for q, spans in self.back.items() if _overlaps(spans.get(path, ()), func_first, func_last)}
        if not front and not queries:
            return None  # the test runs it somewhere this harness does not replay
        return front, queries


def run_check(src: str, base_path: str, job: dict, wall_limit_s: float, log) -> bool:
    """True only when the patched code hands the test the same things as the baseline."""
    import subprocess

    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "check", src, base_path, "-"],
            input=json.dumps(job), capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=wall_limit_s,
        )
    except subprocess.TimeoutExpired:
        log(f"  early cutoff: timed out after {wall_limit_s:.0f}s -> keep the test")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"  early cutoff: could not run ({e!r}) -> keep the test")
        return False
    try:
        verdict = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        log(f"  early cutoff: no verdict (exit {p.returncode}) -> keep the test")
        return False
    log(f"  early cutoff: {verdict}")
    return verdict.get("same") is True


def load_baseline(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if raw.get("version") != VERSION or raw.get("python") != sys.version or str(raw.get("hashseed")) != "0":
        return None
    return raw


def ensure_baseline(src: str, base_path: str, scratch_dir: str, log) -> str | None:
    """The baseline built with the image, or one computed now (costs one full run of
    both halves, so the image should always carry it)."""
    if load_baseline(base_path) is not None:
        return base_path
    import subprocess

    out = os.path.join(scratch_dir, "br_tpcds_base.json")
    if load_baseline(out) is not None:
        return out
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    try:
        subprocess.run(
            [sys.executable, os.path.abspath(__file__), "baseline", src, out],
            env=env, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        log(f"  early cutoff: baseline failed ({e!r})")
        return None
    return out if load_baseline(out) is not None else None


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "baseline":
        sys.exit(cmd_baseline(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 5 and sys.argv[1] == "check":
        sys.exit(cmd_check(sys.argv[2], sys.argv[3], sys.argv[4]))
    print(__doc__, file=sys.stderr)
    sys.exit(2)
