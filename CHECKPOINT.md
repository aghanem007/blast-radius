# Checkpoint: Blast Radius (team 2696). Updated Fri 2026-11-20 ~21:40 ET

Read this first to resume. Cutoff: **Sun 2026-11-22 11:00 AM ET**. Only the final counts
(60 unseen commits, a 300 s penalty per missed commit, CPU seconds added, ties within 1 s go to
the earliest submission).

## Where things are

| What | Where |
| --- | --- |
| Local repo | `C:\Users\Welco\Desktop\Hackathon\blast-radius` |
| Remote (shared with Sergio) | https://github.com/Sergio33113/hackathon-1 (`origin`). **PRIVATE: must be made public to submit** |
| **Current working version (submit candidate)** | branch `selector/v2`, commit `e031ecbe0e4889a4af95727d4606a4c3fb3c438c`. Map only plus runner (prerun on by default in code; see decisions) |
| Review work in progress | branch `review/wip`, commit `4c9f9fc57735c645fd9c2de2ad0376f7cca02bca` (current checkout) |
| Not yet submitted | Nothing has been submitted on the track page |
| Strategy doc | https://claude.ai/artifact/S9GLzXzdo6H9Zxwvb26wyK |
| Critical review report | https://claude.ai/artifact/ChJcHJZrdmCMox2gxp7aSj |
| sqlglot @ eb11aec source | `work/src/sqlglot` (git-ignored) |
| Raw recordings | `work/out` (warm map raw), `work/out_cold` (cold), `work/out_union` (union) |

## How to run things

- Score locally in the scorer's sandbox: `py run_sample.py` (builds `blast-radius-entry`).
- Score any labeled set with real CPU: run the image with `-e BR_PRERUN=0|1 -e BR_PROBE=0|1` and
  mount `<set>/commits` and `<set>/tests.json`, then `py score.py <set> <set>/answers.json out.jsonl`.
- Fast local check (no fork, Windows): `BR_DATA=<set> BR_SRC=work/src/sqlglot BR_PRERUN=0 py selector.py`.
- Use a different map: `BR_MAP=maps_candidate/map.json.gz`.
- Map builder image `br-mapper`: `docker build -f tools/build_map/Dockerfile -t br-mapper work`
  (copy `requirements.lock`, `recorder.py` and `build_map.py` into `work/` first).
  Record: `pytest tests -p recorder -n 14 --dist loadfile` with `-v tools/build_map:/t -e PYTHONPATH=/sqlglot:/t`
  (`-e BR_COLD=1` for the cold mode), then `python /t/build_map.py /out /out/map.json.gz`.
- Label patches with the real suite: `py tools/mutate/label.py <patch dir> <out.json> --jobs 8 --parallel N`.
- Audits: `py tools/audit_prerun.py <set>`, `py tools/audit_probe.py <set>...`,
  cold check: `docker run ... br-mapper python /t/cold_check.py tests/test_optimizer.py tests/test_executor.py`
  with `-v maps:/maps`.
- Git Bash + docker volumes need `MSYS_NO_PATHCONV=1` and `cygpath -w` paths.

## Benchmarks (all with the scorer's sandbox flags)

| Set | Map only (sim + CPU) | + runner | + runner + probe | Misses |
| --- | --- | --- | --- | --- |
| 30 official samples | 174.5 + 1.7 | 169.6 + 4.1 | 144.8 + 35.8 | 0 |
| 31 slow-test fake bugs | 591.4 + 3.2 | 591.4 + 3.5 | 591.4 + 14.5 | 0 |
| 12 exotic fake bugs | 15.0 + 1.1 | 15.0 + 1.2 | 15.0 + 1.2 | 0 |
| **Total** | **786.9** | **784.9** | **802.7** | |

Starter on the samples: 1210.5 s; perfect: 94.4 s. Leaderboard (Fri ~20:00): Vaathi 186.2,
Tark305 188.5, Mustikas 197.2 (40 practice commits). Our estimate for 40 commits: ~235 s.

## Confirmed bugs

| ID | What | Status |
| --- | --- | --- |
| C1 | Constant-equivalence holes: `0`->`False`, `(1,2)`->`(1,2.0)`, `0.0`->`-0.0` treated as "no change" | **Fixed on review/wip**, not on selector/v2 |
| C2 | Labels corrupted by crashed pool workers under load (`m07021c` false label) | Labeler fixed (union + suspect flag + baseline) |
| C3 | **Map under-credits once-per-process cached code** (`Dialect._try_load`, `_build_dispatch`/`_DISPATCH_CACHE`, lazily imported dialect code). Adversarial patch `work/adv/adv2_tryload.patch`: 34 tests fail, we select 2 = **MISS** | Fix in progress: `BR_COLD=1` recording into `maps_candidate/` (union with the warm map). **Not yet validated** |

Earlier fixed bugs (already in selector/v2): spawn-pool tests unmapped (in-process pool + pickling),
`--- ` comment lines misread as headers, replacement edits counted as insertions, `__firstlineno__`
class constant, the runner not swapping lazily imported modules (serde), and an empty failure list read
as "all fail".

## Decisions made

- **Runner (prerun) and probe should be OFF for the final.** The runner is accurate (617 audited verdicts,
  0 errors) but gains ~2 s on 73 patches. The probe is net negative, with too few "harmless" verdicts to
  trust. Note: `selector.py` defaults `BR_PRERUN=1`; flip the default to off (and consider dropping the
  pip install from the Dockerfile) before submitting.
- The giant-test skip (BR_ELIDE) is rejected: the 22nd-cheapest test was the first to catch one bug.
- Attribute-level narrowing of class edits stays (harmless) but rarely applies (metaclasses read nearly
  every attribute).
- Never use other teams' code. Map data only from the pinned commit; labels are never shipped.
- Accuracy over speed: a miss costs 300 s, so anything uncertain selects more.

## Background jobs at checkpoint time

- Labeler, slow-test fake bugs: `work/mutants_big` -> `work/label_big.json` (~41/80; old process, old
  labeler code without the suspect logic). Note `m07021c` is a known bad label (true answer: 0 failing).
- Labeler, exotic: `work/mutants_exotic` -> `work/label_exotic.json` (~22/40).
- Labeler, adversarial: `work/adv` -> `work/label_adv.json` (adv2, adv3 done; adv1 pending).
- Cold candidate recording + union map: -> `work/out_cold`, `work/out_union`, `maps_candidate/map.json.gz`.

## Next steps (in order)

1. When the candidate map finishes: (a) confirm `pytest.log` shows 0 FAILED (the test_enum fix);
   (b) re-run `cold_check.py` against `maps_candidate` (gaps should drop to ~0 outside import lines);
   (c) score all labeled sets + `work/eval_adv` with `BR_MAP=maps_candidate/map.json.gz`: must be 0
   misses, including **adv2_tryload**; record the score cost.
2. If it holds: promote the candidate map to `maps/`, fold C1 in, set prerun/probe default off, and
   (optionally) drop the requirements install from the Dockerfile. Commit on `selector/v2`, verify with
   `run_sample.py`, tag `sub-01`.
3. Sergio makes the repo public; submit `selector/v2` HEAD on the track page; log it in the README table.
4. More adversarial patches: registry keys (P5), pytest config files (P2), shared test helpers,
   multi-hunk-in-one-function. Fix P2 (config files -> run everything), P3 (global time budget if any
   forking remains on), P4 (stdout on a private fd).
5. Optional score work, measured first: per-query slicing of `test_execute_tpcds` (record per-query
   contexts, measure slice sizes before building).
