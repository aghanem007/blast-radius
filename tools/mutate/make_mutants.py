"""Generate synthetic patches shaped like the organizers' sample commits.

    py tools/mutate/make_mutants.py <out dir> --count 100 [--seed 1]

Kinds (sample mix: 16 code, 8 fixture, 3 comment, 3 multi):
  code     one token flipped in sqlglot/: and<->or, ==<->!=, is<->==, <<->=<, >-><=,
           True<->False, a small int +-1, `not` dropped
  fixture  a junk token inserted into one SQL line under tests/fixtures/
  comment  a comment line inserted into a sqlglot/ Python file
  multi    2 or 3 of the above, in different files

Every patch applies to sqlglot at the pinned commit and is written in git's
unified diff format with 3 lines of context, like the samples.
Evaluation only: we never ship anything derived from these.
"""

from __future__ import annotations

import ast
import difflib
import io
import json
import random
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "work" / "src" / "sqlglot"

COMMENTS = [
    "# Kept for backwards compatibility",
    "# Cheap check first",
    "# Mirrors the behavior of the reference implementation",
    "# Intentionally not cached",
    "# Keep in sync with the generator",
    "# TODO: revisit",
]
JUNK = ["42", "x", "0", "NULL", "1", "AS", "y42"]
OP_SWAPS = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}
NAME_SWAPS = {"and": "or", "or": "and", "True": "False", "False": "True"}


def py_files() -> list[Path]:
    files = [p for p in (SRC / "sqlglot").rglob("*.py") if p.stat().st_size > 200]
    return sorted(files)


def fixture_files() -> list[Path]:
    return sorted(p for p in (SRC / "tests" / "fixtures").rglob("*.sql") if p.stat().st_size > 0)


def as_patch(rel: str, old: str, new: str) -> str:
    body = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3
        )
    )
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}"


def tree_changed(old: str, new: str) -> bool:
    try:
        return ast.dump(ast.parse(old)) != ast.dump(ast.parse(new))
    except SyntaxError:
        return False  # we want valid Python, like the samples


FOCUS_ROWS: dict[str, set[int]] | None = None  # rel path -> lines a big test runs


def load_focus(min_seconds: float) -> dict[str, set[int]]:
    """Lines run by at least one test that takes >= min_seconds (from maps/)."""
    import gzip
    import json

    m = json.load(gzip.open(ROOT / "maps" / "map.json.gz", "rt"))
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    secs = {t["id"]: t["seconds"] for t in tests}
    big = {i for i, nodeid in enumerate(m["tests"]) if secs.get(nodeid, 0) >= min_seconds}
    big_sets = {k for k, s in enumerate(m["sets"]) if big.intersection(s)}
    return {
        f: {ln for ln, s in pairs if s in big_sets}
        for f, pairs in m["lines"].items()
        if f.startswith("sqlglot/")
    }


def mutate_code(rng: random.Random, path: Path) -> str | None:
    old = path.read_text(encoding="utf-8")
    focus = None
    if FOCUS_ROWS is not None:
        focus = FOCUS_ROWS.get(path.relative_to(SRC).as_posix())
        if not focus:
            return None
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(old).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    lines = old.split("\n")
    candidates = []
    for i, tok in enumerate(toks):
        s = tok.string
        if tok.type == tokenize.OP and s in OP_SWAPS:
            candidates.append((tok, OP_SWAPS[s]))
        elif tok.type == tokenize.NAME and s in NAME_SWAPS:
            candidates.append((tok, NAME_SWAPS[s]))
        elif tok.type == tokenize.NAME and s == "is":
            nxt = toks[i + 1].string if i + 1 < len(toks) else ""
            candidates.append((tok, "!=" if nxt == "not" else "=="))
        elif tok.type == tokenize.NAME and s == "not":
            candidates.append((tok, ""))
        elif tok.type == tokenize.NUMBER and s.isdigit() and len(s) <= 3:
            candidates.append((tok, str(int(s) + rng.choice([1, -1])) if s != "0" else "1"))
    rng.shuffle(candidates)
    for tok, repl in candidates[:40]:
        (row, col), (erow, ecol) = tok.start, tok.end
        if row != erow or (focus is not None and row not in focus):
            continue
        line = lines[row - 1]
        if tok.string == "is" and line[ecol:].startswith(" not"):
            new_line = line[:col] + repl + line[ecol + 4 :]
        elif repl == "":
            new_line = line[:col] + line[ecol:].lstrip(" ")
        else:
            new_line = line[:col] + repl + line[ecol:]
        new = "\n".join(lines[: row - 1] + [new_line] + lines[row:])
        if tree_changed(old, new):
            return new
    return None


