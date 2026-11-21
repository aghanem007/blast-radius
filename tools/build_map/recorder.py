"""pytest plugin that records, per test, what the test touches in sqlglot.

Loaded with `-p recorder` while running sqlglot's suite at the pinned commit.
It writes, for every pytest worker:

  cov.<worker>          coverage data, one context per test phase:
                        "<nodeid>|setup", "<nodeid>|run", "<nodeid>|teardown",
                        and "collect|<module nodeid>" for import-time work.
  extra.<worker>.json   files each context opened or listed (paths relative to the
                        repo), and the sqlglot classes whose methods ran with
                        `self`/`cls` bound to them (with their full MRO).

Only used offline to build maps/. Never runs at score time.
"""

from __future__ import annotations

import json
import os
import sys
import threading

import coverage
import pytest

OUT = os.environ.get("MAP_OUT", "/out")
REPO = os.path.realpath(os.getcwd())
SQLGLOT_DIR = os.path.join(REPO, "sqlglot") + os.sep
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")

_ctx: str | None = None
_reads: dict[str, set[str]] = {}
_types: dict[str, set[type]] = {}
_cur_types: set[type] = set()
_scratch: set[type] = set()
_cov: coverage.Coverage | None = None
_code_kind: dict = {}


def _rel(path) -> str | None:
    try:
        p = os.fspath(path)
    except TypeError:
        return None
    if isinstance(p, bytes):
        p = p.decode(errors="replace")
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd(), p)
    p = os.path.normpath(p)
    if p.startswith(REPO + os.sep):
        return os.path.relpath(p, REPO).replace(os.sep, "/")
    return None


def _audit(event: str, args) -> None:
    ctx = _ctx
    if ctx is None:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, int):
            return
        rel = _rel(path)
        if rel:
            _reads.setdefault(ctx, set()).add(rel)
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        rel = _rel(args[0] if args and args[0] is not None else ".")
        if rel:
            _reads.setdefault(ctx, set()).add(rel.rstrip("/") + "/")


def _prof(frame, event, arg):
    if event != "call":
        return
    co = frame.f_code
    kind = _code_kind.get(co)
    if kind is None:
        kind = 0
        if co.co_argcount > 0 and co.co_filename.startswith(SQLGLOT_DIR):
            first = co.co_varnames[0]
            kind = 1 if first == "self" else 2 if first == "cls" else 0
        _code_kind[co] = kind
    if kind:
        try:
            value = frame.f_locals[co.co_varnames[0]]
        except Exception:
            return
        if kind == 2:
            if isinstance(value, type):
                _cur_types.add(value)
        else:
            _cur_types.add(type(value))


COLD = os.environ.get("BR_COLD", "0") == "1"


def _import_everything() -> None:
    """Import every sqlglot module now, before any test, so code that runs while a
    module loads is recorded as import-time ("run everything" in the selector) instead
    of being credited to whichever test happened to trigger a lazy import first."""
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass


def _cold_caches() -> None:
    """Reset sqlglot's per-process caches before each test, so every test runs the
    code it depends on instead of reusing what an earlier test built. Without this,
    a patch to _build_dispatch or Dialect._try_load breaks every test, but the map
    credits that code only to the first test in each process."""
    try:
        import sqlglot
        from sqlglot import generator
        from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES, Dialect

        generator._DISPATCH_CACHE.clear()
        loadable = set(DIALECT_MODULE_NAMES)
        for key in list(Dialect._classes):
            # Only dialects _try_load can register again; the base Dialect ("") stays.
            if (key.value if hasattr(key, "value") else key) in loadable:
                del Dialect._classes[key]  # Dialect.get / _try_load re-register it on use
        schema = getattr(sqlglot, "schema", None)
        for name, value in list(vars(schema).items()) if schema is not None else []:
            if "cache" in name and isinstance(value, dict):
                value.clear()
    except Exception as e:  # never break the recording run over this
        print(f"[recorder] cache reset failed: {e!r}", file=sys.stderr)


def _switch(ctx: str | None) -> None:
    global _ctx, _cur_types
    _ctx = ctx
    _cov.switch_context(ctx or "")
    _cur_types = _types.setdefault(ctx, set()) if ctx else _scratch


class InProcessExecutor:
    """Stand-in for ProcessPoolExecutor that runs every task right away, in this process.

    sqlglot's optimizer and executor tests hand their real work to spawned worker
    processes, where coverage cannot see it, so those tests looked like they touched
    almost nothing. Running the tasks in-process attributes that work to the test
    that submitted it.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures as cf

        future = cf.Future()
        try:
            # A real pool pickles the call on the way in and the result on the way
            # out. sqlglot pickles expressions through sqlglot.serde, so skipping
            # this would hide serde from every test that uses a pool.
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # the test sees the error through future.result()
            future.set_exception(e)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def _roundtrip(value):
    import pickle

    return pickle.loads(pickle.dumps(value))


def _patch_process_pools() -> None:
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InProcessExecutor
    cfp.ProcessPoolExecutor = InProcessExecutor


def pytest_configure(config) -> None:
    global _cov
    _patch_process_pools()
    os.makedirs(OUT, exist_ok=True)
    _cov = coverage.Coverage(
        data_file=os.path.join(OUT, f"cov.{WORKER}"),
        source=[os.path.join(REPO, "sqlglot"), os.path.join(REPO, "tests")],
        branch=False,
        config_file=False,
    )
    _cov.start()
    sys.addaudithook(_audit)
    sys.setprofile(_prof)
    threading.setprofile(_prof)
    if COLD:
        _import_everything()  # recorded under the "" context = import time


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    _switch(f"collect|{collector.nodeid}")
    yield
    _switch(None)


def _phase(item, name):
    _switch(f"{item.nodeid}|{name}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    if COLD:
        _cold_caches()
    _phase(item, "setup")
    yield
    _switch(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _phase(item, "run")
    yield
    _switch(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _phase(item, "teardown")
    yield
    _switch(None)


def _qualnames(types: set[type]) -> list[str]:
    names = set()
    for t in types:
        for base in getattr(t, "__mro__", ()):
            module = getattr(base, "__module__", "") or ""
            if module.startswith("sqlglot"):
                names.add(f"{module}.{base.__qualname__}")
    return sorted(names)


def pytest_unconfigure(config) -> None:
    sys.setprofile(None)
    threading.setprofile(None)
    global _ctx
    _ctx = None
    _cov.stop()
    _cov.save()
    extra = {
        "reads": {ctx: sorted(paths) for ctx, paths in _reads.items()},
        "classes": {ctx: _qualnames(types) for ctx, types in _types.items() if types},
    }
    with open(os.path.join(OUT, f"extra.{WORKER}.json"), "w") as f:
        json.dump(extra, f)
