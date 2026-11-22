"""Run one real sqlglot test against the patched code, here, instead of scheduling it.

tests/test_generator.py::TestGenerator::test_generator_memory has 6.7 s of recorded
time: scheduling it costs those seconds (and usually a worker). The test itself is
plain: for every dialect, build 1,100 generators and check that memory did not grow.
Running the real, unmodified test method in a fresh process of the same interpreter,
with the patch applied to sqlglot, costs about 1.3 s of CPU. If it passes there, it is
not scheduled. If it fails, errors, is skipped, times out, or anything at all is
unexpected, it stays scheduled.

Only for patches that change nothing but Python files inside the sqlglot package (the
test file, its helpers and everything else are then exactly the pinned commit's).

Two roles:
  python owntest.py run <src>     child: stdin {"overrides": {path: source}, "test": dotted name, "cpu_limit_s": n}
  import owntest                  used by selector.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import earlycut

GENERATOR_MEMORY_ID = "tests/test_generator.py::TestGenerator::test_generator_memory"
GENERATOR_MEMORY_NAME = "tests.test_generator.TestGenerator.test_generator_memory"


def cmd_run(src: str) -> int:
    """Child. stdout: one JSON line {"ok": bool, "passed": bool, ...}; ok=False means "cannot tell"."""
    real_stdout = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    verdict: dict = {"ok": False, "why": "crash"}
    try:
        job = json.loads(sys.stdin.read())
        if job.get("cpu_limit_s"):
            try:
                import resource

                limit = int(job["cpu_limit_s"])
                resource.setrlimit(resource.RLIMIT_CPU, (limit + 1, limit + 2))
            except Exception:  # noqa: BLE001
                pass
        earlycut.install_overrides(src, job["overrides"])
        if src not in sys.path:
            sys.path.insert(0, src)
        os.chdir(src)  # the suite runs from the repository root
        import unittest

        suite = unittest.defaultTestLoader.loadTestsFromName(job["test"])
        result = unittest.TestResult()
        suite.run(result)
        passed = (
            result.testsRun == 1 and result.wasSuccessful() and not result.failures and not result.errors
            and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses
        )
        first = (result.failures + result.errors)[:1]
        verdict = {
            "ok": True, "passed": bool(passed), "ran": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "first": first[0][1].strip().splitlines()[-1][:160] if first else "",
        }
    except BaseException as e:  # noqa: BLE001
        verdict = {"ok": False, "why": f"crash {type(e).__name__}: {e}"[:200]}
    verdict["cpu_s"] = round(time.process_time(), 3)
    real_stdout.write(json.dumps(verdict) + "\n")
    real_stdout.flush()
    return 0


def run(src: str, test: str, overrides: dict[str, str], wall_limit_s: float, cpu_limit_s: float, log) -> dict | None:
    """The child's verdict, or None when there is none (timeout, crash)."""
    import subprocess

    # No fixed hash seed: the suite is not run with one either.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONHASHSEED", None)
    try:
        p = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "run", src],
            input=json.dumps({"overrides": overrides, "test": test, "cpu_limit_s": cpu_limit_s}),
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=wall_limit_s,
        )
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001 - timeout, crash, no verdict
        log(f"  own run: no verdict ({type(e).__name__})")
        return None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        sys.exit(cmd_run(sys.argv[2]))
    sys.exit("usage: owntest.py run <src>")
