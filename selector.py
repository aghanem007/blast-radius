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
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
MAP_PATH = Path(os.environ.get("BR_MAP", APP / "maps" / "map.json.gz"))
SRC = Path(os.environ.get("BR_SRC", APP / "src"))

MAX_WORKERS = 8
WORKER_COST_MS = 2000
ALL = None  # sentinel: select every test

# Config files that change how pytest itself runs.
PYTEST_CONFIG = {"pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py"}


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


def same_tree(a: str, b: str) -> bool:
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


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

    def source(self, path: str) -> str | None:
        if path not in self.src_cache:
            p = SRC / path
            self.src_cache[path] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self.src_cache[path]

    def select(self, patch_text: str) -> set[int] | None:
        files = parse_patch(patch_text)
        if not files:
            log("  no files in patch -> all")
            return ALL
        chosen: set[int] = set()
        for fp in files:
            got = self.select_file(fp)
            if got is ALL:
                return ALL
            chosen |= got
        return chosen

    def select_file(self, fp: FilePatch) -> set[int] | None:
        path = fp.path
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
        try:
            ast.parse(new)
        except SyntaxError:
            log(f"  {path}: syntax error after patch -> {'its tests' if self.is_test_module(path) else 'all'}")
            return self.whole_test_file(path) if self.is_test_module(path) else ALL
        if same_tree(old, new):
            log(f"  {path}: same syntax tree (comments/formatting) -> none")
            return set()

        tree = ast.parse(old)
        stmts = index_statements(tree)
        out: set[int] = set()
        for old_start, body in fp.hunks:
            # skip hunks that on their own leave the syntax tree unchanged
            try:
                alone = "\n".join(apply_hunks(old_lines, [(old_start, body)]))
            except PatchError:
                return ALL
            if same_tree(old, alone):
                continue
            removed, after = hunk_changes(old_start, body)
            spots = [(ln, ln) for ln in removed] + [(a, a + 1) for a in after]
            for lo, hi in spots:
                got = self.place(path, stmts, lo, hi)
                if got is ALL:
                    return ALL
                out |= got
        return out

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
        if cls_path is not None:
            qual = f"{module_name(path)}.{cls_path}"
            users = m.set_of(m.classes, qual)
            if users is not None:
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


# --------------------------------------------------------------------------- main


def main() -> None:
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
        workers = best_split([(n, ms[n]) for n in numbers])
        log(f"  -> {len(numbers)} tests on {len(workers)} workers")
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
