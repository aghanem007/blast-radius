"""Infection probes (weak mutation): does a changed expression ever change value?

A patch that flips one expression (`x is None` -> `x == None`, `<` -> `<=`) only
matters if the new expression ever yields a different value than the old one on the
inputs the program actually sees. Weak mutation testing (Howden 1982) checks exactly
that: run the program with the *original* behavior, but at the mutated spot evaluate
both versions and record whether they ever differ ("infection").

plan_probe() finds the single changed expression in a function and builds an
instrumented copy of that function: the spot becomes
    __br_probe__(lambda: OLD, lambda: NEW, slot)
which returns OLD's value (so the program runs exactly as before) and records
whether NEW differs. Only side-effect-free expressions qualify (names, attributes,
comparisons, boolean logic, constants, a few known-pure calls), so evaluating NEW
as well cannot change what the program does.

Never infected after many evaluations is strong evidence the patch is harmless; the
selector only acts on it where the audit against labeled patches says it's safe.
"""

from __future__ import annotations

import ast
import types

PURE_CALLS = {"isinstance", "issubclass", "len", "type", "bool", "hasattr", "getattr", "id"}
PURE_METHODS = {
    "get", "startswith", "endswith", "lower", "upper", "is_type", "is_string", "is_int",
    "is_number", "is_star", "is_negative", "is_leaf", "isdigit", "isalpha", "strip",
}
PURE_NODES = (
    ast.Name, ast.Attribute, ast.Constant, ast.Compare, ast.BoolOp, ast.UnaryOp, ast.BinOp,
    ast.Subscript, ast.Slice, ast.Tuple, ast.IfExp, ast.Load, ast.expr_context,
    ast.boolop, ast.cmpop, ast.unaryop, ast.operator, ast.Call, ast.keyword,
)


def _pure(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if not isinstance(sub, PURE_NODES):
            return False
        if isinstance(sub, ast.Name) and sub.id == "super":
            return False
        if isinstance(sub, ast.Call):
            f = sub.func
            ok = (isinstance(f, ast.Name) and f.id in PURE_CALLS) or (
                isinstance(f, ast.Attribute) and f.attr in PURE_METHODS
            )
            if not ok:
                return False
    return True


def _same(a: ast.AST, b: ast.AST) -> bool:
    return ast.dump(a) == ast.dump(b)


SHARED = (ast.boolop, ast.cmpop, ast.unaryop, ast.operator, ast.expr_context)


def _diff(a, b, out: list, parent=None) -> None:
    """Collect the smallest differing node pairs of two parallel syntax trees.

    A pair is reported at the node whose own fields differ: a different node type
    (`not x` vs `x`), a different plain value (Constant 16 vs 17, True vs False), or a
    different number of children. Operator nodes (`and`/`or`, `==`/`!=`) are shared
    singletons in Python's parser, so a difference there is reported at the node
    that holds them (`parent`), never at the operator itself."""
    if type(a) is not type(b):
        out.append(parent if isinstance(a, SHARED) and parent is not None else (a, b))
        return
    if not isinstance(a, ast.AST):
        return
    fields = list(zip(ast.iter_fields(a), ast.iter_fields(b)))
    for (_, va), (_, vb) in fields:
        if isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            continue
        if isinstance(va, list):
            if len(va) != len(vb):
                out.append((a, b))
                return
        elif type(va) is not type(vb) or va != vb:
            out.append((a, b))
            return
    for (_, va), (_, vb) in fields:
        if isinstance(va, list):
            for x, y in zip(va, vb):
                _diff(x, y, out, (a, b))
        else:
            _diff(va, vb, out, (a, b))


class _Parents(ast.NodeVisitor):
    """Parent of every node, except the parser's shared operator singletons."""

    def __init__(self) -> None:
        self.parent: dict[int, ast.AST] = {}

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, SHARED):
                self.parent[id(child)] = node
        super().generic_visit(node)


def plan_probe(filename: str, old_src: str, new_src: str):
    """(qualname, firstlineno, instrumented code) for a one-expression change, else None."""
    try:
        old_tree, new_tree = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    diffs: list = []
    _diff(old_tree, new_tree, diffs)
    if len(diffs) != 1:
        return None
    a, b = diffs[0]
    # Climb to the nearest expression that contains the change on both sides.
    pa, pb = _Parents(), _Parents()
    pa.visit(old_tree)
    pb.visit(new_tree)
    while not (isinstance(a, ast.expr) and isinstance(b, ast.expr)):
        a, b = pa.parent.get(id(a)), pb.parent.get(id(b))
        if a is None or b is None or isinstance(a, ast.stmt):
            return None
    if not (_pure(a) and _pure(b)):
        return None
    # The changed expression must sit inside a function (it runs per call, not at import).
    func, node = None, a
    while node is not None:
        node = pa.parent.get(id(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node
            break
        if isinstance(node, (ast.Lambda, ast.ClassDef, ast.comprehension)):
            return None  # keep it simple: plain function bodies only
    if func is None:
        return None

    probe = ast.Call(
        func=ast.Name(id="__br_probe__", ctx=ast.Load()),
        args=[
            ast.Lambda(args=_no_args(), body=a),
            ast.Lambda(args=_no_args(), body=b),
        ],
        keywords=[],
    )
    parent = pa.parent[id(a)]
    replaced = False
    for field, value in ast.iter_fields(parent):
        if value is a:
            setattr(parent, field, probe)
            replaced = True
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if item is a:
                    value[i] = probe
                    replaced = True
    if not replaced:
        return None
    ast.copy_location(probe, a)
    ast.fix_missing_locations(old_tree)
    try:
        code = compile(old_tree, filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    qualname = _qualname(func, pa)
    for co in _codes(code):
        if co.co_qualname == qualname and co.co_firstlineno == func.lineno:
            return qualname, func.lineno, co
    # decorated functions start at their first decorator line
    for co in _codes(code):
        if co.co_qualname == qualname:
            return qualname, co.co_firstlineno, co
    return None


def _no_args() -> ast.arguments:
    return ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[])


def _qualname(func, parents: _Parents) -> str:
    parts, node = [func.name], parents.parent.get(id(func))
    while node is not None and not isinstance(node, ast.Module):
        if isinstance(node, ast.ClassDef):
            parts.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append("<locals>")
            parts.append(node.name)
        node = parents.parent.get(id(node))
    return ".".join(reversed(parts))


def _codes(co: types.CodeType):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from _codes(c)


class Recorder:
    """The __br_probe__ callable: returns the old value, records any difference."""

    def __init__(self) -> None:
        self.reached = 0
        self.infected = 0

    def __call__(self, old_thunk, new_thunk):
        old = old_thunk()
        self.reached += 1
        try:
            new = new_thunk()
        except BaseException:
            self.infected += 1
            return old
        if not _equal(old, new):
            self.infected += 1
        return old


def _equal(x, y) -> bool:
    if x is y:
        return True
    if type(x) is not type(y):
        return False
    try:
        return bool(x == y)
    except Exception:
        return False