def mutate_fixture(rng: random.Random, path: Path) -> str | None:
    old = path.read_text(encoding="utf-8")
    lines = old.split("\n")
    sql_rows = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("#", "--"))]
    if not sql_rows:
        return None
    i = rng.choice(sql_rows)
    words = lines[i].split(" ")
    pos = rng.randrange(0, len(words) + 1)
    words.insert(pos, rng.choice(JUNK))
    lines[i] = " ".join(words)
    return "\n".join(lines)


def mutate_comment(rng: random.Random, path: Path) -> str | None:
    old = path.read_text(encoding="utf-8")
    lines = old.split("\n")
    rows = [i for i, l in enumerate(lines) if l.strip()]
    i = rng.choice(rows)
    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    new = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


# ----- exotic kinds: changes the samples never show, for the final's surprises


def test_py_files() -> list[Path]:
    return sorted(p for p in (SRC / "tests").rglob("*.py") if p.stat().st_size > 200)


def mutate_docstring(rng: random.Random, path: Path) -> str | None:
    """Change one word inside a docstring (sqlglot runs doctests in test_docs)."""
    old = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(old)
    except SyntaxError:
        return None
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.append(body[0])
    if not docs:
        return None
    lines = old.split("\n")
    d = rng.choice(docs)
    rows = [r for r in range(d.lineno - 1, (d.end_lineno or d.lineno)) if re.search(r"\b[A-Za-z]{3,}\b", lines[r])]
    if not rows:
        return None
    r = rng.choice(rows)
    words = list(re.finditer(r"\b[A-Za-z]{3,}\b", lines[r]))
    w = rng.choice(words)
    repl = rng.choice(["SELECT", "foo", "x", w.group(0).upper(), w.group(0)[::-1]])
    lines[r] = lines[r][: w.start()] + repl + lines[r][w.end():]
    new = "\n".join(lines)
    return new if new != old and tree_changed(old, new) else None


def mutate_delete(rng: random.Random, path: Path) -> str | None:
    """Delete one simple statement inside a function, replacing it with `pass`."""
    old = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(old)
    except SyntaxError:
        return None
    cands = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(fn):
                if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign)) and st.lineno == st.end_lineno \
                        and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
                    cands.append(st)
    if not cands:
        return None
    st = rng.choice(cands)
    lines = old.split("\n")
    line = lines[st.lineno - 1]
    lines[st.lineno - 1] = line[: len(line) - len(line.lstrip())] + "pass"
    new = "\n".join(lines)
    return new if tree_changed(old, new) else None


def mutate_addfunc(rng: random.Random, path: Path) -> str | None:
    """Append a new, unused module-level helper."""
    old = path.read_text(encoding="utf-8")
    name = f"_helper_{rng.randrange(10**6)}"
    new = old.rstrip("\n") + f"\n\n\ndef {name}(value):\n    return value\n"
    return new if tree_changed(old, new) else None


