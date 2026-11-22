# Blast Radius: Team 2696

Our entry for the RWX **Blast Radius** track: given a patch to sqlglot (pinned at
`eb11aec`), print which tests to run and how to split them across up to 8 workers.
Lower simulated CI seconds wins. A missed failing test costs 300 s.

Cutoff: **Sun 11:00 AM ET.** Only the final counts.

Working notes, measurements and the state of every experiment are in `CHECKPOINT.md`.

## Run it locally

You need Docker running. No `make` needed:

```
py run_sample.py          # build, run on the 30 sample commits under the scorer's limits, score
py run_sample.py --check  # output format only
```

(`make sample` does the same on machines that have make.)

## How the selector works

The rule everywhere: when unsure, select more. Everything below either selects the tests a
change can reach, or proves that a selected test cannot change outcome.

1. **Coverage map (offline, `tools/build_map/`).** Runs sqlglot's whole suite at the pinned commit
   in the scorer's image, recording per test: lines run (coverage contexts), files opened, sqlglot
   classes used. Process pools run in-process with pickling, so work done in pool workers is
   attributed to the right test. Output: `maps/map.json.gz`.
2. **Select (`selector.py`).** For each patch: comment-only edits (same compiled code) select
   nothing; function-body edits select the tests that ran that statement; fixtures select the tests
   that opened them; anything that runs at import and that we cannot place selects every test.
3. **Class-level edits (`classstate.py`, `maps/attr.json.gz`; branch `exp/class-attrs`).** A statement
   in a class body (a setting, a table) acts only through the state it leaves after import. We
   import the patched code in a child process, list the class attributes whose value differs from
   the pinned commit's (the edited one plus anything derived from it at import), and select the
   tests that read one of them. Which test reads which attribute was recorded by running every test
   alone in a fresh process (`tools/build_map/attrrec.py`, `attr_isolated.py`, `build_attr_map.py`).
   If the check cannot run or finish, or the patch changes state the map cannot observe, every
   test is selected.
4. **Early cutoff (`earlycut.py`, `maps/tpcds_stage.json.gz`).** `test_execute_tpcds` (21.5 s) has a
   cheap front half (parse, optimize and plan the TPC-DS queries) and an expensive back half
   (execute them). For a patch that only touches front-half code we re-run the front half on the
   patched code and compare what it hands to the executor with the pinned commit's fingerprints.
   Identical means the test cannot change outcome, so it is not scheduled. Any difference, error or
   timeout keeps it. For a patch in code the back half runs, only the queries that reach it are
   re-executed and their results compared; a few queries whose plan changed are executed the same way.
5. **Three rules for the slow tests (branch `exp/giants`).** A diagnostic showed that three of the 40
   practice commits select `test_execute_tpcds`, for about 40 % of our simulated seconds.
   - *The two TPC query files.* `TestExecutor.setUpClass` loads both `tpc-h.sql` and `tpc-ds.sql`, so
     the read map credits all 60 tests of the class with both. After setup the two benchmarks only
     share a result cache keyed by the query text. For a patch to one of the files, the executor
     tests of the other benchmark are dropped if the patched file still loads the way setUpClass
     loads it and none of its queries has the text of a query of the other file.
   - *Early cutoff for class-attribute edits* (`maps/tpcds_back_attrs.json.gz`,
     `tools/build_map/record_back_attrs.py`). When class attributes are all a patch changes once
     sqlglot is imported, the front half is re-run on the patched code as in 4. That decides the test
     if the rest of it (building the fixture tables, the executor) never reads a changed attribute;
     which attributes the rest reads (52 of 6,361) is recorded at the pinned commit, warm and with one
     fresh process per query.
   - *`test_generator_memory` run here* (`owntest.py`). 6.7 s to schedule, about 1.3 s of CPU to run.
     For a patch that changes only Python files of the sqlglot package, the real, unmodified test
     method is run in a fresh process on the patched code; it is left out only if it passes there.
6. **Pack.** Longest test first onto the least-loaded worker, trying 1 to 8 workers. This matches the
   lower bound on every commit we have measured.

`prerun.py` and `probe.py` (run cheap candidate tests, or watch the changed expression, in a forked
child) are earlier experiments, off by default.

