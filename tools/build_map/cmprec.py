"""pytest plugin: record, per test, how the two operands of every comparison in sqlglot RELATED.

Line coverage says which tests ran a statement. For a statement that contains a comparison
(`a is b`, `a == b`, `a < b`, ...), this adds what the comparison saw in each test: were the two
operands the same object, were they equal, how were they ordered. It is a map of runtime values at the
pinned commit, like the coverage map is a map of executed lines. It knows nothing about any patch.

The selector uses it at decision time: if a patch only swaps a comparison operator, a test in which
the old and the new operator give the same answer on every operand pair it ever saw there cannot
behave differently.

How: sqlglot's modules are compiled from a rewritten syntax tree in which every single-operator
comparison `L op R` (op in is, is not, ==, !=, <, <=, >, >=) becomes `__br_cmp__(site, L, R, op)`.
That function returns exactly what the original expression returns (operands are evaluated once, in
the same order) and notes the relation of L and R for the running test.

A relation is seven fields, one per operator (is, ==, !=, <, <=, >, >=), each 0 (False), 1 (True) or
2 (unknown). A field is filled in ONLY when evaluating that operator on these operands cannot have
a side effect: both operands are built-in values, enum members, objects with the default identity
`__eq__`, or (for ==/!=) a sqlglot expression against something of another type. Otherwise it is 2,
and the selector keeps the test. That holds for the operator the code really uses as well: an `==`
with a side effect (hash caching, a lazily loaded dialect) is not interchangeable with `is` even
when both answer the same. A comparison that raises is recorded with the code RAISED.

Loaded with `-p cmprec` (PYTHONPATH must include this folder), BEFORE sqlglot is imported.
Set PYTHONPYCACHEPREFIX to an empty folder shared by the recording runs only: the rewritten bytecode is
cached there and must never be picked up by a run without this plugin.
Writes $MAP_OUT/cmp.<worker>.json: {"ctx": {context: {site: [relation codes]}}}
  contexts: "<nodeid>|call", "class|<file>::<Class>" (anything during a test's setup phase, which
  includes the class's setUpClass: credited to the whole class), or "<nodeid>|whole" with
  CMP_WHOLE=<nodeid> (one test per process: everything from start to finish is that test's).
  site: "sqlglot/x.py:line:col:end_line:end_col" of the comparison in the pinned source.

Offline only; never part of the image.
"""

from __future__ import annotations

import ast
import builtins
import json
import operator
import os
import sys
from importlib.machinery import SourceFileLoader

import pytest

OUT = os.environ.get("MAP_OUT", "/out")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
WHOLE = os.environ.get("CMP_WHOLE")

IS, ISNOT, EQ, NE, LT, LE, GT, GE = range(8)
_OPCODE = {ast.Is: IS, ast.IsNot: ISNOT, ast.Eq: EQ, ast.NotEq: NE, ast.Lt: LT, ast.LtE: LE, ast.Gt: GT, ast.GtE: GE}
_FN = (operator.is_, operator.is_not, operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge)
# field index in a relation for each opcode (is not shares the "is" field)
_FIELD = (0, 0, 1, 2, 3, 4, 5, 6)
RAISED = 3 ** 7  # a code no relation can have: "the comparison raised here"

_cur: dict | None = None
_data: dict[str, dict[str, set[int]]] = {}

_NONE = type(None)
_SCALAR_EQ = {t.__eq__ for t in (int, float, str, bytes, _NONE)}
_SCALAR_LT = {t.__lt__ for t in (int, float, str, bytes)}
_CONTAINERS = (tuple, list, frozenset, set, dict)
_eq_kind: dict[type, int] = {}  # 0 identity, 1 built-in value, 2 sqlglot expression, 3 anything else, 4 container
_lt_ok: dict[type, bool] = {}
_expr_eq = None


def _kind(t: type) -> int:
    k = _eq_kind.get(t)
    if k is None:
        global _expr_eq
        if _expr_eq is None:
            core = sys.modules.get("sqlglot.expressions.core")
            _expr_eq = getattr(getattr(core, "Expr", None), "__eq__", None) if core is not None else None
        f = t.__eq__
        if f is object.__eq__:
            k = 0
        elif f in _SCALAR_EQ:
            k = 1
        elif t in _CONTAINERS:
            k = 4
        elif _expr_eq is not None and f is _expr_eq:
            k = 2
        else:
            k = 3
        if _expr_eq is not None or k != 3:
            _eq_kind[t] = k
    return k


