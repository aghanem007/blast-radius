"""Check infection probes against labels from the real suite.

    py tools/audit_probe.py <data dir with commits/, tests.json, answers.json> [...]

Runs the submission image with BR_PROBE_AUDIT=1. For each patch the probe can
instrument, prints how often the changed expression ran ("reached"), how often its
new version gave a different value ("infected"), and how many tests really fail.

The rule we would use: "reached >= R and never infected => the patch breaks
nothing". A DANGER line is a patch where that rule would have said "harmless" but
tests fail: each one is a 300 s miss. The rule is only enabled at an R with zero.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def audit(data: Path) -> list[tuple]:
    answers = json.loads((data / "answers.json").read_text())["commits"]
    cmd = [
        DOCKER, "run", "--rm", "--cpus", "2", "--memory", "4096m", "--read-only",
        "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "BR_PROBE_AUDIT=1",
        "-v", f"{(data / 'commits').resolve()}:/data/commits:ro",
        "-v", f"{(data / 'tests.json').resolve()}:/data/tests.json:ro",
        "blast-radius-entry",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    rows, commit = [], None
    for line in proc.stderr.splitlines():
        if re.match(r"^\S+:$", line):
            commit = line[:-1]
        m = re.search(r"PROBE (.*) tests=(\d+)", line)
        if m and commit in answers:
            res = json.loads(m.group(1))
            fails = len(answers[commit]["failing"])
            if res is None:
                rows.append((commit, None, None, int(m.group(2)), fails))
            else:
                rows.append((commit, res["reached"], res["infected"], int(m.group(2)), fails, len(res["failed"])))
    return rows


def main() -> None:
    rows = []
    for d in sys.argv[1:]:
        rows += audit(Path(d))
    print(f"{'patch':9s} {'reached':>8s} {'infected':>9s} {'tests':>6s} {'really_fail':>12s}  note")
    for r in rows:
        if r[1] is None:
            print(f"{r[0]:9s} {'-':>8s} {'-':>9s} {r[3]:6d} {r[4]:12d}  probe failed")
            continue
        commit, reached, infected, ntests, fails, probe_fails = r
        note = ""
        if probe_fails:
            note = f"probe run had {probe_fails} failing tests (environment off)"
        elif infected == 0 and fails:
            note = "DANGER: never infected but tests fail"
        print(f"{commit:9s} {reached:8d} {infected:9d} {ntests:6d} {fails:12d}  {note}")
    for R in (1, 10, 50, 200):
        said_safe = [r for r in rows if r[1] is not None and r[1] >= R and r[2] == 0 and not r[5]]
        wrong = [r[0] for r in said_safe if r[4]]
        print(f"rule reached>={R}: says harmless for {len(said_safe)} patches, wrong on {len(wrong)} {wrong}")


if __name__ == "__main__":
    main()
