"""pytest plugin: record, per test, which class-level data of sqlglot it READS.

Line coverage says which code a test ran. This says which class attributes (settings,
tables, flags defined in class bodies) a test's code looked at, which is what decides
whether an edit to one of them can change the test.

How: after every sqlglot module is imported, each plain class attribute is replaced by
a descriptor that returns the original object and notes (class that defines it, name)
for the test that is running. Descriptors fire for `obj.NAME`, `Cls.NAME`, subclass
access and `getattr`, so dynamic lookups are seen too. Functions, properties, nested
classes, other descriptors, dunders and Enum classes are left alone.

Loaded with `-p attrrec` (PYTHONPATH must include this folder). Writes
$MAP_OUT/attr.<worker>.json:
  {"reads": {context: [keys]}, "wrapped": [every key that was wrapped]}
contexts: "<nodeid>|setup", "<nodeid>|call" and "collect|<module nodeid>"; keys are
"module.Qual.Name:ATTR".

Modes (environment):
  ATTR_WHOLE=<nodeid>  one test per process (tools/build_map/attr_isolated.py): every
                       read from start to finish is that test's. A fresh process has no
                       warm caches, so the test performs every read it depends on itself.
  BR_COLD=1            reset sqlglot's per-process caches before each test.

Offline only; never part of the image.
"""

from __future__ import annotations

import enum
import json
import os
import sys

import pytest

OUT = os.environ.get("MAP_OUT", "/out")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
WHOLE = os.environ.get("ATTR_WHOLE")
COLD = os.environ.get("BR_COLD", "0") == "1"

_cur: set | None = None
_reads: dict[str, set[str]] = {}
_seen: dict[int, bool] = {}
_wrapped: list[str] = []


class _Rec:
    """Non-data descriptor standing in for one class attribute."""

    __slots__ = ("value", "key")

    def __init__(self, value, key) -> None:
        self.value = value
        self.key = key

    def __get__(self, obj, objtype=None):
        cur = _cur
        if cur is not None:
            cur.add(self.key)
        return self.value


def _plain(name: str, value) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return False
    if isinstance(value, (_Rec, type)):
        return False
    # anything that already takes part in the descriptor protocol: functions,
    # classmethod/staticmethod/property objects, slot members, functools helpers...
    if hasattr(type(value), "__get__"):
        return False
    return True


def _wrap_class(cls: type) -> None:
    if id(cls) in _seen:
        return
    _seen[id(cls)] = True
    if issubclass(cls, enum.Enum) or not getattr(cls, "__module__", "").startswith("sqlglot"):
        return
    owner = f"{cls.__module__}.{cls.__qualname__}"
    for name, value in list(vars(cls).items()):
        if isinstance(value, type):
            _wrap_class(value)  # nested classes (Dialect.Tokenizer, ...)
            continue
        if not _plain(name, value):
            continue
        try:
            type.__setattr__(cls, name, _Rec(value, f"{owner}:{name}"))
            _wrapped.append(f"{owner}:{name}")
        except (AttributeError, TypeError):
            pass


def _wrap_loaded() -> None:
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
        for value in list(vars(mod).values()):
            if isinstance(value, type):
                _wrap_class(value)


def _import_everything() -> None:
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:  # noqa: BLE001
            pass


def _cold_caches() -> None:
    """Same reset as recorder.py: every test re-reads what a warm process had cached."""
    try:
        import sqlglot
        from sqlglot import generator
        from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES, Dialect

        generator._DISPATCH_CACHE.clear()
        loadable = set(DIALECT_MODULE_NAMES)
        for key in list(Dialect._classes):
            if (key.value if hasattr(key, "value") else key) in loadable:
                del Dialect._classes[key]
        schema = getattr(sqlglot, "schema", None)
        for name, value in list(vars(schema).items()) if schema is not None else []:
            if "cache" in name and isinstance(value, dict):
                value.clear()
    except Exception as e:  # noqa: BLE001
        print(f"[attrrec] cache reset failed: {e!r}", file=sys.stderr)


class InProcessExecutor:
    """Runs pool tasks in this process (with the pickling a real pool does), so the
    reads of sqlglot's pool-based tests are seen. Same stand-in as recorder.py."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures as cf

        future = cf.Future()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
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


def _begin(ctx: str) -> None:
    global _cur
    _wrap_loaded()  # classes from modules imported or created since the last look
    if WHOLE:
        return  # one set for the whole process
    _cur = _reads.setdefault(ctx, set())


def _end() -> None:
    global _cur
    if not WHOLE:
        _cur = None


def pytest_configure(config) -> None:
    global _cur
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InProcessExecutor
    cfp.ProcessPoolExecutor = InProcessExecutor
    _import_everything()
    _wrap_loaded()
    if WHOLE:
        _cur = _reads.setdefault(f"{WHOLE}|whole", set())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    _begin(f"collect|{collector.nodeid}")
    yield
    _end()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    if COLD:
        _cold_caches()
    _begin(f"{item.nodeid}|setup")
    yield
    _end()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _begin(f"{item.nodeid}|call")
    yield
    _end()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _begin(f"{item.nodeid}|call")
    yield
    _end()


def pytest_unconfigure(config) -> None:
    global _cur
    _cur = None
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"attr.{WORKER}.json"), "w", encoding="utf-8") as f:
        json.dump({"reads": {k: sorted(v) for k, v in _reads.items() if v}, "wrapped": sorted(_wrapped)}, f)