def _plain_elements(c) -> bool:
    if len(c) > 32:
        return False
    items = c.items() if type(c) is dict else ((x,) for x in c)
    for group in items:
        for x in group:
            k = _kind(type(x))
            if k > 1:
                if k == 4 and type(x) is tuple and len(x) <= 8 and all(_kind(type(y)) <= 1 for y in x):
                    continue
                return False
    return True


def _eq_is_pure(a, b) -> bool:
    ka, kb = _kind(type(a)), _kind(type(b))
    if ka == 3 or kb == 3:
        return False
    if ka == 2 or kb == 2:
        # a sqlglot expression: its `__eq__` answers without hashing anything only for the
        # very same object, or for something of another type
        return a is b or type(a) is not type(b)
    if ka == 4 and not _plain_elements(a):
        return False
    if kb == 4 and not _plain_elements(b):
        return False
    return True


def _orderable(x) -> bool:
    t = type(x)
    ok = _lt_ok.get(t)
    if ok is None:
        ok = _lt_ok[t] = t.__lt__ in _SCALAR_LT
    if ok:
        return True
    return t is tuple and len(x) <= 8 and all(_lt_ok.get(type(y)) or type(y).__lt__ in _SCALAR_LT for y in x)


def _res(r) -> int:
    return 1 if r is True else 0 if r is False else 2


def __br_cmp__(site, a, b, op):
    try:
        r = _FN[op](a, b)  # exactly the original expression
    except BaseException:
        # The comparison itself raised (say `None < 1`). That is the program's own
        # exception and goes on, but the test did evaluate this comparison on operands
        # we know nothing useful about: note it, so the selector keeps the test.
        cur = _cur
        if cur is not None:
            seen = cur.get(site)
            if seen is None:
                cur[site] = {RAISED}
            else:
                seen.add(RAISED)
        raise
    cur = _cur
    if cur is None:
        return r
    f = [1 if a is b else 0, 2, 2, 2, 2, 2, 2]
    try:
        if _eq_is_pure(a, b):
            f[1] = _res(a == b)
            f[2] = _res(a != b)
        if _orderable(a) and _orderable(b):
            try:
                f[3], f[4], f[5], f[6] = _res(a < b), _res(a <= b), _res(a > b), _res(a >= b)
            except TypeError:
                pass
    except BaseException:  # noqa: BLE001 - never let the recorder change the program
        f = [1 if a is b else 0, 2, 2, 2, 2, 2, 2]
    # The operator the code really used is NOT filled in from `r` when its evaluation on
    # these operands is not known to be free of side effects: swapping it for another
    # operator would then remove (or add) those side effects, whatever the answers are.
    code = f[0] + 3 * f[1] + 9 * f[2] + 27 * f[3] + 81 * f[4] + 243 * f[5] + 729 * f[6]
    seen = cur.get(site)
    if seen is None:
        cur[site] = {code}
    else:
        seen.add(code)
    return r


builtins.__br_cmp__ = __br_cmp__


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.sites = 0

    def visit_Compare(self, node: ast.Compare):
        key = f"{self.rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _OPCODE:
            return node
        self.sites += 1
        call = ast.Call(
            func=ast.Name("__br_cmp__", ast.Load()),
            args=[ast.Constant(key), node.left, node.comparators[0], ast.Constant(_OPCODE[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


_original_source_to_code = SourceFileLoader.source_to_code


def _source_to_code(self, data, path, *, _optimize=-1):
    p = os.fspath(path).replace("\\", "/")
    i = p.find("/sqlglot/sqlglot/")
    if i < 0 or not p.endswith(".py"):
        return _original_source_to_code(self, data, path, _optimize=_optimize)
    rel = p[i + len("/sqlglot/"):]
    tree = ast.parse(data, filename=path)
    tree = _Rewrite(rel).visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


SourceFileLoader.source_to_code = _source_to_code
if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
    raise RuntimeError("cmprec must be loaded before sqlglot is imported")


class InProcessExecutor:
    """Runs pool tasks in this process (with the pickling a real pool does), so the comparisons of
    sqlglot's pool-based tests are seen. Same stand-in as recorder.py and attrrec.py."""

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
    if not WHOLE:
        _cur = _data.setdefault(ctx, {})


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
    if WHOLE:
        _cur = _data.setdefault(f"{WHOLE}|whole", {})


def _class_ctx(item) -> str:
    parts = item.nodeid.split("::")
    return "class|" + "::".join(parts[:-1])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _begin(_class_ctx(item))
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
    with open(os.path.join(OUT, f"cmp.{WORKER}.json"), "w", encoding="utf-8") as f:
        json.dump({"ctx": {k: {s: sorted(v) for s, v in d.items()} for k, d in _data.items() if d}}, f)