Checking for misses beyond the 30 samples: `tools/mutate/make_mutants.py` makes patches like the
samples, and `tools/mutate/label.py` labels them by running the real suite in the labeling image
(it reproduces `sample/answers.json`). Labels are never shipped.

## Layout

| Path | What it is |
| --- | --- |
| `Dockerfile` | What the scorer builds. Must stay at the top of the repo. |
| `selector.py` | The program the scorer runs. Reads `/data`, prints one JSON line per commit. |
| `classstate.py` | Which class attributes a patch changes once sqlglot is imported. |
| `earlycut.py` | Early cutoff for `test_execute_tpcds`. |
| `owntest.py` | Runs the real `test_generator_memory` on the patched code. |
| `prerun.py`, `probe.py` | Earlier experiments, off by default. |
| `maps/` | Maps made from the pinned commit: coverage, attribute readers, test stages. Under 20 MB. |
| `tools/` | Scripts we run on our own machines to build the maps and evaluate. Not used at score time. |
| `sample/` | The kit's 30 sample commits, `tests.json` and answers. |
| `score.py`, `Makefile`, `run_sample.py` | Local scoring (kit's scorer, plus our make-free runner). |
| `docs/` | The kit's README and the exact scoring rules (`SCORING.md`). |

## Rules we hold ourselves to

- No table from a patch, or a kind of edit, to the tests it breaks. Only maps made from the pinned
  commit (coverage, attribute reads, stages) and code run on the patched source at decision time.
  Nothing learned from `sample/answers.json` or from our own labels is shipped.
- No other team's code.
- The build may download only from PyPI/npm/Docker Hub, GitHub releases at a fixed tag, and
  sqlglot at `eb11aec`.
- Logs go to stderr. Anything extra on stdout rejects the run.
- Every submitted commit stays in the repository (never rewrite or force-push it), and the
  repository stays public until results are out.

## Submission log (practice board, 40 commits)

| Submission | Commit | Branch / tag | What it is | Simulated + CPU, misses | Score |
| --- | --- | --- | --- | --- | --- |
| BB-5E85C0A3 | `1a9a77b` | `main`, `sub-01` | Coverage-map selector | 215.778 + 1.06 s, 0 | 216.8 |
| BB-F7D24F70 | `1f0c94a` | `exp/probe`, `sub-02-probe` | + infection probe | 213.771 + 2.62 s, 0 | 216.4 |
| BB-491EBE76 | `38923fa` | `exp/early-cutoff` | + early cutoff | 215.778 + 0.98 s, 0 | 216.8 |
| BB-F4BEB122 (diagnostic) | `89112d3` | `diag/telemetry-1` | Same selections; reports how the selector classified the commits. Not a candidate. | 218.877 + 53.03 s, 0 | 271.9 |
| BB-EA6DA914 | `5b51ebc` | `exp/class-attrs` | + class-attribute selection | 202.814 + 5.65 s, 0 | 208.5 |
| BB-1E9B075A | `5083396` | `exp/module-level` | + module-level assignments, function headers, TPC-DS fixture | 194.714 + 20.16 s, 0 | 214.9 |
| BB-401A9017 | `430d29b` | `exp/cheaper-checks` | + cheaper import-state check, executor-side early cutoff | 194.714 + 17.11 s, 0 | 211.8 |
| BB-31607470 | `c763a65` | `exp/plan-diffs` | + fixes from two reviews, changed-plan queries re-executed, fixed gate costs | 194.714 + 12.90 s, 0 | 207.6 |
| BB-D8482848 | `2c0c8ab` | `exp/value-map` | + comparison map (experimental; not part of this branch) | 192.707 + 12.88 s, 0 | 205.6 |
| BB-5BB38D81 (diagnostic) | `3ec8bfc` | `diag/telemetry-2` | Selections of `c763a65` plus added tests; reports counts and costs of our own selections per kind of commit. Not a candidate. | 1044.281 + 33.90 s, 0 | 1078.2 |
| BB-9F093248 (diagnostic) | `c809629` | `diag/telemetry-2` | Same, second half of the report. Not a candidate. | 627.422 + 36.60 s, 0 | 664.0 |
