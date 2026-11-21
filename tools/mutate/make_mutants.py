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
import random
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


def one(rng: random.Random, kind: str, used: set[Path]) -> str | None:
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
    out = Path(sys.argv[1])
    count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 100
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    kinds = ["code"] * 16 + ["fixture"] * 8 + ["comment"] * 3 + ["multi"] * 3
    if "--focus-big" in sys.argv:
        # Only code mutants, on lines a slow test (>= 2 s) runs: the data we need to
        # decide when skipping a slow test is safe.
        global FOCUS_ROWS
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
