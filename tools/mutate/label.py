"""Label patches with the tests they really break, by running sqlglot's suite.

For each patch: apply it to sqlglot at the pinned commit, mount the changed files
into the map-builder image (the scorer's Python and packages), run the whole suite
in parallel, and record the failing tests. Output has the same shape as the kit's
sample/answers.json, so score.py can score the selector against it.

    py tools/mutate/label.py <patch dir> <out answers.json> [--jobs 14]

Checked against the kit: labeling sample/commits must reproduce sample/answers.json.
Runs on our machines only. Never part of the submitted image.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import selector  # noqa: E402  (reuse the patch parser and applier)

SRC = ROOT / "work" / "src" / "sqlglot"
WORK = ROOT / "work" / "label"
TESTS = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
NUMBER = {t["id"]: t["number"] for t in TESTS}
DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
IMAGE = "br-mapper"


def mutated_files(patch_text: str) -> dict[str, str]:
    """Return {repo path: new content} for every file the patch changes ("" = none)."""
    out = {}
    if not patch_text:
        return out
    for fp in selector.parse_patch(patch_text):
        if fp.new_path is None:
            raise RuntimeError(f"deleting {fp.old_path} is not supported by the labeler")
        old = (SRC / fp.old_path).read_text(encoding="utf-8") if fp.old_path else ""
        new_lines = selector.apply_hunks(old.split("\n") if fp.old_path else [], fp.hunks)
        out[fp.new_path] = "\n".join(new_lines)
    return out


def label_one(cid: str, patch_text: str, jobs: int) -> dict:
    case = WORK / cid
    shutil.rmtree(case, ignore_errors=True)
    (case / "out").mkdir(parents=True)
    mounts = []
    for rel, content in mutated_files(patch_text).items():
        dest = case / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8", newline="\n")
        mounts += ["-v", f"{dest}:/sqlglot/{rel}:ro"]
    name = f"label-{cid}-{os.getpid()}"
    cmd = [
        DOCKER, "run", "--rm", "--name", name, "--platform", "linux/amd64",
        "-v", f"{case / 'out'}:/out",
        "-v", f"{ROOT / 'tools' / 'mutate'}:/m:ro",
        "-e", "PYTHONPATH=/sqlglot:/m", "-e", "FAIL_OUT=/out",
        *mounts,
        IMAGE, "python", "-m", "pytest", "tests", "-p", "failrec", "-p", "no:cacheprovider",
        # Generous on purpose: we don't know the organizers' timeout, and labels must
        # match theirs even when a mutant makes a test slow rather than stuck.
        "-n", str(jobs), "-q", "--timeout", "600",
    ]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    try:
        # A mutant can loop forever inside the suite's own process-pool workers, where
        # pytest's per-test timeout cannot reach. Bound the whole run instead; a run
        # that hits this limit is UNLABELABLE, never given a guessed label.
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=RUN_LIMIT_S)
    except subprocess.TimeoutExpired:
        subprocess.run([DOCKER, "kill", name], capture_output=True)
        raise Unlabelable(f"{cid}: suite still running after {RUN_LIMIT_S // 60} min (likely an infinite loop)")
    (case / "pytest.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")

    failed, broken = set(), set()
    for f in (case / "out").glob("failed.*.json"):
        d = json.loads(f.read_text())
        failed |= set(d["failed"])
        broken |= set(d["broken"])
    numbers = {NUMBER[n] for n in failed if n in NUMBER}
    unknown = sorted(n for n in failed if n not in NUMBER)
    for b in broken:  # a file that cannot be imported fails all of its tests
        path = b.split("::", 1)[0]
        numbers |= {t["number"] for t in TESTS if t["file"] == path or t["id"].startswith(b + "::")}
    if not failed and not broken and proc.returncode not in (0, 1):
        raise RuntimeError(f"{cid}: pytest exit {proc.returncode}, see {case / 'pytest.log'}")
    return {"failing": sorted(numbers), "unknown_failed": unknown}


SUSPECT = ("BrokenProcessPool", "MemoryError", "Killed", "node down", "worker crashed")
RUN_LIMIT_S = 40 * 60


class Unlabelable(RuntimeError):
    pass


_BASELINE: dict | None = None


def baseline(jobs: int) -> dict:
    """The unpatched suite under the same conditions: must be 0 failing, no crashes."""
    global _BASELINE
    if _BASELINE is None:
        got = label_one("_baseline", "", jobs)
        log = (WORK / "_baseline" / "pytest.log").read_text(encoding="utf-8", errors="replace")
        _BASELINE = {"failing": got["failing"], "crashed": any(m in log for m in SUSPECT)}
    return _BASELINE


def label_checked(cid: str, patch_text: str, jobs: int) -> dict:
    """Label conservatively when a run shows a crashed process pool or worker.

    A crash can be the patch (a serde bug breaks pickling, so the pool dies every
    time) or the machine (out of memory while other jobs run), and repeating the
    crash does not tell those apart. So nothing is ever thrown away: the label is the
    UNION of every run's failures, marked suspect, with an unpatched baseline run
    recorded for comparison. A union can only make our miss check stricter; it can
    never hide a real failure. Misses on suspect labels are reviewed by hand."""
    first = label_one(cid, patch_text, jobs)
    log = (WORK / cid / "pytest.log").read_text(encoding="utf-8", errors="replace")
    if not any(m in log for m in SUSPECT):
        return first
    second = label_one(cid, patch_text, jobs)
    log2 = (WORK / cid / "pytest.log").read_text(encoding="utf-8", errors="replace")
    base = baseline(jobs)
    union = sorted(set(first["failing"]) | set(second["failing"]))
    note = {
        "suspect": True,
        "runs": [len(first["failing"]), len(second["failing"])],
        "second_crashed": any(m in log2 for m in SUSPECT),
        "baseline_failing": len(base["failing"]),
        "baseline_crashed": base["crashed"],
    }
    print(f"{cid}: SUSPECT {note}", file=sys.stderr, flush=True)
    return {"failing": union, "unknown_failed": first["unknown_failed"], "note": note}


def main() -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    patch_dir, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 14
    parallel = int(sys.argv[sys.argv.index("--parallel") + 1]) if "--parallel" in sys.argv else 1
    answers = json.loads(out_path.read_text())["commits"] if out_path.exists() else {}
    skipped_path = out_path.with_suffix(".unlabelable.json")
    skipped = json.loads(skipped_path.read_text()) if skipped_path.exists() else {}
    todo = [p for p in sorted(patch_dir.glob("*.patch")) if p.stem not in answers and p.stem not in skipped]
    lock = threading.Lock()

    def run(patch: Path) -> None:
        t0 = time.time()
        try:
            got = label_checked(patch.stem, patch.read_text(encoding="utf-8"), jobs)
        except Unlabelable as e:
            print(f"{patch.stem}: UNLABELABLE {e}", file=sys.stderr, flush=True)
            with lock:
                skipped = out_path.with_suffix(".unlabelable.json")
                seen = json.loads(skipped.read_text()) if skipped.exists() else {}
                seen[patch.stem] = str(e)
                skipped.write_text(json.dumps(seen, indent=1))
            return
        except Exception as e:
            print(f"{patch.stem}: ERROR {e}", file=sys.stderr, flush=True)
            return
        with lock:
            answers[patch.stem] = {"failing": got["failing"], "kind": "generated"}
            if "note" in got:
                answers[patch.stem]["note"] = got["note"]
            out_path.write_text(json.dumps({"commits": answers}, indent=1))
        extra = f" (unmapped failures: {got['unknown_failed'][:3]})" if got["unknown_failed"] else ""
        print(f"{patch.stem}: {len(got['failing'])} failing in {time.time() - t0:.0f}s{extra}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(run, todo))


if __name__ == "__main__":
    main()
