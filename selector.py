"""Blast Radius selector, team 2696.

For each patch: work out which tests could notice the change, using a map built
offline from sqlglot at the pinned commit (tools/build_map), then pack those tests
onto the cheapest number of workers.

The rule everywhere: when unsure, select more. A missed failing test costs 300 s,
far more than any test we might run needlessly.

How a changed file is handled:
  - Python, only comments/formatting changed (same syntax tree): no tests.
  - Python, a statement inside a function or method changed: the tests that ran
    that statement. If the statement also ran while modules were importing, every test.
  - Python, a class-level statement (class attributes, dict tables): the tests that
    used that class or a subclass; if no test did, the tests that use the names it defines.
  - Python, module-level constant: the tests that depend on the names it defines.
  - Python that no longer parses, or import-time changes we can't place: every test.
  - Fixtures and other data files: the tests that opened the file (or listed its dir).
  - Anything we can't parse or locate: every test.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
import types
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
MAP_PATH = Path(os.environ.get("BR_MAP", APP / "maps" / "map.json.gz"))
SRC = Path(os.environ.get("BR_SRC", APP / "src"))

MAX_WORKERS = 8
WORKER_COST_MS = 2000
ALL = None  # sentinel: select every test

# Config files that can change how pytest itself runs (pytest reads its settings from
# pyproject.toml / setup.cfg / tox.ini / pytest.ini, before any test can record a read).
PYTEST_CONFIG = {"pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py", "pyproject.toml", "setup.cfg"}

# Class attributes that register a class under a name other classes are looked up by
# (an expression's key, a function's SQL names): an edit can break *other* classes' tests.
REGISTRY_ATTRS = {"key", "_sql_names", "sql_names"}


def log(*args) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, path: Path) -> None:
        with gzip.open(path, "rt") as f:
            raw = json.load(f)
        self.tests: list[str] = raw["tests"]
        self.sets: list[list[int]] = raw["sets"]
        self.lines = {f: dict(pairs) for f, pairs in raw["lines"].items()}
        self.import_lines = {f: set(ls) for f, ls in raw["import_lines"].items()}
        self.reads: dict[str, int] = raw["reads"]
        self.classes: dict[str, int] = raw["classes"]
        self.names: dict[str, int] = raw["names"]
        self.import_names: set[str] = set(raw.get("import_names", ()))
        self.tests_by_file: dict[str, set[int]] = {}
        for i, nodeid in enumerate(self.tests):
            self.tests_by_file.setdefault(nodeid.split("::", 1)[0], set()).add(i)

    def line_tests(self, path: str, start: int, end: int) -> set[int]:
        by_line = self.lines.get(path, {})
        out: set[int] = set()
        for ln in range(start, end + 1):
            s = by_line.get(ln)
            if s is not None:
                out.update(self.sets[s])
        return out

    def ran_at_import(self, path: str, start: int, end: int) -> bool:
        lines = self.import_lines.get(path, ())
        return any(ln in lines for ln in range(start, end + 1))

    def set_of(self, table: dict[str, int], key: str) -> set[int] | None:
        s = table.get(key)
        return None if s is None else set(self.sets[s])


# ------------------------------------------------------------------------- patch


class FilePatch:
    def __init__(self) -> None:
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list[tuple[int, list[str]]] = []  # (old_start, lines with their +/-/space prefix)
        self.binary = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[FilePatch]:
    """Parse a unified diff. Hunk bodies are read by the counts in their @@ header,
    so a removed line that itself starts with "--" or "++" is never taken for a header."""
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: list[str] | None = None
    left_old = left_new = 0
    for line in text.split("\n"):
        if hunk is not None and (left_old > 0 or left_new > 0):
            tag = line[:1]
            if tag == "\\":
                continue  # "\ No newline at end of file"
            if tag == " " or line == "":
                hunk.append(" " + line[1:])
                left_old -= 1
                left_new -= 1
                continue
            if tag == "-":
                hunk.append(line)
                left_old -= 1
                continue
            if tag == "+":
                hunk.append(line)
                left_new -= 1
                continue
            raise PatchError("malformed hunk")
        hunk = None
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            continue
        if cur is None:
            if line.startswith("--- "):  # a diff without the "diff --git" line
                cur = FilePatch()
                files.append(cur)
            else:
                continue
        if line.startswith("--- "):
            p = line[4:].split("\t")[0].strip()
            cur.old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0].strip()
            cur.new_path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("new file mode"):
            cur.old_path = None
        elif line.startswith("deleted file mode"):
            cur.new_path = None
        elif line.startswith("rename from "):
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.new_path = line[len("rename to "):]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        else:
            m = HUNK_RE.match(line)
            if m:
                hunk = []
                cur.hunks.append((int(m.group(1)), hunk))
                left_old = int(m.group(2)) if m.group(2) is not None else 1
                left_new = int(m.group(4)) if m.group(4) is not None else 1
    return files


class PatchError(Exception):
    pass


def apply_hunks(old_lines: list[str], hunks) -> list[str]:
    """Apply unified-diff hunks to a file's lines (without newlines)."""
    new: list[str] = []
    pos = 0  # 0-based index into old_lines
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if not any(l[:1] in (" ", "-") for l in body):
            start = old_start  # pure insertion: "-N,0" means after line N
        if start < pos:
            raise PatchError("overlapping hunks")
        new.extend(old_lines[pos:start])
        pos = start
        for l in body:
            tag, text = l[:1], l[1:]
            if tag == "+":
                new.append(text)
                continue
            if pos >= len(old_lines) or old_lines[pos] != text:
                raise PatchError("context mismatch")
            if tag == " ":
                new.append(text)
            pos += 1
    new.extend(old_lines[pos:])
    return new


