"""Check prerun.py's verdicts test by test against labels from the real suite.

    py tools/audit_prerun.py <data dir with commits/, tests.json, answers.json>

Runs the submission image with BR_AUDIT=1 (every cheap candidate is run by the
runner and its verdict logged), then compares with answers.json:
  false pass  the runner said a test passes, the real suite says it fails: a MISS
              waiting to happen. Must be zero before the runner is trusted.
  false fail  the runner said it fails, the real suite says it passes: costs time only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def main(data: Path) -> None:
    answers = json.loads((data / "answers.json").read_text())["commits"]
    cmd = [
        DOCKER, "run", "--rm", "--cpus", "2", "--memory", "4096m", "--read-only",
        "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "BR_AUDIT=1",
        "-v", f"{(data / 'commits').resolve()}:/data/commits:ro",
        "-v", f"{(data / 'tests.json').resolve()}:/data/tests.json:ro",
        "blast-radius-entry",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    commit, audited, false_pass, false_fail, ran = None, 0, [], 0, 0
    for line in proc.stderr.splitlines():
        if re.match(r"^\S+:$", line):
            commit = line[:-1]
        m = re.search(r"AUDIT ran=(\[.*?\]) failed=(\[.*?\])", line)
        if m and commit in answers:
            audited += 1
            ran_set, said_fail = set(json.loads(m.group(1))), set(json.loads(m.group(2)))
            truth = set(answers[commit]["failing"]) & ran_set
            ran += len(ran_set)
            if truth - said_fail:
                false_pass.append((commit, sorted(truth - said_fail)))
            false_fail += len(said_fail - truth)
    print(f"audited {audited} patches, {ran} test runs: {len(false_pass)} patches with FALSE PASSES, {false_fail} false fails")
    for c, tests in false_pass:
        print(f"  FALSE PASS {c}: {tests[:8]}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
