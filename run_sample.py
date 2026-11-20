"""Windows-friendly stand-in for `make sample`: build the image, run it on the
30 sample commits under the scorer's limits, and score the output.

Usage:  py run_sample.py          (build + run + score)
        py run_sample.py --check  (format check only)
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample"
OUTPUT = HERE / "output.jsonl"
IMAGE = "blast-radius-entry"

# Same flags as the Makefile's RUN_FLAGS, i.e. the scorer's sandbox.
RUN_FLAGS = [
    "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
    "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
    "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--user", "65534:65534",
    "--env", "HOME=/tmp",
    "--volume", f"{SAMPLE / 'commits'}:/data/commits:ro",
    "--volume", f"{SAMPLE / 'tests.json'}:/data/tests.json:ro",
]


def main() -> int:
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "--tag", IMAGE, str(HERE)],
        check=True,
    )
    with OUTPUT.open("wb") as out:
        subprocess.run(["docker", "run", *RUN_FLAGS, IMAGE], stdout=out, check=True)
    score = subprocess.run(
        [sys.executable, str(HERE / "score.py"), str(SAMPLE),
         str(SAMPLE / "answers.json"), str(OUTPUT)],
        capture_output="--check" in sys.argv, text=True,
    )
    if "--check" in sys.argv:
        print("format ok" if score.returncode == 0 else score.stdout + score.stderr)
    return score.returncode


if __name__ == "__main__":
    sys.exit(main())
