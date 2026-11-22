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
        # A def or class starts at its first decorator: a decorator line is part of the
        # statement that defines the function, not of whatever encloses it.
        self.start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])
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
        # Per patch: every function-body statement it changes, as
        # (path, statement's first line, outermost enclosing function's first and last line).
        self.runtime_spots: list[tuple[str, int, int, int]] = []
        # Class-level changes (see resolve_class_level): the attribute map, and per patch
        # the plain class attributes it assigns as (class, names, what the older rule
        # would select, who ran the lambda the edit sits in).
        self.attrs: "AttrMap | None" = None
        self.header_floor: set[int] = set()  # per patch: tests that ran an edited def's lines
        # per patch and Python file, the kinds of change in it: decided by the import-state
        # check ("deferred"), a statement inside a function body ("runtime"), or "other"
        self.file_kinds: dict[str, set[str]] = {}
        self._test_importers: dict[str, set[str]] | None = None
        self._class_base_path = str(CA_BASE_PATH)
        # The comparison map (CmpMap) and, per patch and file, the comparisons whose
        # operator is the file's only change: (line, col, end line, end col, old, new).
        self.cmp: "CmpMap | None" = None
        self.cmp_swaps: dict[str, list[tuple]] = {}
        self._name_lines: dict[str, set[tuple[str, int]]] | None = None
        self.class_spots: list[tuple[str, set[str], set[int] | None, set[int] | None]] = []
        # Per patch: every file it touches, and the new text of changed data files we
        # may want to re-read (the TPC-DS fixture, for the early cutoff).
        self.changed_paths: list[str] = []
        self.data_sources: dict[str, str] = {}
        self.place_calls = 0
        self._class_base: dict | None | bool = False

    def source(self, path: str) -> str | None:
        if path not in self.src_cache:
            p = SRC / path
            self.src_cache[path] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self.src_cache[path]

    def select(self, patch_text: str) -> set[int] | None:
        self.prerun_ok = True
        self.new_sources = {}
        self.runtime_spots = []
        self.class_spots = []
        self.header_floor = set()
        self.cmp_swaps = {}
        self.file_kinds = {}
        self.place_calls = 0
        self.changed_paths = []
        self.data_sources = {}
        files = parse_patch(patch_text)
        if not files:
            log("  no files in patch -> all")
            self.prerun_ok = False
            return ALL
        chosen: set[int] = set()
        for fp in files:
            self.changed_paths.append(fp.path)
            got = self.select_file(fp)
            if got is ALL:
                self.prerun_ok = False
                return ALL
            chosen |= got
        if self.class_spots:
            got = self.resolve_class_level()
            if got is ALL:
                return ALL
            chosen |= got
        return chosen

    def class_baseline(self) -> dict | None:
        if self._class_base is False:
            import classstate

            self._class_base_path = str(CA_BASE_PATH)
            self._class_base = classstate.load_baseline(self._class_base_path)
            if self._class_base is None:
                # The image should carry it. If it does not (the build step failed, or
                # another interpreter made it), make it now: under a second of CPU,
                # against selecting every test for every import-time change.
                import subprocess
                import tempfile

                out = os.path.join(tempfile.gettempdir(), "br_class_state.json")
                env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
                try:
                    subprocess.run(
                        [sys.executable, str(APP / "classstate.py"), "baseline", str(SRC.resolve()), out],
                        env=env, capture_output=True, timeout=30,
                    )
                    self._class_base = classstate.load_baseline(out)
                except Exception as e:  # noqa: BLE001
                    log(f"class-state baseline could not be made ({e!r})")
                if self._class_base is not None:
                    self._class_base_path = out
                    log("class-state baseline was missing from the image: made one at run time")
        return self._class_base

    def resolve_class_level(self) -> set[int] | None:
        """The tests for this patch's class-level assignments.

        A class-body statement only acts through the state it leaves after import. So:
        import the patched code in a child, list the class attributes whose value differs
        from the pinned commit's (classstate.py: the edited attribute plus anything
        derived from it at import, in ANY class), and run the tests that read one of them
        (the attribute map).

        The older rule (every test that used the class or a subclass) is both more
        expensive and less safe: it misses the tests of a class that merely copies the
        edited attribute while it is imported (Presto building its table from MySQL's).
        So the check always runs when it can. The older rule is only the fallback when
        the check cannot run or finish, or when the patch also changes other Python code.
        An import error, or a change to state the map cannot observe, selects every test."""
        olds = [old for _, _, old, _ in self.class_spots]
        old_union: set[int] | None = ALL if any(o is ALL for o in olds) else set().union(*olds)
        # class spots are "pkg.mod.Class" + ":" + NAME, module spots "pkg.mod:" + ":" + NAME
        edited = {f"{qual}:{name}" for qual, names, _, _ in self.class_spots for name in names}

        def give_up(why: str):
            # The older rule can leave failing tests out (it misses state other classes
            # derive at import, and a patch that hangs or breaks the import fails every
            # test). When the check was possible but did not answer: every test.
            log(f"  class attributes {sorted(edited)[:3]}: {why} -> all")
            return ALL

        if not CLASSATTR or self.attrs is None:
            return ALL  # nothing is sent here without the attribute map; never the older rule
        base = self.class_baseline()
        if base is None:
            return give_up("no class-state baseline, and none could be made")
        import classstate

        # Which files to import patched: all of them, except a file whose every change is
        # a statement inside a function body. Such a statement does not run at import
        # (place() sends the ones that do to "all"), its tests come from the line map,
        # and importing it patched would only make the changed function, and every
        # table that holds it, show up as "changed". A file that mixes both kinds is
        # imported patched: that only adds tests.
        overrides = {p: text for p, text in self.new_sources.items() if self.file_kinds.get(p) != {"runtime"}}
        cost = max(0.5, float(base.get("cpu_s", 2.0)))
        remaining = CA_DEADLINE_S - (_wall() - _STARTED)
        wall = min(5 + 5 * cost, remaining)
        if wall < 3 + 2 * cost:
            return give_up("out of time budget")
        # CPU limit: generous (about 6x a normal check), because no answer now means
        # "every test"; a patch that hangs the import still stops after a few seconds.
        verdict = classstate.run_check(str(SRC.resolve()), self._class_base_path, overrides, wall, 2 + 4 * cost, log)
        if verdict is None:
            return give_up("the import-state check gave no verdict (timeout or crash)")
        if not verdict.get("ok"):
            if "import error" in str(verdict.get("why", "")):
                return give_up(f"sqlglot no longer imports ({verdict['why'][:120]})")
            return give_up(f"the import-state check failed ({verdict.get('why')})")
        changed = set(verdict["changed"])
        # A module-level name that is gone (deleted or renamed): a test FILE that imports
        # it by name no longer loads, and all of its tests fail, whether or not they
        # ever ran a line that mentions the name.
        gone = sorted(k for k in verdict.get("removed", ()) if "::" in k)
        if gone:
            return give_up(f"removes module-level names {gone[:3]} that other files may import")
        # A class attribute that is new can shadow one the class inherited: the tests
        # that read the inherited one through this class are recorded under its parent.
        new_attrs = sorted(k for k in verdict.get("added", ()) if "::" not in k)
        if new_attrs:
            return give_up(f"adds class attributes {new_attrs[:3]} that may override inherited ones")
        was = "all" if old_union is ALL else len(old_union)
        kinds, ranges = verdict.get("kinds", {}), verdict.get("ranges", {})
        # Who can see each changed thing.
        extra: set[int] = set(self.header_floor)
        attr_keys: set[str] = set()
        unseen = []
        for k in sorted(changed):
            if k in self.attrs.wrapped:
                attr_keys.add(k)  # a class attribute the map observes: its readers
            elif kinds.get(k) == "func" and k in ranges:
                for rel, first, last in ranges[k]:  # a function: whoever ran its lines
                    extra |= self.m.line_tests(rel, first, last)
            elif "::" in k and kinds.get(k) in ("data", "code"):
                extra |= self.name_line_tests(k.split("::", 1)[1])  # a module global: by name
            else:
                unseen.append(k)
        if unseen:
            log(f"  class attributes {sorted(edited)[:3]}: changes state nothing here can observe ({unseen[:3]}) -> all")
            return ALL
        module_tables = any("::" in k for k in changed)
        # Every edit inside a lambda of a table: the tests that ran those lambdas' lines.
        callers: set[int] | None = None
        if all(c is not None for _, _, _, c in self.class_spots):
            callers = set().union(*(c for _, _, _, c in self.class_spots)) | self.attrs.always
        tests = self.attrs.readers(attr_keys | {k for k in edited if "::" not in k}) | self.attrs.always | extra
        if verdict.get("code_only") and callers is not None and module_tables:
            # Bodies of functions in a module-level table: there is no reader map for
            # module globals, so the tests are the ones that ran those functions' lines.
            log(f"  module table {sorted(changed)[:3]}: only the body of a table entry changed -> {len(callers)} tests ran the entry (older rule: {was}); check cpu {verdict.get('cpu_s')}s")
            return callers
        if verdict.get("code_only") and callers is not None:
            # Only the body of a function stored in a table changed (same keys, same
            # defaults and closure). To see that, a test must both read a table that
            # holds the function and run the function's lines. (The line map alone
            # also credits a test that merely imported the file while it was recorded.)
            called = callers & tests
            log(f"  class attributes {sorted(changed)[:3]}: only the body of a table entry changed -> {len(called)} tests read the table and ran the entry (readers {len(tests)}, older rule: {was}); check cpu {verdict.get('cpu_s')}s")
            return called
        log(f"  class attributes changed: {sorted(changed)[:4]}{' ...' if len(changed) > 4 else ''} -> {len(tests)} tests read them (older rule: {was}); check cpu {verdict.get('cpu_s')}s")
        return tests

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
        if path == TPCDS_FIXTURE_PATH and not fp.binary and fp.old_path == fp.new_path:
            old = self.source(path)
            if old is not None:
                try:
                    self.data_sources[path] = "\n".join(apply_hunks(old.split("\n"), fp.hunks))
                except PatchError:
                    pass
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
            log(f"  {path}: syntax error after patch -> {'its tests and its importers' if self.is_test_module(path) else 'all'}")
            return self.test_module_and_importers(path) if self.is_test_module(path) else ALL
        if same_tree(old, new):
            log(f"  {path}: same compiled code (comments/formatting) -> none")
            return set()
        self.new_sources[path] = new
        if self.cmp is not None and path.startswith("sqlglot/"):
            swaps = comparison_swaps(old, new)
            if swaps:
                self.cmp_swaps[path] = swaps

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
                deferred, runtime = len(self.class_spots), len(self.runtime_spots)
                got = self.place(path, stmts, lo, hi)
                if got is ALL:
                    return ALL
                kind = "deferred" if len(self.class_spots) > deferred else "runtime" if len(self.runtime_spots) > runtime else "other"
                self.file_kinds.setdefault(path, set()).add(kind)
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

    def test_importers(self) -> dict[str, set[str]]:
        """For each file under tests/: the files under tests/ that import it, directly or
        through another one (read from the pinned source, once per run)."""
        if self._test_importers is not None:
            return self._test_importers
        modules: dict[str, str] = {}
        root = SRC / "tests"
        for p in sorted(root.rglob("*.py")) if root.is_dir() else ():
            rel = p.relative_to(SRC).as_posix()
            name = rel[:-3].replace("/", ".")
            modules[name[: -len(".__init__")] if name.endswith(".__init__") else name] = rel
        direct: dict[str, set[str]] = {}
        for name, rel in modules.items():
            try:
                tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                continue
            package = name if rel.endswith("__init__.py") else name.rsplit(".", 1)[0]
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        parts = package.split(".")
                        base = ".".join(parts[: len(parts) - node.level + 1] + ([node.module] if node.module else []))
                    targets = [base] + [f"{base}.{a.name}" for a in node.names]
                for t in targets:
                    # importing a.b.c imports a and a.b as well
                    bits = t.split(".")
                    for i in range(1, len(bits) + 1):
                        hit = modules.get(".".join(bits[:i]))
                        if hit is not None and hit != rel:
                            direct.setdefault(hit, set()).add(rel)
        closed: dict[str, set[str]] = {}
        for start in direct:
            seen: set[str] = set()
            todo = [start]
            while todo:
                for who in direct.get(todo.pop(), ()):
                    if who not in seen:
                        seen.add(who)
                        todo.append(who)
            closed[start] = seen
        self._test_importers = closed
        return closed

    def test_module_and_importers(self, path: str) -> set[int]:
        """A test module's own tests plus the tests of every test module that imports it:
        `tests/dialects/test_dialect.py` holds the Validator base class of 33 other
        files, so what its import-time code does (or a failure to import it) reaches
        their tests too."""
        out = self.whole_test_file(path)
        for other in self.test_importers().get(path, ()):
            out |= self.whole_test_file(other)
        return out

    def place(self, path: str, stmts: list[Stmt], lo: int, hi: int) -> set[int] | None:
        """Tests affected by a change at old lines lo..hi of a Python file."""
        self.place_calls += 1
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
            swaps = [w for w in self.cmp_swaps.get(path, ()) if s.start <= w[0] <= s.end]
            if swaps and self.cmp is not None:
                # The file's only change is the operator of these comparisons. A test in
                # which the old and the new operator agree on every pair of operands it
                # ever compared here computes exactly what it computed before.
                kept = self.cmp.narrow(path, got, swaps)
                if kept is None:
                    log(f"  {path}:{lo}: comparison operator changed, but the comparison map has no record of it: all {len(got)} kept")
                else:
                    log(f"  {path}:{lo}: comparison operator {swaps[0][4]} -> {swaps[0][5]}: {len(kept)} of {len(got)} tests ever saw operands on which they differ")
                    got = kept
            outer, p = s.func, s.parent
            while p is not None:
                if isinstance(p.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    outer = p.node
                p = p.parent
            first = min([outer.lineno] + [d.lineno for d in outer.decorator_list])
            self.runtime_spots.append((path, s.start, first, outer.end_lineno or outer.lineno))
            return got

        # Between two statements of a function's own body (lines inserted there, or a
        # change that spans several of its statements): it can only matter to a test
        # that runs the function. Everything the function's body ran is the answer.
        if (
            isinstance(s.node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and s.func is None
            and lo >= s.node.body[0].lineno
        ):
            first_body = s.node.body[0].lineno
            if m.ran_at_import(path, first_body, s.end):
                log(f"  {path}:{lo}: inside {s.node.name}, which runs at import time -> all")
                return ALL
            got = m.line_tests(path, first_body, s.end)
            log(f"  {path}:{lo}: between statements of {s.node.name} -> {len(got)} tests that ran it")
            self.runtime_spots.append((path, lo, s.start, s.end))
            return got

        # Everything below runs at import time: not something a code swap can try.
        self.prerun_ok = False

        # A def or class header itself (signature, defaults, decorators, bases).
        if isinstance(s.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and lo <= (s.node.body[0].lineno - 1):
            ran = m.line_tests(path, s.start, s.end)
            got = ran | (self.names_users(defined_names(s.node)) or set())
            log(f"  {path}:{lo}: def header {s.node.name} -> {len(got)} tests")
            if self.attrs is not None and s.func is None and path.startswith("sqlglot/"):
                # A def at module or class level: it runs once, at import, and leaves a
                # function object behind. Decided from what the patch changes once
                # everything is imported; whoever ran the function's lines is the floor.
                self.class_spots.append((f"<def {module_name(path)}.{s.node.name}>", set(), got, None))
                self.header_floor |= ran
                return set()
            if s.func is None and path.startswith("sqlglot/"):
                # no attribute map, so no import-state check: nothing vouches for it
                log(f"  {path}:{lo}: def header at import time, no import-state check available -> all")
                return ALL
            if s.func is None and path.startswith("tests/"):
                # The header of a def at module or class level in the test tree runs when
                # the file is imported (decorators, default values): if it raises, the
                # file does not import, and neither does any test file that imports it.
                if not self.is_test_module(path):
                    log(f"  {path}:{lo}: test helper def header -> all")
                    return ALL
                got |= self.test_module_and_importers(path)
                log(f"  {path}:{lo}: ... in a test module: with it and its importers -> {len(got)} tests")
            return got

        if self.is_test_module(path):
            # A test module's top-level statement runs when pytest imports the file. A
            # definition or constant only affects that file's tests, but a statement that
            # runs code (`if __name__ ...: unittest.main()`, a call, an import, a loop) can
            # exit or crash the whole pytest session: every test.
            top = s
            while top.parent is not None and not isinstance(top.parent.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top = top.parent
            if top.func is None and not isinstance(
                top.node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)
            ):
                log(f"  {path}:{lo}: test module top-level {type(top.node).__name__} runs at import -> all")
                return ALL
            got = self.test_module_and_importers(path)
            log(f"  {path}:{lo}: test module import-time code -> the {len(got)} tests of it and of the test modules that import it")
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
            old = self.class_level_older_rule(path, lo, s, qual)
            defined = defined_names(s.node)
            if (
                self.attrs is not None
                and isinstance(s.node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and defined
                and all(f"{qual}:{name}" in self.attrs.wrapped for name in defined)
            ):
                # A plain class attribute the recorder could observe: decided once for
                # the whole patch, in resolve_class_level().
                # If the edit sits inside a lambda of the table, also note who called it.
                lam = None
                for node in ast.walk(s.node):
                    if isinstance(node, ast.Lambda) and node.lineno <= lo and hi <= (node.end_lineno or node.lineno):
                        if lam is None or (node.end_lineno - node.lineno) <= (lam.end_lineno - lam.lineno):
                            lam = node
                callers = None if lam is None else m.line_tests(path, lam.lineno, lam.end_lineno or lam.lineno)
                self.class_spots.append((qual, defined, old, callers))
                return set()
            # Anything else in a class body: an enum member, the class line itself (name,
            # bases), a statement that is not a plain assignment, an attribute the
            # recorder could not observe, lines inserted between two statements. The
            # import-state check cannot vouch for these (it does not see bases or special
            # names), and "the tests that used the class" misses what other classes
            # derive from it and a change that stops sqlglot from importing at all.
            log(f"  {path}:{lo}: class-level {type(s.node).__name__} the import-state check cannot vouch for -> all")
            return ALL

        old = self.module_level_older_rule(path, lo, s)
        defined = defined_names(s.node)
        targets_are_names = all(
            isinstance(x, ast.Name)
            for x in (getattr(s.node, "targets", None) or [getattr(s.node, "target", None)])
        )
        if self.attrs is not None and isinstance(s.node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and defined and targets_are_names:
            # A module-level constant or table: decided with the class-level ones, from
            # what the patch changes once everything is imported.
            lam = None
            for node in ast.walk(s.node):
                if isinstance(node, ast.Lambda) and node.lineno <= lo and hi <= (node.end_lineno or node.lineno):
                    if lam is None or (node.end_lineno - node.lineno) <= (lam.end_lineno - lam.lineno):
                        lam = node
            callers = None if lam is None else m.line_tests(path, lam.lineno, lam.end_lineno or lam.lineno)
            self.class_spots.append((f"{module_name(path)}:", defined, old, callers))
            return set()
        if old is not ALL:
            log(f"  {path}:{lo}: module-level statement the import-state check cannot decide -> all")
        return ALL

    def module_level_older_rule(self, path: str, lo: int, s: Stmt) -> set[int] | None:
        """A module-level statement, by who mentions the names it defines (the rule before)."""
        if isinstance(s.node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            got = self.names_users(defined_names(s.node))
            if got is not None:
                log(f"  {path}:{lo}: module constant {sorted(defined_names(s.node))} -> {len(got)} tests")
                return got
        log(f"  {path}:{lo}: module-level {type(s.node).__name__} -> all")
        return ALL

    def name_line_tests(self, name: str) -> set[int]:
        """Tests that ran a function line mentioning `name` (as a name, an attribute or a
        string): the only places a module global can be read at run time. Import-time
        uses are not needed here: what they derive shows up in the import-state diff."""
        if self._name_lines is None:
            index: dict[str, set[tuple[str, int]]] = {}
            for base in ("sqlglot", "tests"):
                for file in sorted((SRC / base).rglob("*.py")):
                    rel = file.relative_to(SRC).as_posix()
                    try:
                        tree = ast.parse(file.read_text(encoding="utf-8"))
                    except (SyntaxError, UnicodeDecodeError, OSError):
                        continue
                    for fn in ast.walk(tree):
                        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                            continue
                        for node in ast.walk(fn):
                            if isinstance(node, ast.Name):
                                word = node.id
                            elif isinstance(node, ast.Attribute):
                                word = node.attr
                            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                                word = node.value
                            else:
                                continue
                            index.setdefault(word, set()).add((rel, node.lineno))
            self._name_lines = index
        out: set[int] = set()
        for rel, line in self._name_lines.get(name, ()):
            out |= self.m.line_tests(rel, line, line)
        return out

    def class_level_older_rule(self, path: str, lo: int, s: Stmt, qual: str) -> set[int] | None:
        """A class-level statement, by who uses the class (the rule before the attribute map)."""
        m = self.m
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
                cpu0 = _cpu()
                out = prerun.probe_run(
                    runner, path, plan, [nodeid_of[n] for n in order], 60 + 3 * COSTS.run_ms([nodeid_of[n] for n in order]) / 1000
                )
                cpu1 = _cpu()
                slow = [n for n in numbers if ms[n] > 2000]
                info = {
                    "probe": out, "tests": len(order),
                    "cpu_s": round(sum(cpu1) - sum(cpu0), 2),
                    "est_s": round(COSTS.run_ms([nodeid_of[n] for n in order]) / 1000, 2),
                    "schedule_all_s": commit_cost_ms(numbers, ms) / 1000,
                    "schedule_without_slow_s": commit_cost_ms([n for n in numbers if ms[n] <= 2000], ms) / 1000,
                    "slow": slow,
                }
                log(f"  PROBE {json.dumps(info)}")
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


# ------------------------------------------------------------- class attributes

# A statement in a class body (a setting, a table) used to select every test that used
# the class: 27-37 s for the core Dialect/Generator/Parser/Tokenizer classes. With
# maps/attr.json.gz (which tests READ which class attribute, recorded per test in
# isolation by tools/build_map/attrrec.py) and classstate.py (which attributes a patch
# changes once everything is imported), only the tests that read a changed attribute run.
# ------------------------------------------------------------- comparison map

# EXPERIMENT (branch exp/value-map). The coverage map says which tests RAN a statement.
# maps/cmp.json.gz (tools/build_map/cmprec.py, recorded at the pinned commit, every test
# alone and once more in a whole-suite run) adds, for every comparison in sqlglot, how
# its two operands related in each test: same object, equal, ordered. It records what
# the tests did, nothing about any patch.
# When a patch only swaps a comparison operator (`is` -> `==`, `<` -> `<=`, ...), a test
# keeps computing exactly what it computed if, for every pair of operands it compares
# there, the old and the new operator give the same answer AND evaluating either operator
# on such operands has no side effect (the recorder only fills in an operator's answer
# when that holds). What a test compares can depend on what ran before it (caches, lazy
# loading) and on the process (hashes, ids), so the map is used cautiously:
#   - a comparison about which the recordings disagree is flagged in the map: never used;
#   - by default (BR_CMP_MODE=site) tests are dropped only when NO test in ANY recording
#     saw operands there on which the two operators disagree or are unknown;
#   - a test with no record at the comparison is always kept.
CMPMAP = os.environ.get("BR_CMP", "1") == "1"
CMP_MAP_PATH = Path(os.environ.get("BR_CMP_MAP", APP / "maps" / "cmp.json.gz"))
_CMP_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
_CMP_NAMES = ("is", "is not", "==", "!=", "<", "<=", ">", ">=")
_CMP_FIELD = (0, 0, 1, 2, 3, 4, 5, 6)  # which field of a relation answers each operator
_CMP_TOKEN = re.compile(r"\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>")


# The operator swaps the comparison map is used for: (old, new).
_CMP_SWAPS = {(0, 2), (1, 3), (4, 5), (5, 4), (6, 7), (7, 6)}
# "site": drop tests only when NO recorded test ever saw operands on which the two
# operators disagree at that comparison (then every test that evaluated it is dropped).
# "test": decide each test from its own recorded operands (more is dropped; it trusts that
# a test sees the same operands whatever ran before it).
CMP_MODE = os.environ.get("BR_CMP_MODE", "site")


def _volatile(node: ast.AST) -> bool:
    """Does this operand look like a hash or an object id? Such values differ between
    processes, so what a recording saw of them says little about the next run."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("hash", "id"):
            return True
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
        if name is not None and "hash" in name.lower():
            return True
    return False


def _cmp_outcome(code: int, op: int) -> int:
    """0 False, 1 True, 2 unknown: what operator `op` gives on operands with this relation."""
    if code >= 2187 or code < 0:
        return 2  # the recorder's mark for "the comparison raised", or anything unexpected
    f = (code // (3 ** _CMP_FIELD[op])) % 3
    return 1 - f if (op == 1 and f != 2) else f


def comparison_swaps(old: str, new: str) -> list[tuple] | None:
    """[(line, col, end line, end col, old operator name, new operator name, old op, new op)]
    when the two sources differ ONLY in the operator of single-operator comparisons
    (positions are the comparison's in `old`); otherwise None."""
    a_lines, b_lines = old.split("\n"), new.split("\n")
    if len(a_lines) != len(b_lines):
        return None
    changed = [i + 1 for i, (x, y) in enumerate(zip(a_lines, b_lines)) if x != y]
    if not changed or len(changed) > 4:
        return None
    for ln in changed:  # cheap filter before any parsing: only operator tokens may differ
        if _CMP_TOKEN.sub("\0", a_lines[ln - 1]) != _CMP_TOKEN.sub("\0", b_lines[ln - 1]):
            return None
    try:
        a, b = ast.parse(old), ast.parse(new)
    except (SyntaxError, ValueError):
        return None
    if len(a.body) != len(b.body):
        return None
    found = []
    for x_top, y_top in zip(a.body, b.body):
        last = getattr(x_top, "end_lineno", None) or x_top.lineno
        first = min([x_top.lineno] + [d.lineno for d in getattr(x_top, "decorator_list", ())])
        if not any(first <= ln <= last for ln in changed):
            continue
        xs = [n for n in ast.walk(x_top) if isinstance(n, ast.Compare)]
        ys = [n for n in ast.walk(y_top) if isinstance(n, ast.Compare)]
        if len(xs) != len(ys):
            return None
        for x, y in zip(xs, ys):
            if [type(o) for o in x.ops] == [type(o) for o in y.ops]:
                continue
            if len(x.ops) != 1 or len(y.ops) != 1:
                return None
            p, q = _CMP_OPS.get(type(x.ops[0])), _CMP_OPS.get(type(y.ops[0]))
            if (p, q) not in _CMP_SWAPS:
                # Only swaps whose two answers differ in ONE way: is/== and is not/!= differ
                # when the operands are equal without being one object; </<= and >/>=
                # differ when the operands are equal. Not used: a swap TOWARDS `is`
                # (whether two equal values are one object is the compiler's and the
                # interpreter's business: literals, small ints, interned strings), and any
                # swap that depends on which operand is the larger one.
                return None
            if _volatile(x.left) or _volatile(x.comparators[0]):
                return None  # hash() / id() values: not the same from one process to the next
            found.append((x.lineno, x.col_offset, x.end_lineno, x.end_col_offset, _CMP_NAMES[p], _CMP_NAMES[q], p, q))
            y.ops = x.ops
        if ast.dump(x_top) != ast.dump(y_top):
            return None  # something else differs as well
    return found or None


class CmpMap:
    def __init__(self, path: Path, m: Map) -> None:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("version") != 2:
            raise ValueError("comparison map version")
        self.flagged: dict[str, set[str]] = {p: set(keys) for p, keys in raw.get("flagged", {}).items()}
        index = {nodeid: i for i, nodeid in enumerate(m.tests)}
        # recorded test -> index in the coverage map (None: a test the coverage map lacks)
        self.to_index = [index.get(t) for t in raw["tests"]]
        self.recorded: set[int] = {i for i in self.to_index if i is not None}
        self.relsets: list[list[int]] = raw["relsets"]
        self.testsets: list[list[int]] = raw["testsets"]
        self.sites: dict[str, dict[str, list]] = raw["sites"]

    def members(self, testset: int):
        total = 0
        for d in self.testsets[testset]:
            total += d
            i = self.to_index[total]
            if i is not None:
                yield i

    def narrow(self, path: str, got: set[int], swaps: list[tuple]) -> set[int] | None:
        """The tests of `got` that must stay; None when the map cannot say anything."""
        by_site = self.sites.get(path)
        if by_site is None:
            return None
        verdicts = []
        flagged = self.flagged.get(path, ())
        for line, col, end_line, end_col, _, _, old, new in swaps:
            key = f"{line}:{col}:{end_line}:{end_col}"
            if key in flagged:
                return None  # the recordings disagree about this comparison: not used
            rows = by_site.get(key)
            if rows is None:
                return None  # no recorded test ever evaluated it (or not a recorded kind)
            differs: dict[int, bool] = {}
            for relset, testset in rows:
                d = False
                for code in self.relsets[relset]:
                    x, y = _cmp_outcome(code, old), _cmp_outcome(code, new)
                    if x == 2 or y == 2 or x != y:
                        d = True
                        break
                for t in self.members(testset):
                    differs[t] = differs.get(t, False) or d
            if CMP_MODE == "site" and any(differs.values()):
                # Some test, somewhere, saw operands on which the two operators disagree
                # (or that we cannot vouch for). Another test could meet the same
                # operands when the suite runs in another order: nobody is dropped.
                return set(got)
            verdicts.append(differs)
        keep = set()
        for t in got:
            if t not in self.recorded:
                keep.add(t)  # no complete recording of this test
                continue
            for differs in verdicts:
                d = differs.get(t)
                if d is None or d:
                    # no record at this comparison (we cannot show what it saw), or it
                    # saw operands on which the two operators differ
                    keep.add(t)
                    break
        return keep


CLASSATTR = os.environ.get("BR_CLASSATTR", "1") == "1"
CA_MAP_PATH = Path(os.environ.get("BR_CA_MAP", APP / "maps" / "attr.json.gz"))
CA_BASE_PATH = Path(os.environ.get("BR_CA_BASE", APP / "maps" / "class_state.json"))


class AttrMap:
    def __init__(self, path: Path, m: Map) -> None:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("version") != 1:
            raise ValueError("attribute map version")
        index = {nodeid: i for i, nodeid in enumerate(m.tests)}
        if set(raw["tests"]) != set(index):
            raise ValueError("the attribute map and the coverage map list different tests")
        self.to_index = [index[t] for t in raw["tests"]]
        self.wrapped: set[str] = set(raw["wrapped"])
        self._readers: dict[str, int] = raw["readers"]
        self._sets: list[list[int]] = raw["sets"]
        self.always: set[int] = {self.to_index[i] for i in raw["always"]}

    def readers(self, keys) -> set[int]:
        out: set[int] = set()
        for key in keys:
            s = self._readers.get(key)
            if s is not None:
                out.update(self.to_index[i] for i in self._sets[s])
        return out


# ------------------------------------------------------------------ early cutoff

# test_execute_tpcds (21.5 s) is scheduled for any change to a line it ran. It has a
# cheap front half (parse, optimize and plan the TPC-DS queries) and an expensive back
# half (execute them). earlycut.py re-runs, on the patched code, only what the patch
# can reach: the front half if the patched function runs there, and the execution of
# just the queries whose back half reaches the patched function. If everything the
# test would compare is identical to the pinned commit's, the test cannot change
# outcome and is dropped. Any difference, error or timeout keeps it.
EARLYCUT = os.environ.get("BR_EARLYCUT", "1") == "1"
# Re-executing queries for a patch to code the back half runs. Audited against the real
# test (work/exp_fronthalf/audit_back.py: 42 of 42 dropped tests pass) and on 135
# labeled patches in the sandbox (0 misses; a full re-execution costs ~15 s CPU against
# 21.5 s + a worker to schedule the test, and a difference usually shows up early).
EC_BACK = os.environ.get("BR_EC_BACK", "1") == "1"
# A front-half patch that changes a query's PLAN can still give the same result. Such a
# query is executed with its new plan as soon as the plan exists, and its result
# compared with the pinned commit's; the first different result ends the check.
# Measured on labeled patches: when a handful of plans change the results are usually
# the same (11 of 12 such checks dropped the test, 1-9 queries each); when most plans
# change it rarely pays (3 of 4 ran out of budget after 17-29 queries). Hence the cap.
EC_PLAN_DIFFS = int(os.environ.get("BR_EC_PLAN_DIFFS", "12"))  # execute up to this many queries whose plan changed
EC_PLAN_DIFF_CPU_S = float(os.environ.get("BR_EC_PLAN_DIFF_CPU", "12"))  # ...within at most this much baseline CPU
EC_PLAN_DIFF_SHARE = float(os.environ.get("BR_EC_PLAN_DIFF_SHARE", "0.6"))  # ...and this share of the remaining saving
# An edit to the TPC-DS fixture file itself: re-run the front half on the patched fixture.
EC_FIXTURE = os.environ.get("BR_EC_FIXTURE", "1") == "1"
TPCDS_FIXTURE_PATH = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
EC_STAGE_PATH = Path(os.environ.get("BR_EC_STAGE", APP / "maps" / "tpcds_stage.json.gz"))
EC_BASE_PATH = Path(os.environ.get("BR_EC_BASE", APP / "maps" / "tpcds_base.json"))
# The whole batch has 180 s of wall time (270 s in the final). Every check, including
# its own timeout, must END before this many seconds after the program started; the
# selection itself takes about a second for the whole batch.
EC_DEADLINE_S = float(os.environ.get("BR_EC_DEADLINE", "145"))
# ...and earlier than that while many commits are still to come: each of them may need
# an import-state check, so that much time is kept back for them.
CA_RESERVE_PER_COMMIT_S = 1.2
_COMMITS_LEFT = 0  # set by main() for each patch: how many come after it
# The import-state check is what keeps class-level and module-level changes from being
# missed, and it is cheap (under a second each), so it gets the time the optional early
# cutoff must leave: it may run until this many seconds. After that such a change
# selects every test (never the older rule, which can leave failing tests out).
CA_DEADLINE_S = float(os.environ.get("BR_CA_DEADLINE", "160"))
# Without the image's baseline the check could compute one at run time, but that costs
# a full run of both halves: only for development outside the image.
EC_RUNTIME_BASELINE = os.environ.get("BR_EC_RUNTIME_BASELINE", "0") == "1"
_STARTED = __import__("time").monotonic()
# Run a check only if dropping the test saves more than the check is expected to cost.
EC_GAIN_FACTOR = 1.15
EC_GAIN_MARGIN_S = 1.0
# Reference CPU seconds of the test's parts at the pinned commit (idle machine, scorer's
# image and limits): the front half, loading the fixture tables, executing all 57 queries.
EC_FRONT_REF_S = 4.3
EC_SETUP_REF_S = 1.0
EC_BACK_REF_S = 11.3


class EarlyCut:
    def __init__(self, tests: list[dict]) -> None:
        import earlycut

        self.mod = earlycut
        self.src = str(SRC.resolve())
        self.stage = earlycut.StageMap(EC_STAGE_PATH)
        self.number = next((t["number"] for t in tests if t["id"] == earlycut.TPCDS_TEST), None)
        self.base: str | None = None
        self.front_cpu = EC_FRONT_REF_S
        self.setup_cpu = EC_SETUP_REF_S
        self.back_cpu: dict[int, float] = {}
        self.back_scale = 1.0
        self.slow = 1.0
        self.tried_base = False
        self.started = _wall()

    def baseline(self) -> str | None:
        if not self.tried_base:
            self.tried_base = True
            import tempfile

            if EC_RUNTIME_BASELINE:
                self.base = self.mod.ensure_baseline(self.src, str(EC_BASE_PATH), tempfile.gettempdir(), log)
            else:
                self.base = str(EC_BASE_PATH)
            raw = self.mod.load_baseline(self.base) if self.base is not None else None
            if raw is None:
                self.base = None
            else:
                # What a check costs is taken from fixed reference figures (measured on an
                # idle machine), not from the CPU the image build happened to measure: a
                # busy build machine would inflate those, and the same commit would then
                # be decided differently from the build that was validated. Only the
                # split of the back half between the queries comes from the baseline.
                measured = {int(q): float(s) for q, s in raw.get("back_cpu", {}).items()}
                total = sum(measured.values())
                self.back_scale = (EC_BACK_REF_S / total) if total > 0 else 1.0
                # The LIMITS that stop a runaway check do follow the build's measurement
                # when it is slower than the reference: on a slower machine a healthy
                # check must still be allowed to finish.
                self.slow = max(1.0, float(raw.get("cpu_s", EC_FRONT_REF_S)) / EC_FRONT_REF_S)
                self.front_cpu = EC_FRONT_REF_S
                self.setup_cpu = EC_SETUP_REF_S
                self.back_cpu = {q: s * self.back_scale for q, s in measured.items()}
        return self.base

    def plan(self, selector: "Selector"):
        """What has to be re-run to decide the test for this patch: (front, queries),
        or None when the early cutoff cannot decide it.

        Only for patches whose every change is a statement inside a sqlglot function
        body (never import-time code), and only when the stage map knows where the
        test runs each of those functions."""
        if EC_FIXTURE and selector.changed_paths == [TPCDS_FIXTURE_PATH] and TPCDS_FIXTURE_PATH in selector.data_sources:
            # Only the TPC-DS fixture changed: the front half is re-run on the patched
            # fixture with the pinned code. Nothing in the back half reads that file.
            return True, set()
        if not selector.prerun_ok or not selector.runtime_spots or not selector.new_sources:
            return None
        front, queries = False, set()
        for path, _stmt_start, func_first, func_last in selector.runtime_spots:
            if path == "sqlglot/executor/__init__.py":
                # execute() itself: the check runs it with a stand-in executor to capture
                # the plan, so what execute() does around the real executor (the tables
                # it hands over, what it returns) is not something the check sees.
                return None
            where = self.stage.classify(path, func_first, func_last)
            if where is None:
                return None
            front = front or where[0]
            queries |= where[1]
        return front, queries

    def apply(self, selector: "Selector", numbers: list[int], ms: dict[int, int]) -> list[int]:
        if self.number is None or self.number not in numbers:
            return numbers
        plan = self.plan(selector)
        if plan is None:
            log("  early cutoff: not decidable for this change")
            return numbers
        front, queries = plan
        if queries and not EC_BACK:
            log("  early cutoff: back-half change (re-execution is off)")
            return numbers
        without = [n for n in numbers if n != self.number]
        saving = (commit_cost_ms(numbers, ms) - commit_cost_ms(without, ms)) / 1000
        if self.baseline() is None:
            log("  early cutoff: no baseline")
            return numbers
        if queries and not all(q in self.back_cpu for q in queries):
            log("  early cutoff: baseline has no back-half results")
            return numbers
        per_query_front = self.front_cpu / 57
        back = sum(self.back_cpu[q] for q in queries)
        if front:
            cost = self.front_cpu + ((self.setup_cpu + back) if queries else 0.0)
        else:
            cost = 0.6 + self.setup_cpu + back + per_query_front * len(queries)
        if saving < EC_GAIN_FACTOR * cost + EC_GAIN_MARGIN_S:
            log(f"  early cutoff: not worth it (saves {saving:.1f}s, costs ~{cost:.1f}s; front={front}, {len(queries)} queries)")
            return numbers
        fixture_text = selector.data_sources.get(TPCDS_FIXTURE_PATH) if selector.changed_paths == [TPCDS_FIXTURE_PATH] else None
        diffs = EC_PLAN_DIFFS if (front and self.back_cpu and fixture_text is None) else 0
        budget = 0.0
        if diffs:
            # How much executing changed-plan queries may cost: a share of what dropping
            # the test would still save after the front half is paid for.
            headroom = (saving - EC_GAIN_MARGIN_S) / EC_GAIN_FACTOR - cost - self.setup_cpu
            budget = max(0.0, min(EC_PLAN_DIFF_CPU_S, EC_PLAN_DIFF_SHARE * headroom))
            if budget <= 0:
                diffs = 0
        limit = cost + (budget + self.setup_cpu if diffs else 0.0)
        job = {
            "overrides": dict(selector.new_sources), "front": front, "queries": sorted(queries),
            "fixture_text": fixture_text,
            "max_plan_diffs": diffs, "plan_diff_cpu_s": budget, "back_scale": self.back_scale,
            # what the front half usually costs: the child gives up on a run that is
            # clearly slower than that (and on any query far slower than its usual)
            "front_cpu_s": self.front_cpu * self.slow,
            # A patch that makes the code loop forever must not cost much: the child is
            # stopped at about three times the expected CPU (rlimit) or 4x the expected
            # wall time, both stretched when this machine measured slower at build time.
            "cpu_limit_s": 1 + 3 * limit * self.slow,
        }
        # The check's wall-clock timeout never reaches past the deadline; if what is left
        # is not clearly enough for a normal run, the test is simply kept.
        cutoff = min(EC_DEADLINE_S, CA_DEADLINE_S - 5.0 - CA_RESERVE_PER_COMMIT_S * _COMMITS_LEFT)
        remaining = cutoff - (_wall() - _STARTED)
        wall_limit = min(5 + 4 * limit * self.slow, remaining)
        if wall_limit < 3 + 2 * limit:
            log(f"  early cutoff: out of time budget ({remaining:.0f}s left, needs ~{limit:.0f}s)")
            return numbers
        log(f"  early cutoff: checking (front={front}, {len(queries)} queries to execute, ~{cost:.1f}s, saves {saving:.1f}s)")
        if not self.mod.run_check(self.src, self.base, job, wall_limit_s=wall_limit, log=log):
            return numbers
        log(f"  early cutoff: identical -> dropping test_execute_tpcds (saves {saving:.1f}s)")
        return without



def _wall() -> float:
    import time

    return time.monotonic()


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
    if selector is not None and CLASSATTR and CA_MAP_PATH.is_file() and SRC.is_dir() and not unmapped:
        try:
            selector.attrs = AttrMap(CA_MAP_PATH, m)
        except Exception as e:  # never a miss: without it the older class rule applies
            log(f"attribute map unavailable ({e!r})")
            selector.attrs = None
    if selector is not None and CMPMAP and CMP_MAP_PATH.is_file() and not unmapped:
        try:
            selector.cmp = CmpMap(CMP_MAP_PATH, m)
        except Exception as e:  # never a miss: without it every test that ran the statement is kept
            log(f"comparison map unavailable ({e!r})")
            selector.cmp = None
    early = None
    if EARLYCUT and selector is not None and SRC.is_dir() and EC_STAGE_PATH.is_file():
        try:
            early = EarlyCut(tests)
        except Exception as e:  # never a miss: without it every candidate is kept
            log(f"early cutoff unavailable ({e!r})")

    global _COMMITS_LEFT
    patches = sorted((DATA / "commits").glob("*.patch"))
    for position, patch in enumerate(patches):
        _COMMITS_LEFT = len(patches) - position - 1
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
            if early is not None and numbers and not unmapped:
                try:
                    numbers = early.apply(selector, numbers, ms)
                except Exception as e:  # never a miss: keep the full selection
                    log(f"  early cutoff error {e!r}; keeping all candidates")
        workers = best_split([(n, ms[n]) for n in numbers])
        log(f"  -> {len(numbers)} tests on {len(workers)} workers")
        answers.write(json.dumps({"commit": patch.stem, "workers": workers}) + "\n")
        answers.flush()


if __name__ == "__main__":
    main()
