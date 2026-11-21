"""Turn the recorder's raw output into the compact map the selector ships with.

Run inside the map-builder image after the recorded pytest run:
    python /tools/build_map.py /out /out/map.json.gz

Map layout (JSON, gzipped). Every test set is an index into "sets",
and each set is a sorted list of indexes into "tests" (pytest node ids):

  tests    [nodeid, ...]
  sets     [[test index, ...], ...]
  lines    {relpath: [[line, set], ...]}  tests that ran the line (setup/teardown of
                                          a class's tests count for the whole class)
  reads    {relpath: set}                 tests that opened the file; "dir/" = listed the dir
  classes  {module.QualName: set}         tests that ran a method with self/cls of that
                                          class or a subclass
  names    {name: set}                    tests that ran a function or lambda that refers
                                          to the name, for names assigned at module or
                                          class level
"""

from __future__ import annotations

import ast
import glob
import gzip
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData

REPO = "/sqlglot"


class Sets:
    """Interns frozensets of test indexes."""

    def __init__(self) -> None:
        self.index: dict[frozenset, int] = {}
        self.items: list[list[int]] = []

    def add(self, tests) -> int:
        key = frozenset(tests)
        found = self.index.get(key)
        if found is None:
            found = self.index[key] = len(self.items)
            self.items.append(sorted(key))
        return found


def split_ctx(ctx: str) -> tuple[str, str]:
    nodeid, _, phase = ctx.rpartition("|")
    return nodeid, phase


def group_of(nodeid: str) -> str:
    """Class (or module, for plain functions) that a test belongs to."""
    return nodeid.rsplit("::", 1)[0]


def main(raw_dir: str, out_path: str) -> None:
    datas = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "cov.*"))):
        data = CoverageData(basename=path)
        data.read()
        datas.append(data)

    nodeids: set[str] = set()
    for data in datas:
        for ctx in data.measured_contexts():
            nodeid, phase = split_ctx(ctx)
            if phase in ("setup", "run", "teardown"):
                nodeids.add(nodeid)
    tests = sorted(nodeids)
    idx = {t: i for i, t in enumerate(tests)}
    groups: dict[str, set[int]] = defaultdict(set)
    for t, i in idx.items():
        groups[group_of(t)].add(i)

    def tests_for(ctx: str) -> set[int]:
        nodeid, phase = split_ctx(ctx)
        if phase == "run":
            return {idx[nodeid]}
        if phase in ("setup", "teardown"):
            # setUpClass / tearDownClass run inside one test's setup/teardown but
            # serve every test of the class.
            return groups[group_of(nodeid)] | {idx[nodeid]}
        if nodeid.startswith("collect"):
            return set()
        return set()

    ctx_cache: dict[str, set[int]] = {}

    def cached(ctx: str) -> set[int]:
        found = ctx_cache.get(ctx)
        if found is None:
            found = ctx_cache[ctx] = tests_for(ctx)
        return found

    sets = Sets()
    lines: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    # Lines that ran while modules were being imported (collection, or before any
    # test): a change there can affect every test that imports the module.
    import_lines: dict[str, set[int]] = defaultdict(set)
    for data in datas:
        for path in data.measured_files():
            rel = os.path.relpath(path, REPO).replace(os.sep, "/")
            for line, ctxs in data.contexts_by_lineno(path).items():
                bucket = lines[rel][line]
                for ctx in ctxs:
                    if ctx == "" or ctx.startswith("collect|"):
                        import_lines[rel].add(line)
                    else:
                        bucket |= cached(ctx)

    # reads and classes from every worker's extra file
    reads: dict[str, set[int]] = defaultdict(set)
    classes: dict[str, set[int]] = defaultdict(set)

    def ctx_tests(ctx: str) -> set[int]:
        nodeid, phase = split_ctx(ctx)
        if nodeid == "collect" or ctx.startswith("collect|"):
            module = ctx.split("|", 1)[1]
            if not module:
                return set()
            prefix = module + "::"
            return {i for t, i in idx.items() if t.startswith(prefix) or t == module}
        return cached(ctx)

    for path in sorted(glob.glob(os.path.join(raw_dir, "extra.*.json"))):
        extra = json.load(open(path))
        for ctx, paths in extra["reads"].items():
            who = ctx_tests(ctx)
            for p in paths:
                reads[p] |= who
        for ctx, names in extra["classes"].items():
            who = ctx_tests(ctx)
            for n in names:
                classes[n] |= who

    names = build_name_index(lines, classes)

    out = {
        "tests": tests,
        "lines": {
            f: [[ln, sets.add(ts)] for ln, ts in sorted(by_line.items()) if ts]
            for f, by_line in sorted(lines.items())
        },
        "import_lines": {f: sorted(ls) for f, ls in sorted(import_lines.items())},
        "reads": {p: sets.add(ts) for p, ts in sorted(reads.items()) if ts},
        "classes": {c: sets.add(ts) for c, ts in sorted(classes.items()) if ts},
        "names": {n: sets.add(ts) for n, ts in sorted(names.items()) if ts},
    }
    out["sets"] = sets.items
    with gzip.open(out_path, "wt", compresslevel=9) as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"tests={len(tests)} files={len(out['lines'])} sets={len(sets.items)} "
        f"reads={len(out['reads'])} classes={len(out['classes'])} names={len(out['names'])} "
        f"size={os.path.getsize(out_path) / 1e6:.2f} MB",
        file=sys.stderr,
    )


