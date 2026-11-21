"""Measure the full-coverage infection probe against labels from the real suite.

    py tools/audit_probe.py <set dir> [<set dir> ...]

Runs the EXPERIMENT image (br-exp: the selector plus sqlglot's test packages) with
BR_PRERUN=1 BR_PROBE_AUDIT=1. For every patch whose one changed expression can be
instrumented, the probe runs over all cheap candidate tests (stopping at the first
infection) and logs its real CPU and the scheduling costs it could save.

Two policies are scored against the labels:
  A  never infected (complete, reached >= R)  -> schedule nothing
  B  never infected (complete, reached >= R)  -> drop only the slow tests (> 2 s)
A "MISS" is a patch the policy would clear while a test it dropped really fails.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
IMAGE = "br-exp"


def run(data: Path) -> list[dict]:
    answers = json.loads((data / "answers.json").read_text())["commits"]
    tests = json.loads((data / "tests.json").read_text())["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    cmd = [
        DOCKER, "run", "--rm", "--cpus", "2", "--memory", "4096m", "--read-only",
        "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534", "-e", "HOME=/tmp",
        "-e", "BR_PRERUN=1", "-e", "BR_PROBE_AUDIT=1",
        "-v", f"{(data / 'commits').resolve()}:/data/commits:ro",
        "-v", f"{(data / 'tests.json').resolve()}:/data/tests.json:ro",
        IMAGE,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    rows, commit = [], None
    for line in proc.stderr.splitlines():
        if re.match(r"^\S+:$", line):
            commit = line[:-1]
        m = re.search(r"  PROBE (\{.*\})$", line)
        if m and commit in answers:
            info = json.loads(m.group(1))
            failing = set(answers[commit]["failing"])
            info.update(
                set=data.name, patch=commit, fails=len(failing),
                slow_fails=sorted(n for n in failing if secs.get(n, 0) > 2),
                any_fail=bool(failing),
            )
            rows.append(info)
    return rows


def main() -> None:
    rows = []
    for d in sys.argv[1:]:
        rows += run(Path(d))
    json.dump(rows, open("work/probe_audit.json", "w"), indent=1)
    print(f"{'set':8s} {'patch':12s} {'reached':>8s} {'infect':>6s} {'done':>5s} {'cpu':>6s} {'sched':>6s} {'no-slow':>7s} {'fails':>5s} slow_fail")
    for r in rows:
        p = r["probe"] or {}
        print(f"{r['set'][:8]:8s} {r['patch']:12s} {p.get('reached', '-'):>8} {p.get('infected', '-'):>6} "
              f"{str(p.get('complete', '-'))[:5]:>5s} {r['cpu_s']:6.1f} {r['schedule_all_s']:6.1f} {r['schedule_without_slow_s']:7.1f} "
              f"{r['fails']:5d} {bool(r['slow_fails'])}")
    for R in (10, 100, 1000):
        for policy in ("A", "B"):
            saved = cost = 0.0
            misses = []
            for r in rows:
                p = r["probe"]
                cost += r["cpu_s"]
                clear = bool(p) and p.get("complete") and not p["failed"] and p["infected"] == 0 and p["reached"] >= R
                if not clear:
                    continue
                if policy == "A":
                    saved += r["schedule_all_s"]
                    if r["any_fail"]:
                        misses.append(r["patch"])
                else:
                    saved += r["schedule_all_s"] - r["schedule_without_slow_s"]
                    if r["slow_fails"]:
                        misses.append(r["patch"])
            print(f"policy {policy}, reached>={R:4d}: probe CPU {cost:6.1f}s, scheduling saved {saved:6.1f}s, "
                  f"net {saved - cost - 300 * len(misses):7.1f}s, MISSES {len(misses)} {misses}")


if __name__ == "__main__":
    main()
