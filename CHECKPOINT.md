# Checkpoint: Blast Radius (team 2696). Updated Fri 2026-11-20 ~21:40 ET

Read this first to resume. Cutoff: **Sun 2026-11-22 11:00 AM ET**. Only the final counts
(60 unseen commits, a 300 s penalty per missed commit, CPU seconds added, ties within 1 s go to
the earliest submission).

## Where things are

| What | Where |
| --- | --- |
| Local repo | `C:\Users\Welco\Desktop\Hackathon\blast-radius` |
| Remote (shared with Sergio) | https://github.com/Sergio33113/hackathon-1 (`origin`). **PRIVATE: must be made public to submit** |
| **Current working version (submit candidate)** | branch `selector/v2` HEAD (see `git log -1 selector/v2`); review fixes shipped Nov 20 ~22:40. Previous: `e031ecbe0e4889a4af95727d4606a4c3fb3c438c`. Map only plus runner (prerun on by default in code; see decisions) |
| Review work in progress | branch `review/wip`, commit `4c9f9fc57735c645fd9c2de2ad0376f7cca02bca` (current checkout) |
| Not yet submitted | Nothing has been submitted on the track page |
| Strategy doc | private team doc (link shared directly) |
| Critical review report | private team doc (link shared directly) |
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
| C3 | **Map under-credits once-per-process cached code** (`Dialect._try_load`, `_build_dispatch`/`_DISPATCH_CACHE`, lazily imported dialect code). Adversarial patch `work/adv/adv2_tryload.patch`: 34 tests fail, we select 2 = **MISS** | **Fix validated**: `maps_candidate/map.json.gz` (union of warm `work/out` + cold `work/out_cold`; recording 1620 passed). `_try_load` lines credited to 1232 tests (was 2); adv2 caught, 0 misses; scores on samples / big / exotic unchanged (174.5 / 591.4 / 15.0). Final cold check against the candidate: **0 runtime gaps** in 227 optimizer/executor tests (was 197 tests, 2,972 lines) |

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

## Labeler hang (fixed ~22:00)

Some mutants loop forever inside the suite's own process-pool workers, where pytest's
per-test timeout can't reach (`m07013c` ran 3+ hours on ~6 cores). `label.py` now bounds each
run at 40 min, kills the container, and records the patch in `<out>.unlabelable.json` (never a
guessed label). `m07013c` is recorded unlabelable. The slow-test labeler was restarted with this
code (it resumes from saved labels). `adv1_dispatch` (old labeler code) has a 30-min watchdog that
kills it if it's still running.

## Background jobs at checkpoint time

- Labeler, slow-test fake bugs: `work/mutants_big` -> `work/label_big.json` (~41/80; old process, old
  labeler code without the suspect logic). Note `m07021c` is a known bad label (true answer: 0 failing).
- Labeler, exotic: `work/mutants_exotic` -> `work/label_exotic.json` (~22/40).
- Labeler, adversarial: `work/adv` -> `work/label_adv.json` (adv2, adv3 done; adv1 pending).
- Cold candidate recording + union map: -> `work/out_cold`, `work/out_union`, `maps_candidate/map.json.gz`.

## Shipped on selector/v2 (Nov 20 ~22:40)

- C3 fix: cold-cache union map promoted to `maps/map.json.gz`.
- C1 fix: exact constant comparison.
- Runner (prerun) off by default; Dockerfile no longer installs test packages (build only fetches sqlglot).
- Insurance: pytest/packaging config -> all (P2), `sqlglotc/**` -> its 3 tests, answers on a private stdout fd (P4),
  registry attributes `key`/`_sql_names` -> all (P5).
- Verified in the scorer sandbox: 0 misses on 121 labeled patches; samples 174.5 s + ~1.4 s CPU; `run_sample.py` format ok.

- Nov 20 ~23:00: test-file top-level code that runs at import -> all (exotic `m11020t`:
  `__name__ != "__main__"` in tests/test_docs.py crashed the pytest session; we had selected 0).
  **Submit candidate: selector/v2 @ 439b5e4b47a01e24242d4dd8a1db3aa44bb215b0.** 0 misses on 125 labeled patches.

## TPC-DS slicing experiment (Nov 20 ~23:45): REJECTED

Per-query coverage + CPU (tools/tpcds_slice, branch exp/tpcds-slice; data in work/tpcds). Running all 99
TPC-DS queries ourselves costs 75.7 s CPU + 5.4 s setup; 30 of 37 labeled patches that select the test
reach all 57 executing queries (slice ~80 s vs ~23.5 s to schedule). Only m07038c would gain (~17 s).
Decision: do not implement; submit the verified version.

## Submission target (user's own repo, no Claude trailers)

