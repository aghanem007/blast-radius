"""Blast Radius selector, team 2696.

v1: still runs every test on every commit (zero misses), but packs the tests
onto whichever worker count from 1 to 8 is cheapest under the scorer's cost
rule, instead of a fixed 3. The real selection logic comes next.

Cost of one commit (docs/SCORING.md), in whole milliseconds:
    busiest worker's load + 2000 ms per worker (+300 s if a failing test is left out)
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_COST_MS = 2000


def pack(tests: list[dict], worker_count: int) -> tuple[list[list[int]], int]:
    """Longest test first, each onto the least-loaded worker.

    Returns the workers and the busiest worker's load in ms.
    """
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test in sorted(tests, key=lambda t: t["ms"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += test["ms"]
    return workers, max(loads)


def best_split(tests: list[dict]) -> list[list[int]]:
    """Try every worker count and keep the cheapest. No tests means no workers (cost 0)."""
    if not tests:
        return []
    best_workers, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers, busiest = pack(tests, k)
        cost = busiest + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_workers, best_cost = workers, cost
    return best_workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    for test in tests:
        test["ms"] = round(test["seconds"] * 1000)

    workers = best_split(tests)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"selected all {len(tests)} tests on {len(workers)} workers", file=sys.stderr)


if __name__ == "__main__":
    main()