def mutate_fixmeta(rng: random.Random, path: Path) -> str | None:
    """Change a fixture's meta line (# dialect: / # execute:) or add a SQL comment."""
    old = path.read_text(encoding="utf-8")
    lines = old.split("\n")
    meta = [i for i, l in enumerate(lines) if l.startswith("# dialect:") or l.startswith("# execute:")]
    if meta and rng.random() < 0.6:
        i = rng.choice(meta)
        if lines[i].startswith("# dialect:"):
            lines[i] = "# dialect: " + rng.choice(["duckdb", "bigquery", "snowflake", "postgres", "spark"])
        else:
            lines[i] = "# execute: " + ("false" if "true" in lines[i] else "true")
    else:
        sql_rows = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
        if not sql_rows:
            return None
        i = rng.choice(sql_rows)
        lines.insert(i, "-- note: keep this case")
    new = "\n".join(lines)
    return new if new != old else None


def mutate_doc_file(rng: random.Random, path: Path) -> str | None:
    old = path.read_text(encoding="utf-8")
    return old.rstrip("\n") + "\n\nSee the changelog for details.\n"


def doc_files() -> list[Path]:
    return [p for p in (SRC / "README.md", SRC / "CHANGELOG.md", SRC / "CONTRIBUTING.md") if p.exists()]


EXOTIC = {
    "testcode": (test_py_files, lambda rng, p: mutate_code(rng, p)),
    "docstring": (py_files, mutate_docstring),
    "delete": (py_files, mutate_delete),
    "addfunc": (py_files, mutate_addfunc),
    "fixmeta": (fixture_files, mutate_fixmeta),
    "docs": (doc_files, mutate_doc_file),
}


def one(rng: random.Random, kind: str, used: set[Path]) -> str | None:
    if kind in EXOTIC:
        files, fn = EXOTIC[kind]
        pool = files()
        for _ in range(50):
            path = rng.choice(pool)
            if path in used:
                continue
            new = fn(rng, path)
            if new is not None:
                used.add(path)
                rel = path.relative_to(SRC).as_posix()
                return as_patch(rel, path.read_text(encoding="utf-8"), new)
        return None
    pool = fixture_files() if kind == "fixture" else py_files()
    for _ in range(50):
        path = rng.choice(pool)
        if path in used:
            continue
        new = {"code": mutate_code, "fixture": mutate_fixture, "comment": mutate_comment}[kind](rng, path)
        if new is not None:
            used.add(path)
            rel = path.relative_to(SRC).as_posix()
            return as_patch(rel, path.read_text(encoding="utf-8"), new)
    return None


def main() -> None:
    global FOCUS_ROWS
    out = Path(sys.argv[1])
    count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 100
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    kinds = ["code"] * 16 + ["fixture"] * 8 + ["comment"] * 3 + ["multi"] * 3
    if "--focus-lines" in sys.argv:
        # Only code mutants, only on the given lines ({rel path: [lines]}): aimed tests,
        # e.g. code test_execute_tpcds runs but its cheap proxy never does.
        raw = json.loads(Path(sys.argv[sys.argv.index("--focus-lines") + 1]).read_text())
        FOCUS_ROWS = {f: set(v) for f, v in raw.items()}
        kinds = ["code"]
    if "--exotic" in sys.argv:
        kinds = list(EXOTIC) * 2 + ["testcode", "docstring", "delete"]
    if "--focus-big" in sys.argv:
        # Only code mutants, on lines a slow test (>= 2 s) runs: the data we need to
        # decide when skipping a slow test is safe.
        FOCUS_ROWS = load_focus(2.0)
        kinds = ["code"]
    made = 0
    while made < count:
        kind = rng.choice(kinds)
        used: set[Path] = set()
        if kind == "multi":
            parts = [one(rng, k, used) for k in rng.sample(["code", "fixture", "comment", "comment"], rng.choice([2, 3]))]
            if any(p is None for p in parts):
                continue
            patch = "".join(parts)
        else:
            patch = one(rng, kind, used)
            if patch is None:
                continue
        name = f"m{seed:02d}{made:03d}{kind[0]}"
        (out / f"{name}.patch").write_text(patch, encoding="utf-8", newline="\n")
        made += 1
    print(f"wrote {made} patches to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