def hunk_changes(old_start: int, body: list[str]) -> tuple[list[int], list[int]]:
    """Old line numbers removed, and old line numbers that pure insertions follow.

    Added lines that replace removed lines (a "-" run followed by a "+" run) are
    placed by the removed lines; only "+" runs with no "-" run right before them
    count as insertions.
    """
    removed, inserted_after = [], []
    ln = old_start if any(l[:1] in (" ", "-") for l in body) else old_start + 1
    prev = " "
    for l in body:
        tag = l[:1]
        if tag == "-":
            removed.append(ln)
            ln += 1
        elif tag == " ":
            ln += 1
        elif tag == "+" and prev == " ":
            inserted_after.append(ln - 1)
        if tag != "+":
            prev = tag
    return removed, sorted(set(inserted_after))


# ------------------------------------------------------------------- python ast


_COMPILED: dict[str, list] = {}


def _code_list(src: str) -> list | None:
    """Every code object of a module, in a fixed order, keyed by its source text."""
    found = _COMPILED.get(src)
    if found is None:
        try:
            top = compile(src, "<m>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return None
        found, stack = [], [top]
        while stack:
            co = stack.pop()
            found.append(co)
            stack.extend(c for c in reversed(co.co_consts) if isinstance(c, types.CodeType))
        _COMPILED[src] = found
    return found


def const_key(c):
    """A constant's identity by exact type and value, all the way down.

    Plain == is not enough: (0,) == (False,), (1, 2) == (1, 2.0) and 0.0 == -0.0 are
    all True in Python, yet each pair behaves differently. Containers are compared
    element by element with their types; floats and complex numbers by repr."""
    if isinstance(c, types.CodeType):
        return "<code>"
    t = type(c)
    if t is tuple:
        return ("tuple", tuple(const_key(x) for x in c))
    if t is frozenset:
        return ("frozenset", frozenset(const_key(x) for x in c))
    if t in (float, complex):
        return (t.__name__, repr(c))
    return (t.__name__, repr(c) if t not in (int, str, bytes, bool, type(None), type(...)) else c)


def _behavior(co):
    """What a code object does, ignoring line numbers (comments move lines around).

    Python 3.13 stores a class's first line as a constant in its body
    (__firstlineno__, used only by introspection), so that constant is ignored too."""
    class_line = co.co_firstlineno if "__firstlineno__" in co.co_names else None
    consts = tuple(
        "<firstlineno>" if type(c) is int and c == class_line
        else const_key(c)
        for c in co.co_consts
    )
    return (
        co.co_name, co.co_qualname, co.co_code, co.co_names, co.co_varnames, co.co_freevars,
        co.co_cellvars, consts, co.co_flags, co.co_argcount, co.co_kwonlyargcount, co.co_posonlyargcount,
    )


def same_tree(a: str, b: str) -> bool:
    """True when two sources compile to the same code (comments, spacing, redundant
    parentheses: Trivial Compiler Equivalence). Docstrings are constants, so a
    docstring edit is a real change here."""
    x, y = _code_list(a), _code_list(b)
    if x is None or y is None or len(x) != len(y):
        return False
    return all(_behavior(p) == _behavior(q) for p, q in zip(x, y))


class Stmt:
    __slots__ = ("node", "start", "end", "func", "cls", "parent")

    def __init__(self, node, func, cls, parent) -> None:
        self.node = node
        self.start = node.lineno
        self.end = node.end_lineno or node.lineno
        self.func = func  # innermost enclosing function node, or None
        self.cls = cls  # dotted class path inside the module, or None
        self.parent = parent


def index_statements(tree: ast.Module) -> list[Stmt]:
    out: list[Stmt] = []

    def visit(body, func, cls, parent) -> None:
        for node in body:
            if not isinstance(node, ast.stmt):
                continue
            s = Stmt(node, func, cls, parent)
            out.append(s)
            if isinstance(node, ast.ClassDef):
                inner_cls = f"{cls}.{node.name}" if cls else node.name
                visit(node.body, func, inner_cls, s)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(node.body, node, cls, s)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    child = getattr(node, field, None)
                    if isinstance(child, list):
                        kids = []
                        for c in child:
                            if isinstance(c, ast.ExceptHandler):
                                kids.extend(c.body)
                            else:
                                kids.append(c)
                        visit(kids, func, cls, s)
                if isinstance(node, ast.Match):
                    for case in node.cases:
                        visit(case.body, func, cls, s)

    visit(tree.body, None, None, None)
    return out


def innermost(stmts: list[Stmt], lo: int, hi: int) -> Stmt | None:
    best = None
    for s in stmts:
        if s.start <= lo and hi <= s.end and (best is None or s.end - s.start <= best.end - best.start):
            best = s
    return best


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def defined_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    names = set()
    targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
    for t in targets:
        if t is not None:
            names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def is_docstring(s: Stmt) -> bool:
    node = s.node
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
        return False
    body = s.parent.node.body if s.parent is not None else None
    return body is None or (body and body[0] is node)


# --------------------------------------------------------------------- selection


class Selector:
    def __init__(self, m: Map) -> None:
        self.m = m
        self.src_cache: dict[str, str | None] = {}
        self.stmt_cache: dict[str, list[Stmt]] = {}
        # Per patch: can its candidate tests be tried with prerun.py? Only when every
        # real change is inside sqlglot function bodies (see prerun.py).
        self.prerun_ok = False
        self.new_sources: dict[str, str] = {}

    def source(self, path: str) -> str | None:
        if path not in self.src_cache:
            p = SRC / path
            self.src_cache[path] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self.src_cache[path]

    def select(self, patch_text: str) -> set[int] | None:
        self.prerun_ok = True
        self.new_sources = {}
        files = parse_patch(patch_text)
        if not files:
            log("  no files in patch -> all")
            self.prerun_ok = False
            return ALL
        chosen: set[int] = set()
        for fp in files:
            got = self.select_file(fp)
            if got is ALL:
                self.prerun_ok = False
                return ALL
            chosen |= got
        return chosen

    def select_file(self, fp: FilePatch) -> set[int] | None:
        path = fp.path
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            self.prerun_ok = False  # fixtures, tests, data: their own rules
        if fp.binary:
            return self.data_file(path, fp)
        if path in PYTEST_CONFIG or path.endswith("/conftest.py"):
            log(f"  {path}: pytest config -> all")
            return ALL
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            return self.python_file(fp)
        return self.data_file(path, fp)

    # ---- data files (fixtures, docs, config)
    def data_file(self, path: str, fp: FilePatch) -> set[int]:
        out: set[int] = set()
        if path.startswith("sqlglotc/"):
            # test_sqlglotc builds the sqlglotc package; setuptools reads its files in
            # ways our read map may not see (pyproject.toml, MANIFEST.in).
            out |= self.whole_test_file("tests/test_sqlglotc.py")
        for p in {fp.old_path, fp.new_path} - {None}:
            got = self.m.set_of(self.m.reads, p)
            if got:
                out |= got
            # a new or deleted file matters to tests that list its directory
            if fp.old_path is None or fp.new_path is None:
                d = p.rsplit("/", 1)[0] + "/" if "/" in p else "./"
                got = self.m.set_of(self.m.reads, d)
                if got:
                    out |= got
        if path.endswith(".py"):
            # Python outside sqlglot/ and tests/ (setup.py, benchmarks, docs tooling):
            # only matters if a test ran it.
            for p in {fp.old_path, fp.new_path} - {None}:
                if p in self.m.lines:
                    log(f"  {path}: python outside the package that tests ran -> all")
                    return ALL
        log(f"  {path}: data file read by {len(out)} tests")
        return out

    # ---- python
    def python_file(self, fp: FilePatch) -> set[int] | None:
        path = fp.path
        if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            log(f"  {path}: python file added/removed/renamed -> all")
            return ALL
        old = self.source(path)
        if old is None:
            log(f"  {path}: not in the pinned source -> all")
            return ALL
        old_lines = old.split("\n")
        try:
            new_lines = apply_hunks(old_lines, fp.hunks)
        except PatchError as e:
            log(f"  {path}: patch does not apply ({e}) -> all")
            return ALL
        new = "\n".join(new_lines)
        if _code_list(new) is None:  # does not compile
            log(f"  {path}: syntax error after patch -> {'its tests' if self.is_test_module(path) else 'all'}")
            return self.whole_test_file(path) if self.is_test_module(path) else ALL
        if same_tree(old, new):
            log(f"  {path}: same compiled code (comments/formatting) -> none")
            return set()
        self.new_sources[path] = new

        stmts = self.statements(path, old)
        out: set[int] = set()
        for old_start, body in fp.hunks:
            # skip hunks that on their own leave the syntax tree unchanged
            try:
                alone = "\n".join(apply_hunks(old_lines, [(old_start, body)]))
            except PatchError:
                return ALL
            if same_tree(old, alone):
                continue
            if self.only_adds_new_functions(old, alone):
                log(f"  {path}: hunk only adds new, unused module-level functions -> none")
                continue
            removed, after = hunk_changes(old_start, body)
            spots = [(ln, ln) for ln in removed] + [(a, a + 1) for a in after]
            for lo, hi in spots:
                got = self.place(path, stmts, lo, hi)
                if got is ALL:
                    return ALL
                out |= got
        return out

    def only_adds_new_functions(self, old: str, new: str) -> bool:
        """True when `new` is `old` plus module-level functions that cannot matter: no
        decorators, constant defaults only, and a name no sqlglot code defines or reads.
        Defining such a function runs nothing but the def itself."""
        try:
            a, b = ast.parse(old).body, ast.parse(new).body
        except SyntaxError:
            return False
        dumps_a = [ast.dump(s) for s in a]
        extra, i = [], 0
        for s in b:
            if i < len(a) and ast.dump(s) == dumps_a[i]:
                i += 1
            else:
                extra.append(s)
        if i != len(a) or not extra:
            return False
        known = set(self.m.names) | self.m.import_names
        for s in extra:
            if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) or s.decorator_list:
                return False
            defaults = s.args.defaults + [d for d in s.args.kw_defaults if d is not None]
            if not all(isinstance(d, ast.Constant) for d in defaults):
                return False
            if s.returns is not None or any(arg.annotation is not None for arg in s.args.args):
                return False  # annotations can be evaluated at definition time
            if s.name in known or s.name in old:
                return False
        return True

    def statements(self, path: str, old: str) -> list[Stmt]:
        """The original file's statements, parsed once per run."""
        found = self.stmt_cache.get(path)
        if found is None:
            found = self.stmt_cache[path] = index_statements(ast.parse(old))
        return found

    def is_test_module(self, path: str) -> bool:
        return path.startswith("tests/") and path.rsplit("/", 1)[-1].startswith("test_")

    def whole_test_file(self, path: str) -> set[int]:
        return set(self.m.tests_by_file.get(path, set()))

    def place(self, path: str, stmts: list[Stmt], lo: int, hi: int) -> set[int] | None:
        """Tests affected by a change at old lines lo..hi of a Python file."""
        s = innermost(stmts, lo, hi)
        if s is None:
            log(f"  {path}:{lo}: module level outside any statement -> all")
            return ALL
        m = self.m

        if is_docstring(s):
            self.prerun_ok = False  # doctests read __doc__, which a code swap leaves alone
            docs = self.whole_test_file("tests/test_docs.py")
            log(f"  {path}:{lo}: docstring -> {len(docs)} doc tests")
            return docs

        # Inside a function or method body: the tests that ran this statement.
        if s.func is not None and s.node is not s.func:
            if m.ran_at_import(path, s.start, s.end):
                log(f"  {path}:{lo}: statement runs at import time -> all")
                return ALL
            got = m.line_tests(path, s.start, s.end)
            log(f"  {path}:{lo}: runtime statement {s.start}-{s.end} -> {len(got)} tests")
            return got

        # Everything below runs at import time: not something a code swap can try.
        self.prerun_ok = False

        # A def or class header itself (signature, defaults, decorators, bases).
        if isinstance(s.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and lo <= (s.node.body[0].lineno - 1):
            got = m.line_tests(path, s.start, s.end)
            got |= self.names_users(defined_names(s.node)) or set()
            log(f"  {path}:{lo}: def header {s.node.name} -> {len(got)} tests")
            return got

        if self.is_test_module(path):
            got = self.whole_test_file(path)
            log(f"  {path}:{lo}: test module import-time code -> its {len(got)} tests")
            return got
        if path.startswith("tests/"):
            log(f"  {path}:{lo}: test helper import-time code -> all")
            return ALL

        # Import-time code in sqlglot: class bodies and module constants.
        # (A change to a class header, or between two statements of its body, lands
        # on the ClassDef itself: it concerns that class and its subclasses.)
        cls_path = s.cls
        if isinstance(s.node, ast.ClassDef):
            cls_path = f"{s.cls}.{s.node.name}" if s.cls else s.node.name
        if cls_path is not None and defined_names(s.node) & REGISTRY_ATTRS:
            log(f"  {path}:{lo}: registry attribute {sorted(defined_names(s.node) & REGISTRY_ATTRS)} -> all")
            return ALL
        if cls_path is not None:
            qual = f"{module_name(path)}.{cls_path}"
            users = m.set_of(m.classes, qual)
            if users is not None:
                # A plain class attribute only matters to code that reads it by name.
                # Narrow to the class's users that ran such code, unless the name is
                # also read while modules load (a metaclass building tables from it, or
                # another class copying it), where tests depend on it without reading it.
                defined = defined_names(s.node)
                if (
                    isinstance(s.node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    and defined
                    and not (defined & m.import_names)
                ):
                    readers = self.names_users(defined)
                    if readers is not None:
                        got = users & readers
                        log(f"  {path}:{lo}: attribute {sorted(defined)} of {qual} -> {len(got)} tests (of {len(users)} users)")
                        return got
                log(f"  {path}:{lo}: class-level in {qual} -> {len(users)} tests using it")
                return users
            got = self.names_users(defined_names(s.node))
            if got is not None:
                log(f"  {path}:{lo}: class-level in unused {qual}; names -> {len(got)} tests")
                return got
            log(f"  {path}:{lo}: class-level in unused {qual}, unknown names -> all")
            return ALL

        if isinstance(s.node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            got = self.names_users(defined_names(s.node))
            if got is not None:
                log(f"  {path}:{lo}: module constant {sorted(defined_names(s.node))} -> {len(got)} tests")
                return got
        log(f"  {path}:{lo}: module-level {type(s.node).__name__} -> all")
        return ALL

    def names_users(self, names: set[str]) -> set[int] | None:
        if not names:
            return None
        out: set[int] = set()
        for n in names:
            got = self.m.set_of(self.m.names, n)
            if got is None:
                return None
            out |= got
        return out


# ------------------------------------------------------------------------ packing


def pack(tests: list[tuple[int, int]], worker_count: int) -> tuple[list[list[int]], int]:
    """Longest test first, each onto the least-loaded worker. tests = [(number, ms)]."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for number, ms in sorted(tests, key=lambda t: t[1], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += ms
    return workers, max(loads)


def best_split(tests: list[tuple[int, int]]) -> list[list[int]]:
    if not tests:
        return []
    best_workers, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers, busiest = pack(tests, k)
        cost = busiest + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_workers, best_cost = workers, cost
    return best_workers


# ------------------------------------------------------------------------ prerun

# Off by default (review, Nov 20): accurate (617 audited verdicts, 0 errors) but it saved
# ~2 s over 73 patches while adding forks, test packages and a time-budget risk.
PRERUN = os.environ.get("BR_PRERUN", "0") == "1"
ELIDE_BIG = os.environ.get("BR_ELIDE", "0") == "1"  # giant-test skip: off until the data supports it
AUDIT = os.environ.get("BR_AUDIT", "0") == "1"  # validation: log the runner's verdict on every cheap candidate
PROBE_AUDIT = os.environ.get("BR_PROBE_AUDIT", "0") == "1"  # validation: log infection-probe results
PROBE = os.environ.get("BR_PROBE", "0") == "1"  # infection probe: off until audited on enough patches
PROBE_MIN_REACHED = 10
CHEAP_MS = 1500  # tests at most this long may be run by us; longer ones are scheduled
PRERUN_OVERHEAD_MS = 40  # fork and code swap, per patch
COST_MARGIN = 1.35  # our CPU can vary from the calibration run (optimizer tests ran ~1.25x over)
COSTS_PATH = Path(os.environ.get("BR_COSTS", APP / "maps" / "prerun_costs.json"))


class PrerunCosts:
    """Measured CPU of running tests ourselves (tools/calibrate_prerun.py).

    child_ms[nodeid]: a cold forked child running only that test; import_ms[file]:
    importing the test module in the parent, paid once per run. Tests without a
    measurement are never run by us.
    """

    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text())
        self.child_ms: dict[str, int] = raw["child_ms"]
        self.import_ms: dict[str, int] = raw["import_ms"]
        # warm_ms: a test's CPU when run after the others of its module in one child
        # (shared lazy setup already paid): the right per-test cost for a batch.
        self.warm_ms: dict[str, int] = raw.get("warm_ms", {})
        self.imported: set[str] = set()
        # Each measurement = the module's one-time work in a fresh child (class setup,
        # lazy sqlglot init) + the test itself. Split them: a module's startup is its
        # cheapest measurement; a test's own cost is what it adds on top of that.
        self.startup_ms: dict[str, int] = {}
        for nodeid, v in self.child_ms.items():
            if v < 10**6:
                f = nodeid.split("::", 1)[0]
                self.startup_ms[f] = min(v, self.startup_ms.get(f, v))

    def test_ms(self, nodeid: str) -> int | None:
        v = self.child_ms.get(nodeid)
        if v is None or v >= 10**6:
            return None
        if nodeid in self.warm_ms:
            return self.warm_ms[nodeid]
        return v - self.startup_ms[nodeid.split("::", 1)[0]]

    def run_ms(self, nodeids: list[str]) -> float:
        files = {n.split("::", 1)[0] for n in nodeids}
        total = sum(self.test_ms(n) for n in nodeids)
        # module startup: its class setup measured in a warm suite when we have it,
        # else the cold child's cheapest test (which includes that setup)
        total += sum(self.warm_ms.get(f"{f}::__setup__", self.startup_ms[f]) for f in files)
        total += sum(self.import_ms.get(f, 500) for f in files - self.imported)
        return COST_MARGIN * total + PRERUN_OVERHEAD_MS


COSTS: PrerunCosts | None = None


def _cpu() -> tuple[float, float]:
    import resource

    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime, kids.ru_utime + kids.ru_stime


def commit_cost_ms(numbers: list[int], ms: dict[int, int]) -> int:
    workers = best_split([(n, ms[n]) for n in numbers])
    if not workers:
        return 0
    return max(sum(ms[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)


def try_prerun(runner, selector: "Selector", numbers: list[int], ms, nodeid_of) -> list[int]:
    """Run the cheap candidates against the patched code; keep only those that fail.

    Expensive candidates are always kept (scheduled). Falls back to the full list
    whenever the change is not a pure function-body change or it would not pay.
    """
    import prerun

    swaps = {}
    for path, new in selector.new_sources.items():
        planned = prerun.plan_swaps(str(SRC / path), selector.source(path), new)
        if planned is None:
            log(f"  prerun: {path} is not a pure function-body change")
            return numbers
        swaps[path] = planned
    if not swaps:
        return numbers

    if COSTS is None:
        return numbers
    cheap = [
        n for n in numbers
        if ms[n] <= CHEAP_MS and runner.runnable(nodeid_of[n]) and COSTS.test_ms(nodeid_of[n]) is not None
    ]
    if not cheap:
        return numbers

    if PROBE and len(selector.new_sources) == 1:
        # Infection probe (weak mutation): if the one changed expression runs many
        # times across every cheap candidate test and never yields a different value
        # than the original, the patch cannot change any outcome on those inputs:
        # print nothing. Rare infections exist (1 in 469 in one labeled patch), so the
        # probe covers all cheap candidates, never a sample of them.
        import probe

        (path, new), = selector.new_sources.items()
        plan = probe.plan_probe(str(SRC / path), selector.source(path), new)
        if plan is not None:
            order = sorted(cheap, key=lambda n: COSTS.test_ms(nodeid_of[n]))
            probe_cost = COSTS.run_ms([nodeid_of[n] for n in order])
            schedule_cost = commit_cost_ms(numbers, ms)
            if probe_cost < schedule_cost:
                cpu0 = _cpu()
                out = prerun.probe_run(
                    runner, path, plan, [nodeid_of[n] for n in order], 15 + 3 * probe_cost / 1000,
                    cpu_budget_s=0.8 * schedule_cost / 1000,
                )
                COSTS.imported |= {nodeid_of[n].split("::", 1)[0] for n in order}
                cpu1 = _cpu()
                log(f"  probe: {out} over {len(order)} tests, cpu {sum(cpu1) - sum(cpu0):.2f}s (expected {probe_cost / 1000:.2f}s, schedule {schedule_cost / 1000:.2f}s)")
                if (
                    out and out.get("complete") and not out["failed"]
                    and out["infected"] == 0 and out["reached"] >= PROBE_MIN_REACHED
                ):
                    log(f"  probe: never infected in {out['reached']} evaluations -> none")
                    return []
            else:
                log(f"  probe: not worth it (probe ~{probe_cost / 1000:.1f}s vs schedule {schedule_cost / 1000:.1f}s)")

    if PROBE_AUDIT:
        # Validation only: run an infection probe over the cheap candidates and log
        # how often the changed expression ran and whether it ever changed value.
        import probe

        if len(selector.new_sources) == 1:
            (path, new), = selector.new_sources.items()
            plan = probe.plan_probe(str(SRC / path), selector.source(path), new)
            if plan is not None:
                order = sorted(cheap, key=lambda n: COSTS.test_ms(nodeid_of[n]))
                out = prerun.probe_run(runner, path, plan, [nodeid_of[n] for n in order], 60 + 4 * sum(ms[n] for n in order) / 1000)
                log(f"  PROBE {json.dumps(out)} tests={len(order)}")
        return numbers

    if AUDIT:
        # Validation only: run every cheap candidate and log exactly what the runner
        # says, to compare test by test with labels from the real suite.
        by_nodeid = {nodeid_of[n]: n for n in cheap}
        failed = runner.run(swaps, list(by_nodeid), 30 + 4 * sum(ms[n] for n in cheap) / 1000)
        log(f"  AUDIT ran={sorted(cheap)} failed={sorted(by_nodeid[x] for x in failed if x in by_nodeid)}")
        return numbers

    if ELIDE_BIG and len(cheap) < len(numbers):
        # Giant-test skip (off unless BR_ELIDE=1): run every cheap candidate; if none
        # fails, the change survived every cheap test that runs it, and we drop the
        # slow candidates too. Only justified by measured data: in labeled mutants,
        # a slow test never failed unless a cheap candidate also failed.
        run_cost = COSTS.run_ms([nodeid_of[n] for n in cheap])
        if run_cost < commit_cost_ms(numbers, ms):
            by_nodeid = {nodeid_of[n]: n for n in cheap}
            failed = runner.run(swaps, list(by_nodeid), 5 + 4 * sum(ms[n] for n in cheap) / 1000)
            COSTS.imported |= {n.split("::", 1)[0] for n in by_nodeid}
            failed_numbers = {by_nodeid[x] for x in failed if x in by_nodeid}
            if not failed_numbers:
                log(f"  prerun+skip: all {len(cheap)} cheap candidates pass; dropping {len(numbers) - len(cheap)} slow ones")
                return []
            slow = [n for n in numbers if n not in set(cheap)]
            log(f"  prerun+skip: {len(failed_numbers)} cheap fail; keeping them and {len(slow)} slow ones")
            return sorted(set(slow) | failed_numbers)
    # Run only the cheap tests whose CPU pays for itself: drop the costliest to run
    # first, while that keeps the saving above the cost.
    cheap.sort(key=lambda n: COSTS.test_ms(nodeid_of[n]))
    best = None
    cuts = range(len(cheap), 0, -1)
    if len(cheap) > 12:  # packing is not free: try a dozen cut points, not all of them
        cuts = sorted({max(1, round(len(cheap) * f)) for f in (1, .9, .8, .7, .6, .5, .4, .3, .2, .1, .05)}, reverse=True)
    for cut in cuts:
        run = cheap[:cut]
        keep = [n for n in numbers if n not in set(run)]
        run_cost = COSTS.run_ms([nodeid_of[n] for n in run])
        saving = commit_cost_ms(numbers, ms) - commit_cost_ms(keep, ms)
        if saving - run_cost > 0 and (best is None or saving - run_cost > best[0]):
            best = (saving - run_cost, run, keep, run_cost, saving)
    if best is None:
        log(f"  prerun: not worth it for {len(cheap)} cheap candidates")
        return numbers
    _, cheap, keep, run_cost, saving = best
    log(f"  prerun: expect cost {run_cost / 1000:.2f}s to save up to {saving / 1000:.2f}s")

    deadline = 15 + 3 * run_cost / 1000
    by_nodeid = {nodeid_of[n]: n for n in cheap}
    cpu0 = _cpu()
    failed = runner.run(swaps, list(by_nodeid), deadline)
    COSTS.imported |= {n.split("::", 1)[0] for n in by_nodeid}
    cpu1 = _cpu()
    failed_numbers = [by_nodeid[x] for x in failed if x in by_nodeid]
    log(
        f"  prerun: ran {len(cheap)} cheap tests ({sum(ms[n] for n in cheap) / 1000:.2f}s recorded), "
        f"{len(failed_numbers)} failed; keeping {len(keep)} slow ones; "
        f"cpu parent {cpu1[0] - cpu0[0]:.2f}s child {cpu1[1] - cpu0[1]:.2f}s"
    )
    return sorted(set(keep) | set(failed_numbers))


# --------------------------------------------------------------------------- main


def private_stdout():
    """Keep the answer channel to ourselves.

    Anything else that writes to stdout (a library warning, an import-time print)
    would corrupt the answers and reject the run. Duplicate the real stdout for our
    answers, then point file descriptor 1 and sys.stdout at stderr."""
    sys.stdout.flush()
    answers = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return answers


def main() -> None:
    answers = private_stdout()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_numbers = [t["number"] for t in tests]

    try:
        m = Map(MAP_PATH)
    except Exception as e:  # no map: never miss, run everything
        log(f"map unavailable ({e}); running every test")
        m = None

    number_of = {t["id"]: t["number"] for t in tests}
    unmapped = []
    to_number: dict[int, int] = {}
    if m is not None:
        for i, nodeid in enumerate(m.tests):
            if nodeid in number_of:
                to_number[i] = number_of[nodeid]
        mapped = set(to_number.values())
        unmapped = [n for n in all_numbers if n not in mapped]
        if unmapped:
            log(f"{len(unmapped)} tests are not in the map; they run on every non-empty selection")

    selector = Selector(m) if m is not None else None
    runner = None
    if PRERUN and hasattr(os, "fork") and SRC.is_dir() and COSTS_PATH.is_file():
        import prerun

        global COSTS
        COSTS = PrerunCosts(COSTS_PATH)
        runner = prerun.Runner(str(SRC), log=log)
    nodeid_of = {t["number"]: t["id"] for t in tests}

    for patch in sorted((DATA / "commits").glob("*.patch")):
        log(f"{patch.stem}:")
        chosen: set[int] | None = ALL
        if selector is not None:
            try:
                chosen = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:  # any bug of ours must cost time, never a miss
                log(f"  error {e!r} -> all")
                chosen = ALL
        if chosen is ALL:
            numbers = all_numbers
        else:
            numbers = sorted({to_number[i] for i in chosen if i in to_number})
            if numbers and unmapped:
                numbers = sorted(set(numbers) | set(unmapped))
            if runner is not None and numbers and selector.prerun_ok and not unmapped:
                try:
                    numbers = try_prerun(runner, selector, numbers, ms, nodeid_of)
                except Exception as e:  # never a miss: keep the full selection
                    log(f"  prerun error {e!r}; keeping all candidates")
        workers = best_split([(n, ms[n]) for n in numbers])
        log(f"  -> {len(numbers)} tests on {len(workers)} workers")
        answers.write(json.dumps({"commit": patch.stem, "workers": workers}) + "\n")
        answers.flush()


if __name__ == "__main__":
    main()