Repo https://github.com/aghanem007/blast-radius (PRIVATE until the user approves going public).
Submit commit: selector/v2 = main = 1a9a77bc3e94ea44473cc38d5ded159a8671cde9 (verified: fresh clone,
clean build, samples 174.5 s, 0 misses). selector/v2 has not changed since.

## SUBMITTED (Nov 20, late night)

sub-01 = 1a9a77bc3e94ea44473cc38d5ded159a8671cde9 on https://github.com/aghanem007/blast-radius (now PUBLIC).
Practice board: **216.8 s, rank 41** (submission BB-5E85C0A3, Fri 10:32 PM). All 3 runs: 215.778 s simulated, ~1.06 s CPU, 0 commits missed. Marked "Entered in the final (your best score)". Tag `sub-01` pushed.
Keep the repo public and never rewrite this commit until results are published.

## test_execute_tpcds investigation (Nov 21, early)

Waste breakdown on 126 labeled patches: 849 s total vs perfect; 530 s is test_execute_tpcds scheduled needlessly.
- Proxy (test_tpcds as stand-in): REFUTED by adversarial m21001c (execute_tpcds fails, test_tpcds passes).
  836 lines run by execute_tpcds are never run by test_tpcds.
- Provably-safe edit classes: none worth it (needless cases are logic edits: is/==, copy=, defaults, conditions).
- Infection probe (full coverage, experiment image br-exp, tools/audit_probe.py, results work/probe_audit.json):
  no trigger -121 s; trigger est<=0.4-0.7x schedule +33..+41 s over 126 patches; 6/6 harmless verdicts correct.
  PENDING: 20 rare-input adversarial bugs (work/mutants_rare -> work/label_rare.json) must be audited before any decision.

## Practice experiment: infection probe (Nov 21, ~01:00)

- Branch `exp/probe` @ 1f0c94a64e8fdfea234391d02faf4069e8330fe1 (pushed, public). Probe on (trigger: estimate <= 0.5x
  scheduling cost, CPU budget 0.7x, no probe after 90 s wall), cheap-test runner off, image installs requirements.lock.
- Local, scorer sandbox, 142 labeled patches: probe 1599.4 s vs baseline 1608.5 s (0 misses both); samples 163.6 vs 176.9.
- WARNING: clean no-cache build took 542 s here under heavy load (limit 600 s). Before ANY final use: trim the image to
  only the packages the probe's tests need, and re-time the build.
- Practice submission pending (user submits the hash). Not yet validated against the rare-input adversarial bugs.

## Validation totals (Nov 21, ~01:30)

Baseline sub-01 (selector/v2 @ 1a9a77b): **0 misses on 162 labeled patches** (30 samples, 77 slow-test
[3 unlabelable infinite loops: m07013c, m07071c, m07075c; m07065c suspect = union label], 39 exotic
[m11020t unlabelable: crashes pytest session], 2 cache adversarial, 14 proxy adversarial) + 0 misses on
the 40 hidden practice commits. Proxy labeling stopped at 14/20 (already refuted: 5 counterexamples).
Rare-input adversarial labeling: work/mutants_rare -> work/label_rare.json (running).

## FINAL ENTRY RULE

The final defaults to the best practice score. The verified baseline is **sub-01 = 1a9a77bc3e94ea44473cc38d5ded159a8671cde9**
(submission BB-5E85C0A3, 216.8 s, 0 misses). If an experiment scores better but is judged too risky, explicitly pick
BB-5E85C0A3 as the final entry on the track page before Sun 11:00 AM ET.

## Next steps (in order)

1. DONE (see Shipped). Was: when the candidate map finishes: (a) confirm `pytest.log` shows 0 FAILED (the test_enum fix);
   (b) re-run `cold_check.py` against `maps_candidate` (gaps should drop to ~0 outside import lines);
   (c) score all labeled sets + `work/eval_adv` with `BR_MAP=maps_candidate/map.json.gz`: must be 0
   misses, including **adv2_tryload**; record the score cost.
2. DONE. Was: if it holds, promote the candidate map to `maps/`, fold C1 in, set prerun/probe default off, and
   (optionally) drop the requirements install from the Dockerfile. Commit on `selector/v2`, verify with
   `run_sample.py`, tag `sub-01`.
3. Sergio makes the repo public; submit `selector/v2` HEAD on the track page; log it in the README table.
4. More adversarial patches: registry keys (P5), pytest config files (P2), shared test helpers,
   multi-hunk-in-one-function. Fix P2 (config files -> run everything), P3 (global time budget if any
   forking remains on), P4 (stdout on a private fd).
5. Optional score work, measured first: per-query slicing of `test_execute_tpcds` (record per-query
   contexts, measure slice sizes before building).