def refs_of(node) -> set[str]:
    refs = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            refs.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            refs.add(sub.attr)
    return refs


def build_name_index(lines, classes) -> dict[str, set[int]]:
    """For each name defined at module or class level, the tests that depend on it.

    A test depends on a name when it ran a function or lambda that refers to it, or
    used a class whose body refers to it at import time (a class attribute built
    from a module constant), or depends on a module-level name built from it.
    Names are matched by spelling only, across all modules: a safe over-estimate.
    """
    assigned: set[str] = set()
    units: list[tuple[str, int, int, set[str]]] = []
    class_refs: list[tuple[str, set[str]]] = []  # (class qualname, names its body uses at import)
    module_refs: list[tuple[set[str], set[str]]] = []  # (names a module stmt defines, names it uses)
    for path in glob.glob(os.path.join(REPO, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(
        os.path.join(REPO, "tests", "**", "*.py"), recursive=True
    ):
        rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        collect_assigned(tree.body, assigned)
        collect_import_time_refs(tree.body, module, None, class_refs, module_refs)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                units.append((rel, node.lineno, node.end_lineno or node.lineno, refs_of(node)))

    names: dict[str, set[int]] = defaultdict(set)
    for rel, start, end, refs in units:
        wanted = refs & assigned
        if not wanted:
            continue
        by_line = lines.get(rel)
        if not by_line:
            continue
        covered: set[int] = set()
        for ln in range(start, end + 1):
            ts = by_line.get(ln)
            if ts:
                covered |= ts
        if covered:
            for n in wanted:
                names[n] |= covered

    for qualname, refs in class_refs:
        users = classes.get(qualname)
        if users:
            for n in refs & assigned:
                names[n] |= users

    # A module-level name built from another inherits its dependents.
    changed = True
    while changed:
        changed = False
        for defined, used in module_refs:
            dependents: set[int] = set()
            for d in defined:
                dependents |= names.get(d, set())
            if not dependents:
                continue
            for n in used & assigned:
                before = len(names[n])
                names[n] |= dependents
                changed |= len(names[n]) != before
    return names


def collect_import_time_refs(body, module, cls, class_refs, module_refs) -> None:
    """Record names used by statements that run at import (outside any function)."""
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            qual = f"{cls}.{stmt.name}" if cls else f"{module}.{stmt.name}"
            header = set()
            for part in stmt.bases + stmt.keywords + stmt.decorator_list:
                header |= refs_of(part)
            class_refs.append((qual, header))
            collect_import_time_refs(stmt.body, module, qual, class_refs, module_refs)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            used = set()
            for part in stmt.decorator_list + stmt.args.defaults + [
                d for d in stmt.args.kw_defaults if d is not None
            ]:
                used |= refs_of(part)
            if cls:
                class_refs.append((cls, used))
            else:
                module_refs.append(({stmt.name}, used))
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        else:
            used = set()
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(sub, ast.Name):
                    used.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    used.add(sub.attr)
            if cls:
                class_refs.append((cls, used))
            else:
                defined = set()
                for target in getattr(stmt, "targets", []) + [getattr(stmt, "target", None)]:
                    if target is not None:
                        defined |= {s.id for s in ast.walk(target) if isinstance(s, ast.Name)}
                module_refs.append((defined, used))


def collect_assigned(body, out: set[str]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            out.add(stmt.name)
            collect_assigned(stmt.body, out)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(stmt, (ast.If, ast.Try)):
            for part in ("body", "orelse", "finalbody"):
                collect_assigned(getattr(stmt, part, []) or [], out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
