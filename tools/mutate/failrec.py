"""pytest plugin: record which tests fail, the way the organizers define it.

A test fails when pytest reports a failure, an error or a timeout for it (in setup,
call or teardown), when any of its subtests fails, or when its file cannot be
imported. Writes /out/failed.<worker>.json; the labeler unions all of them.
"""

import json
import os

OUT = os.environ.get("FAIL_OUT", "/out")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_failed: set[str] = set()
_broken: set[str] = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _broken.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"failed.{WORKER}.json"), "w") as f:
        json.dump({"failed": sorted(_failed), "broken": sorted(_broken), "exit": int(exitstatus)}, f)
