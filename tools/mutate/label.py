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
    """Return {repo path: new content} for every file the patch changes."""
    out = {}
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
    cmd = [
        DOCKER, "run", "--rm", "--platform", "linux/amd64",
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
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
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


def main() -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    patch_dir, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 14
    parallel = int(sys.argv[sys.argv.index("--parallel") + 1]) if "--parallel" in sys.argv else 1
    answers = json.loads(out_path.read_text())["commits"] if out_path.exists() else {}
    todo = [p for p in sorted(patch_dir.glob("*.patch")) if p.stem not in answers]
    lock = threading.Lock()

    def run(patch: Path) -> None:
        t0 = time.time()
        try:
            got = label_one(patch.stem, patch.read_text(encoding="utf-8"), jobs)
        except Exception as e:
            print(f"{patch.stem}: ERROR {e}", file=sys.stderr, flush=True)
            return
        with lock:
            answers[patch.stem] = {"failing": got["failing"], "kind": "generated"}
            out_path.write_text(json.dumps({"commits": answers}, indent=1))
        extra = f" (unmapped failures: {got['unknown_failed'][:3]})" if got["unknown_failed"] else ""
        print(f"{patch.stem}: {len(got['failing'])} failing in {time.time() - t0:.0f}s{extra}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(run, todo))


if __name__ == "__main__":
    main()
