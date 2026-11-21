# Blast Radius: Team 2696

Our entry for the RWX **Blast Radius** track: given a patch to sqlglot (pinned at
`eb11aec`), print which tests to run and how to split them across up to 8 workers.
Lower simulated CI seconds wins. A missed failing test costs 300 s.

Cutoff: **Sun 11:00 AM ET.** Only the final counts.

## Run it locally

You need Docker running. No `make` needed:

```
py run_sample.py          # build, run on the 30 sample commits under the scorer's limits, score
py run_sample.py --check  # output format only
```

(`make sample` does the same on machines that have make.)

## How the selector works

1. **Map (offline, `tools/build_map/`).** Runs sqlglot's whole suite at the pinned commit in the
   scorer's image, recording per test: lines run (coverage contexts), files opened, sqlglot
   classes used. Process pools run in-process with pickling, so work done in pool workers is
   attributed to the right test. Output: `maps/map.json.gz` (~0.8 MB).
2. **Select (`selector.py`).** For each patch: comment-only edits (same compiled code) select
   nothing; function-body edits select the tests that ran that statement; class-level edits select
   the tests that used the class or a subclass; fixtures select the tests that opened them;
   anything that runs at import, or that we can't place, selects every test.
3. **Try (`prerun.py`).** For function-body edits, fork a warm copy of the process, swap the new
   code into the live functions, and run the cheap candidate tests ourselves. Only the ones that
   fail get scheduled. Used only when the measured CPU (`maps/prerun_costs.json`, from
   `tools/calibrate_prerun.py`) is lower than the time it saves.
4. **Pack.** Longest test first onto the least-loaded worker, trying 1 to 8 workers.

Checking for misses beyond the 30 samples: `tools/mutate/make_mutants.py` makes fake-bug
patches like the samples, and `tools/mutate/label.py` labels them by running the real suite
(it reproduces `sample/answers.json` exactly).

## Layout

| Path | What it is |
| --- | --- |
| `Dockerfile` | What the scorer builds. Must stay at the top of the repo. |
| `selector.py` | The program the scorer runs. Reads `/data`, prints one JSON line per commit. |
| `prerun.py` | Runs cheap candidate tests against the patched code in a forked child. |
| `maps/` | Precomputed maps from the pinned commit (coverage, fixtures). Committed, under 20 MB total. |
| `tools/` | Scripts we run on our own machines to build the maps and evaluate. Not used at score time. |
| `sample/` | The kit's 30 sample commits, `tests.json` and answers. |
| `score.py`, `Makefile`, `run_sample.py` | Local scoring (kit's scorer, plus our make-free runner). |
| `docs/` | The kit's README and the exact scoring rules (`SCORING.md`). |

## How we work (two people)

- **`main` is always submittable.** Only merge into it after `py run_sample.py` shows **0 misses**.
- **Do the work on branches** (`map/...`, `selector/...`), and open a PR into `main`.
- **Every submission gets a tag** (`sub-01`, `sub-02`, ...) on the exact commit submitted, logged
  in the table below. The rules say every submitted commit has to stay in the repo, so never
  force-push or rewrite `main`.
- **The repo must be public** when we submit, and stay public until results are out.

## Rule reminders

- No table from a patch, or a kind of edit, to the tests it breaks. Only maps derived from the
  pinned commit (coverage, imports, fixtures). Never ship anything learned from `sample/answers.json`.
- The build may download only from PyPI/npm/Docker Hub, GitHub releases at a fixed tag, and
  sqlglot at `eb11aec`.
- Logs go to stderr. Anything extra on stdout rejects the run.
- They labeled the tests with `SKIP_INTEGRATION=0`, so the executor tests count.

## Submission log

| Tag | Commit | What changed | Sample score (misses) | Board score |
| --- | --- | --- | --- | --- |
| sub-01 | | Run everything, best worker count (4) | 1104.5 s (0) | |
