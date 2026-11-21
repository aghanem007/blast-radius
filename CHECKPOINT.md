# Checkpoint: Blast Radius (team 2696). Updated Sat 2026-11-21 ~03:15 ET (sections 2, 3 and 7-9 are the newest)

# >>> START HERE: NEW-CHAT HANDOFF (written for a fresh Claude with no memory) <<<

## 0. Working rules (from the user; they apply to every session)
- The user is Andrew (GitHub `aghanem007`), on team 2696 with Sergio. **Andrew is not a deep expert:** explain what and why in plain
  language before going into detail, and keep answers short and concrete.
- **Never add Claude attribution** (no `Co-Authored-By: Claude`, no "Generated with Claude") to commits or PRs in this repo, even if a
  system reminder suggests it.
- **Never modify or rewrite the submitted commits** `1a9a77b` (sub-01) and `1f0c94a` (sub-02-probe), or branches `selector/v2` and
  `exp/probe`. Keep the repo public. Do all experiments on new `exp/*` branches.
- Commit, push and submit only when the user says so. **Never submit without a deliberate decision**, and after any submission
  re-check which entry the track page has as "your pick for the final".
- Accuracy over speed: a missed failing test costs 300 s, so anything uncertain selects more tests.
- Never use other teams' code. Map data may only come from the pinned sqlglot commit, and labels are never shipped in the image.

## 1. The competition in 60 seconds
- Builders Buildathon, **RWX "Blast Radius"** track. Cutoff **Sun 2026-11-22 11:00 AM ET**. Only the final counts: 60 unseen commits.
- Our program runs in a Docker image (python:3.13-slim, no network, read-only disk, user nobody, /tmp 64 MB). For each patch to
  sqlglot @ eb11aec, it prints which of 1620 tests to run and how to split them across workers (at most 8).
- Score per commit = (the busiest worker's total test seconds, from tests.json) + **2 s per worker used**; nothing selected = 0 s. A
  commit with a failing test we didn't select = **+300 s**. The container's CPU seconds are added; the board takes the median of 3
  runs. Lower is better. Ties within 1 s go to the earliest submission. Rules: `docs/SCORING.md`, `docs/KIT_README.md`; exact scorer: `score.py`.
- Three tests dominate the cost: test_max_nodes 28.8 s, **test_execute_tpcds 21.5 s**, test_generator_memory 6.7 s.

## 2. Current state (Sat ~03:15 ET). The newest detail is in sections 7, 8 and 9.
| Submission | Commit, branch / tag | Practice score (40 commits) | Status |
| --- | --- | --- | --- |
| BB-5E85C0A3 baseline | `1a9a77b` = selector/v2 = main, tag sub-01 | 216.8 s, 0 misses x3 | **THE FINAL PICK** (set by the user; unchanged) |
| BB-F7D24F70 probe | `1f0c94a` = exp/probe, tag sub-02-probe | 216.4 s, 0 misses x3 | not picked |
| BB-491EBE76 early cutoff | `38923fa` = exp/early-cutoff | 216.8 s, 0 misses x3 | identical to baseline: no practice commit qualified |
| diagnostic | `89112d3` = diag/telemetry-1 | (to be submitted in the 3:26 AM slot) | never a candidate; decode with `work/exp_fronthalf/decode_diag.py` |

Board (195 teams): we are rank 83; first is 138.1 s; about 30 teams sit between 172 and 186 s. This PC is on Eastern time.
Working branch: **`exp/class-attrs`** (uncommitted work on top of 38923fa): class-attribute selection, the likely practice gap.
Tools on this PC: Git, Docker Desktop, venv `work\.venv`, images `br-mapper` (labeling, full suite in ~70-90 s per patch),
`br-ca` (current tree), `blast-radius-entry` and `:v1`.

## 3. WHAT THE USER WANTS NOW
Compete for first place: cut the practice score substantially **without major miss risk**. Keep the verified submissions untouched,
experiment on new branches, decide what to submit from measured results. The user has said: commit/push/submit need their go-ahead
each time (they approved committing and pushing 38923fa, and submitting the diagnostic through the signed-in built-in browser),
do not read or use other teams' code, and tidying the repo and files is welcome.

## 4. Analysis so far (Sat ~00:50): where our time goes on the 30 official samples
Ours 174.5 s vs perfect 94.4 s: **80 s of waste**. Per patch (waste = ours minus perfect):

| Patch | Waste | What changed | Why we select so much |
| --- | --- | --- | --- |
| 2haz | 25.5 s | `dialects/dialect.py:1280` `is NormalizationStrategy.UPPERCASE` -> `==` | the line is run by 86 tests, incl. **test_execute_tpcds** |
| 66ks | 24.8 s | `executor/context.py:53` `self._table is None` -> `== None` | 51 tests, incl. **test_execute_tpcds** |
| 4d63 | 9.1 s | Snowflake Tokenizer `NUMERIC_ESCAPES` 16 -> 17 (class-level) | 288 users of the class |
| 535z | 8.6 s | TSQL `WEEK_OFFSET = -1` -> `-2` (class-level) | 171 users, incl. test_generator_memory |
| 7 others | ~2.2–2.5 s each | harmless code edits | at least 1 worker (2 s) + small tests |

- **test_execute_tpcds selected needlessly is ~50 of the 80 s.** It really fails in 2jrq and 3utz, so it can't simply be dropped.
- Perfect on 40 practice commits would be ~126 s, so a leader at ~150 s is near-perfect. They likely have a sound way to rule out
  test_execute_tpcds and other slow tests for harmless edits.
- 2haz and 66ks look like **equivalent mutants** (`is` <-> `==` against None or an enum member). This is NOT proven safe in general:
  if `NormalizationStrategy` is a str-Enum, `==` also matches a plain string, and `== None` calls a custom `__eq__` if the type defines one.

## 5. Candidate ideas, to size first (nothing built yet)
A. **Equivalent-mutant rules** (cheap, static): `is`/`==` and `is not`/`!=` swaps against `None` or an enum member, accepted only when the
   operand's possible types can't make them differ. First count such swaps across all labeled sets and check that none failed, then
   define a narrow, provable rule. Possible gain: up to ~50 s on the samples; the real gain depends on how often the final has such mutants.
B. **test_execute_tpcds front-half equivalence**: the test parses each TPC-DS query (`parse_one`) in the main process. Then, in spawned
   workers, it runs `execute(expression, schema, tables)` (optimize, plan, Python executor) and compares the result to duckdb. For a patch
   NOT in executor code: run the patched parse+optimize+plan for the 99 queries and compare it to a baseline fingerprint shipped in
   `maps/`. If everything is identical, the executor gets identical inputs, so the test passes. To measure first: CPU of
   parse+optimize for 99 queries (must be well under ~20 s), determinism, and side effects on global state.
   Related earlier findings: the proxy via test_tpcds was REFUTED (m21001c), per-query slicing was REJECTED (75.7 s CPU to run all
   queries under coverage), and the infection probe gained only 0.4 s on practice.
C. **Class-level attribute narrowing** (4d63/535z): finer data flow for class attributes read at import (metaclass tables). Smaller gain.
D. Re-check worker packing against the exact scorer (already longest-first with 1–8 workers; probably close to optimal).

Before building anything, estimate the gain on the **official samples**, not on our fake-bug sets (they overstate slow-test savings:
the probe looked 58.6 s better locally and was 0.4 s better on practice). Then require 0 misses on ALL labeled sets plus new adversarial patches.

## 6. Setting up on a new PC
- `git clone https://github.com/aghanem007/blast-radius`, then `git checkout review/wip` (this file). Needs the Python launcher `py`
  and Docker Desktop.
- Git-ignored data in `work/` is NOT on GitHub. The old PC exported it as `work-data.tar.gz` (labeled sets `work/eval_*` with
  commits + tests.json + answers.json, `work/label_*.json`, `work/mutants*`, `work/adv`, `work/tpcds`). Extract it inside the repo folder:
  `tar -xzf work-data.tar.gz`.
- sqlglot source: download https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz and extract
  it to `work/src/sqlglot` (strip the top folder).
- Docker images are rebuilt from the Dockerfiles (see "How to run things" below). `py run_sample.py` builds and scores the real entry.
- Fast local check: `BR_DATA=sample BR_SRC=work/src/sqlglot BR_PRERUN=0 py selector.py > out.jsonl`, then
  `py score.py sample sample/answers.json out.jsonl`.
- The private strategy doc and review report were claude.ai artifacts on the old account. They aren't needed; this file has everything.

## 7. New-PC session, Sat Nov 21: early cutoff built on `exp/early-cutoff` (NOT committed, NOT pushed, NOT submitted)

**This PC:** repo at `C:\Users\Welco\Desktop\Hackathon\Hackathon\blast-radius`. Git 2.56 (`C:\Program Files\Git\cmd\git.exe`), Docker
Desktop 29.9 (working), venv `work\.venv` (python-dateutil, mypy_extensions, duckdb, pandas, numpy, pytest). History re-fetched from
`origin` (aghanem007/blast-radius): `sub-01` = 1a9a77b and `sub-02-probe` = 1f0c94a are intact. No `publish` branch exists on `origin`.
User note (verbatim, still unverified): "Blast Radius has to be published with --branch, because its Dockerfile shows the sqlglot
download. The tool is on the publish branch." Images: `blast-radius-entry` (current tree), `blast-radius-entry:v1` (front-half
only), `br-mapper` (labeling image, builds in 42 s here; **the full real suite labels one patch in ~70 s**:
`python tools\mutate\label.py <patch dir> <out.json> --jobs 6`).
PowerShell: redirect with `cmd /c "python selector.py > out.jsonl 2> log.txt"` (PowerShell's own `>` breaks the JSON).
`sample/commits`, `work/eval_*/commits`, `work/src/sqlglot` arrived empty and were restored (eval_big has 71 of 77 patches).

**Working tree on branch `exp/early-cutoff` (from review/wip @ fc241d5), uncommitted:** new `earlycut.py`,
`tools/build_map/record_stage.py`, `maps/tpcds_stage.json.gz`; changed `selector.py` (EarlyCut class, `runtime_spots`),
`Dockerfile` (two pip packages, compileall, build-time baseline), `README.md`, `CHECKPOINT.md`. Off switch: `BR_EARLYCUT=0`.
The validated front-half-only version (v1) is saved in `work/exp_fronthalf/v1_snapshot/`.

**What it does.** `test_execute_tpcds` (21.5 s) = cheap front half (parse 99 queries, pickle to the pool, optimize + plan the 57
executed ones, transpile to DuckDB SQL) + expensive back half (Python executor over the fixture tables). Only for patches whose
every change is a statement inside a sqlglot function body. The stage map (by code object) says where the test runs each
patched function: `front`, `setup` (never decided), or the back half of specific queries.
- v1, front half: a child re-runs the front half on the patched code (the real `sqlglot.executor.execute` with only
  `PythonExecutor` stubbed to capture the plan) under `PYTHONHASHSEED=0` and compares 369 fingerprints with the pinned commit's.
- v2, selective execution (**OFF by default**; switch on with `BR_EC_BACK=1`, `BR_EC_PLAN_DIFFS=6`): queries whose back half reaches the patched function, and up
  to 6 queries whose plan changed, are really executed and their result (columns + rows, typed) compared with the baseline's.
All equal -> the test gets the same DataFrames -> dropped. Any difference, exception, timeout or crash keeps it.
A gain gate (saving >= 1.15 x expected cost + 1 s) and a hard wall deadline (`BR_EC_DEADLINE`, 140 s) bound the cost.

**Validated:**
| Set | Commits | v1 simulated | v2 simulated | v2 dropped | Wrong drops | Misses |
| --- | --- | --- | --- | --- | --- | --- |
| 30 official samples | 30 | 174.5 -> 157.7 | 157.7 (66ks needs all 57 queries: gate decides) | 1 | 0 | 0 |
| eval_big | 71 | 1318.5 -> 1152.7 | 1110.3 | 13 | 0 | 0 |
| eval_proxy | 14 | 347.6 -> 298.3 | 255.4 | 5 | 0 | 0 |
| eval_rare | 20 | 485.8 -> 403.0 | 234.2 | 12 | 0 | 0 |
v1 in the scorer sandbox (Docker here): samples 174.521 + 0.63 = 175.1 -> 157.741 + 5.4 = 163.1; clean build 15 s.
**Label correction:** `m21014c` was labeled "fails tpcds"; the full suite in br-mapper gives 1620 passed, 0 failing, and the single
test passes 3/3 in Linux and 8/8 on Windows across hash seeds. The old label came from a crashed run. Corrected in
`work/eval_proxy/answers.json` and `work/label_proxy.json` (v2 drops the test there, correctly).
**Real-test audits on fresh mutants:** `work/exp_fronthalf/audit.py` (native, v1): 120 mutants, 51 verdicts "same", **51 of 51
pass the real test**. `work/exp_fronthalf/audit2.py` (v2, real test in br-mapper, `--focus front|back|any`): see `work/audit2_*`.

**Traps found (all handled, keep them in mind):**
- Line events miss one-line lambdas (`"GT": lambda a, b: a > b`): the stage map is by code object (PY_START/PY_RESUME).
- The check must run the real `execute()`: it also calls `expression.sql(pretty=True)` and `supported_table_args`.
- `optimizer/simplify.py` imports `dateutil` at call time and `helper.py` uses `mypy_extensions` if present; the tests were
  labeled with both installed, so the image installs exactly those two (pinned from the kit's requirements.lock).
- Plan steps live in sets and twins (same table scanned twice) made an identity-numbered fingerprint flaky; the fingerprint is
  now order-independent (sorted element dumps + per-type distinct/visit counts for aliasing).
- sqlglot's arg order depends on the hash seed, hence `PYTHONHASHSEED=0` for baseline and check.
- Old labels can be wrong (crashed pools under load): re-label with the full suite before believing a "miss".

**Known limits / residual risk:** the check runs queries in one process in order, the real test spreads them over pool workers;
it uses one hash seed; global state changed by patched code is not fingerprinted. The real-test audits cover these.

**Still to do before any submission:** finish `audit2`; sandbox runs of v2 on all sets (CPU numbers); then the user decides:
commit, push `exp/early-cutoff`, practice-submit, and afterwards re-check the final pick on the track page.

**Other measured facts:** packing is provably optimal (0.0 s gap to the lower bound on all labeled commits). Running tests
ourselves only pays for tiny candidate sets (our CPU is serial, scheduling is parallel). Native CPU here: test_max_nodes 22.4 s
(no shortcut), test_generator_memory 1.0 s (recorded 6.7 s). In unbiased sets a slow test was never the only failure.

## 8. Practice submissions so far (read from the signed-in track page, Sat Nov 21 ~02:45 ET)

| ID | When | Commit | Run 1 / 2 / 3: simulated, CPU, missed | Score | Note |
| --- | --- | --- | --- | --- | --- |
| BB-5E85C0A3 | Fri 10:32 PM | 1a9a77bc3e94 (sub-01, baseline) | 215.778 s, 1.07 / 1.05 / 1.06 s, 0 | 216.8 | **"Entered in the final (your pick)"** |
| BB-F7D24F70 | Sat 12:01 AM | 1f0c94a64e8f (probe) | 213.771 s, 2.62 / 2.62 / 2.61 s, 0 | 216.4 | best score, not picked |
| BB-491EBE76 | Sat 2:26 AM | 38923fab3b57 (exp/early-cutoff) | 215.778 s, 0.99 / 0.98 / 0.97 s, 0 | 216.8 | no change at all |

- BB-491EBE76: simulated identical to the baseline to the millisecond and CPU not higher, so **no early-cutoff check ran on any of
  the 40 practice commits** (a check costs 0.1-5 s). The same commit, fresh-cloned and rebuilt here, has the feature on, the
  baseline and stage map loading, and runs 3 checks on the 30 samples (`work/exp_fronthalf/image_probe.py`). Conclusion: no practice
  commit selects test_execute_tpcds through a front-half function-body change. The probe found almost nothing there either.
- The practice gap is therefore NOT the slow executor test. Strong candidate, measured here: any class-level edit in the base
  `Dialect`, `Generator`, `Parser` or `Tokenizer` class selects 1347-1585 tests = **27.6-36.8 s** per commit (about 209 True/False
  flags in those four classes alone). Not yet confirmed on practice.
- Public board at the same time (195 teams): we are rank 83 (216.4). Top: 138.1, 146.3, 151.4, 152.2, 153.7, 156.3, 158.1;
  rank 10 = 166.9; about 30 teams between 172 and 186; rank 41 = 185.6.
- Server CPU is roughly 1.3-1.6x this PC's Docker (baseline 1.06 s for 40 commits there, 0.63 s for 30 here).
- Clock: this PC is on Eastern time; one submission per hour (next slot about Sat 3:26 AM); cutoff Sun 11:00 AM EST.

## 9. Diagnostic submission and class-attribute selection (Sat Nov 21, ~03:10 ET)

**Diagnostic build, pushed, to be submitted in the 3:26 AM slot:** branch `diag/telemetry-1`, commit
`89112d3e66cf0855d956da05dff65f94fae69253`. Same selections as 38923fa; it reports how our own selector classified the practice
commits through the two numbers we get back (comment block above `class Diagnostic` in its selector.py). No misses possible (it
only ADDS tests to one empty commit), scores ~240-300 s on purpose, never a final candidate.
Decode with `python work\exp_fronthalf\decode_diag.py <simulated> <cpu>` -> cost of run-everything commits, of class-level
commits, of function-body commits, whether the early cutoff is usable on the server and whether it would have run.
(The first push of that branch pointed at 38923fa because a commit command failed on PowerShell quoting; use `git commit -F file`.)

**Executor-side extension of the early cutoff (BR_EC_BACK / BR_EC_PLAN_DIFFS, still off by default):** real-test audit in Linux,
80 fresh mutants: 42 dropped, **42 of 42 pass the real test**; of 15 kept, 1 would have passed.

**Class-attribute selection (branch `exp/class-attrs`, uncommitted): the likely practice gap.**
Measured: a class-level edit in the base `Dialect`/`Generator`/`Parser`/`Tokenizer` costs 27.6-36.8 s with the old rule
("every test that used the class"). 17,962 of sqlglot's lines are class-level (16%), 3,009 of them in those four files.
- `tools/build_map/attrrec.py`: pytest plugin; wraps every plain class attribute in a descriptor and records which test reads
  which (class, attribute). Transparent: all 1620 tests pass with it. Recorded twice in br-mapper: whole suite warm
  (`work/attr_out/warm`), and **every test alone in a fresh process** (`tools/build_map/attr_isolated.py`, `work/attr_out/iso`).
  `tools/build_map/build_attr_map.py` -> `maps/attr.json.gz` (6361 attributes, 187 KB).
- `classstate.py`: imports everything with the patched source and fingerprints all 10,094 class attributes and module globals;
  the diff with the pinned commit (baseline built in the image) is the exact set of attributes the patch changes, including ones
  derived at import (ktz3: `arg_types` and `required_args`). 0 unstable keys over 5 runs, ~1.2-1.9 s CPU.
- selector: `resolve_class_level()` runs only the tests that read a changed attribute. Falls back to the old rule when the
  patch also changes other Python code, changes state the map cannot observe, is already cheap (< 7 s), or the readers alone
  would not beat the old rule; an import error means run everything. Off switch `BR_CLASSATTR=0`.
- Results so far: samples 157.7 -> 149.2 s (535z: 171 tests -> 3, the failing one included); flag flips 27.6-36.8 s -> 2.0-4.0 s
  when few tests read the flag; older labeled sets 0 misses (eval_big 1152.7 -> 1015.4). First 6 of 120 freshly labeled
  class-level mutants (`work/mutants_cls`, labels `work/label_cls.json`, full suite in br-mapper): 145.8 -> 53.8 s, 0 misses.
- Evaluate: `work\.venv\Scripts\python work\exp_fronthalf\eval_labeled.py work\mutants_cls work\label_cls.json work\eval_cls BR_CLASSATTR=0 -- BR_CLASSATTR=1`
  (needs `BR_EC_BASE` and `BR_CA_BASE` pointing at baselines made by the same interpreter).
- To do: finish labeling (about 90 min), require 0 misses on all 120, sandbox CPU numbers on an idle machine, then ask the user
  before committing/pushing/submitting.

## 10. Diagnostic result (BB-F4BEB122, Sat 3:31 AM) and what it means

Submitted by Claude through the signed-in built-in browser with the user's OK. All 3 runs: **218.877 s simulated, 53.03 s CPU,
0 missed** (score 271.9; final pick still BB-5E85C0A3; next slot 4:31 AM). Decoded (`decode_diag.py 218.877 53.03`):
| Bucket of the 40 practice commits | Cost with the current selector |
| --- | --- |
| run-everything fallbacks | **0 s** (none) |
| class-level / import-time edits | **99 s** |
| function-body edits | about 57-59 s |
| everything else (fixtures, test files, docs) | about 58-60 s |
- The early cutoff IS usable on the server (flag set) and would have run no check: no practice commit qualified.
- So the class-attribute work (section 9) targets the largest bucket.
- "Everything else" is larger than small fixture commits explain. Likely cause, measured on the map: an edit to
  `tests/fixtures/optimizer/tpc-ds/tpc-ds.sql` or `tpc-h/tpc-h.sql` selects all 60 TestExecutor tests = **25.5 s**, because
  `setUpClass` opens every TPC file and class-setup reads are credited to the whole class. The samples' fixture commits look
  file-uniform (11 touches, none on those two files); with 29 .sql fixture files, about one such commit per 40 is expected.
  Idea (not built): for an edit to tpc-ds.sql only, run the early cutoff's front half on the patched fixture; if every
  fingerprint is identical (most lines are expected output the executor test never reads), drop test_execute_tpcds.

**Class-attribute selection, state at 03:38:** table-entry refinement added (when only the body of a function stored in a
class-level table changes, run the tests whose coverage includes that lambda's lines). 54 of 120 real-labeled class-level
mutants so far: 0 misses; at 46 labeled the simulated cost was 1027.2 -> 352.8 s. All features together on the 135 older
labeled patches: 2326.4 -> 1611.6 s, 0 misses. `classstate.py` check now ~1.1 s CPU (function hashes cached).

## 11. The older class rule can MISS (found Sat ~04:05 ET); the class-attribute rule does not

Hand-picked class-level patches (`work/exp_fronthalf/make_adversarial_cls.py` -> `work/mutants_cls_adv`, labeled with the full
suite into `work/label_cls_adv.json`). The older rule "every test that used the class or a subclass" (what sub-01 = the current
final pick BB-5E85C0A3, and 38923fa, do) left failing tests out on 3 of the first 6:
| Patch | Edit | Failing | Older rule left out |
| --- | --- | --- | --- |
| x01 | `MySQL.TIME_MAPPING` entry | 10 | test_hive::test_time, test_presto::test_to_char (other dialects copy the table at import) |
| x04 | base `Dialect.UNNEST_COLUMN_ONLY = True` | 1117 | test_dialect::test_lazy_load (sqlglot.dialects.duckdb fails to import) |
| x06 | `MySQL.Tokenizer.IDENTIFIERS` | 43 | 4 tests of redshift / spark / teradata / dialect |
The class-attribute rule selected every failing test on all 6 (the import-state diff sees the derived attributes in the other
classes; an import error selects everything). On random class-level mutants the older rule has not missed so far (0 of 82),
so the hole needs an attribute that other, unrelated classes copy at import: uncommon, but 300 s each.
Consequence: the class-state check now ALWAYS runs for class-level assignments (no cost gates), and state the map cannot
observe selects every test. The older rule remains only as the fallback when the check cannot run or the patch also changes
other Python code.
Also fixed: for "only the body of a table entry changed", the tests are (ran the entry's lines) AND (read the table); the
line map alone also credited a test that merely imported the file while it was recorded (6ze8 picked up test_generator_memory).

## 12. Status at 04:35 ET: candidate for the 4:31 slot, and what comes next

**User's standing instruction (Sat ~03:40):** "Yes, if it's clean": commit, push and submit the class-level fix once all 120
labeled class-level mutants show 0 misses. Submitting goes through the signed-in built-in browser (form fields `repo`, `commit`).
- 98 of 120 labeled: old rule 2028.3 s -> candidate 740.4 s, **0 misses**; no fallback was needed on any of them.
- Tricky set: 11 of 12 labeled, candidate 0 misses (older rule misses 3). x12 still to label.
- Sandbox (image `br-ca`, deadline lifted): samples 148.453, eval_big 1042.358, eval_proxy 298.289, eval_rare 403.028,
  tricky 193.392: all 0 misses, valid output. With the default 140 s deadline on an oversized 70-commit class-only set the run
  finished in 139 s wall, 13 checks skipped (older rule), still 0 misses.
- **Lesson:** Docker's VM has 16 GB. Three labelers plus sandbox runs exhausted it at ~04:22 and the engine hung; I stopped
  the jobs, ran `wsl --shutdown`, restarted Docker Desktop, and resumed with ONE labeler (`--jobs 6 --parallel 2`). Never run
  more than about 12 pytest workers in Docker at once. Labels are backed up in `work/label_cls.before_restart.json`.

**Final validation of the candidate (04:52, idle machine):** all 120 class-level mutants labeled by the full suite:
older rule 2541.7 s -> candidate 938.1 s simulated, **0 misses** (per patch: core files 34.2 -> 12.8 s, other files 9.4 -> 3.3 s).
Sandbox with the image built from the exact committed tree: 120-set 938.139 s + 119.3 s CPU (about 1.0 s per class check),
0 misses; samples 148.453 + 9.8 s CPU = 158.3 s (class feature off: 157.741 + 5.6 = 163.3; sub-01: 175.1), 0 misses;
clean `--no-cache` build 44 s. Tricky set 11 of 12: 0 misses.

**Next features, prototyped in scratch copies only (NOT in the candidate):**
- `work/exp_fronthalf/patch_fixture.py <dir>`: edits to `tests/fixtures/optimizer/tpc-ds/tpc-ds.sql` (25.5 s today): re-run the
  early cutoff's front half on the patched fixture; 5 of 12 junk-token mutants keep every fingerprint and drop the 21.5 s
  test (25.5 -> 10.2 s). Needs labels for `work/mutants_fx` (18 patches) before use.
- `work/exp_fronthalf/patch_module.py <dir>`: MODULE-LEVEL assignments (constants, the tables in `sqlglot/typing/*.py`,
  `executor/env.py`). Today they cost 25.8 s on average over 30 random mutants (`work/mutants_mod`), many of them run-everything;
  the prototype brings that set from 774.2 s to 461.4 s. They are part of the diagnostic's 99 s bucket together with
  class-level edits and def headers. Needs the 30 labels (`work/label_mod.json`, 2 done, labeling was paused) before use.
  Both patch scripts apply on top of the candidate's files; apply them in a copy, test, then port.

## 13. Practice result of the class-attribute build (BB-EA6DA914, Sat 4:53 AM): 208.5 s, new best

Commit `5b51ebca1dfb5183e13d7889528fee8727169d19` (branch `exp/class-attrs`, pushed), submitted by Claude through the signed-in
built-in browser under the user's "Yes, if it's clean". All 3 runs: **202.814 s simulated, 5.65 / 5.59 / 5.69 s CPU, 0 missed**.
- vs 38923fa / sub-01: simulated 215.778 -> 202.814 (-13.0 s), CPU 0.98 -> 5.65 (+4.7 s, about 3-4 class checks), score
  216.8 -> **208.5**. The page now says the pick BB-5E85C0A3 "is not your best score: BB-EA6DA914 scored better";
  **the final pick is still BB-5E85C0A3** (not changed; that is the user's decision). Next slot 5:53 AM.
- So class-level ASSIGNMENTS were only a small part of the diagnostic's 99 s bucket on practice. The rest of that bucket is
  some mix of: module-level constants, def headers, class-level statements that are not plain assignments, patches that mix a
  class-level edit with other Python changes, and class attributes nearly every test reads (still ~36 s each).
- Working branch now: **`exp/module-level`** (from 5b51ebc, uncommitted): `patch_fixture.py` and `patch_module.py` applied
  (TPC-DS fixture mode of the early cutoff; module-level assignments through the import-state check; class-state VERSION 3).
  No regression on the 120 class-level labels (0 misses, 938.1 s). Labels being made, one labeler at a time:
  `work/label_cls_adv.json` (x12), `work/label_mod.json` (30 module-level), `work/label_fx.json` (18 TPC fixture mutants).
- Idea for a second diagnostic if needed: split what is left of the class bucket (module constant / def header / deferred but
  still expensive / other class-level) the same way as section 9's diagnostic.

## 14. Branch `exp/module-level` (Sat ~06:00 ET): three more import-time kinds, validated, NOT committed or submitted

No user answer yet to "May I keep submitting builds each hour while you're away?", so nothing new was committed, pushed or
submitted after BB-EA6DA914. Everything below sits in the working tree of `exp/module-level` (on top of 5b51ebc).

**What the branch adds** (scripts that produced it, applied in this order: `work/exp_fronthalf/patch_fixture.py .`,
`patch_module.py .`, `patch_header.py .`; class-state baseline VERSION 4):
1. **Module-level assignments** (constants, tables such as `sqlglot/typing/*.py`, `executor/env.py`): deferred like class-level
   ones and decided from the import-state diff. A changed function -> tests that ran its lines; a changed module global holding
   data -> tests that ran a function line mentioning its name (static scan, `name_line_tests`); bodies of functions in a
   module-level table -> tests that ran those lambdas' lines; anything unobservable -> every test.
2. **Function headers** (signature, defaults, decorators) of defs at module or class level: same check; the tests that ran any
   line of the edited def are always included; a changed function object (or the wrapper around it) -> tests that ran its lines.
3. **TPC-DS fixture file**: the early cutoff's front half is re-run on the patched fixture (`BR_EC_FIXTURE`).

**Why it matters (real labels, full suite in br-mapper):**
| Set (random single-token mutants, file-uniform) | Old rule: cost, misses | Branch: cost, misses |
| --- | --- | --- |
| 30 module-level (`work/mutants_mod`, `label_mod.json`) | 774.2 s, **2 MISSES** (g51005 left out 157 of 171 failing tests; g51010 366 of 1419) | 461.4 s, 0 |
| 30 function-header (`work/mutants_hdr`, `label_hdr.json`) | 770.5 s, 0 | 438.0 s, 0 |
| 18 TPC fixture (`work/mutants_fx`, `label_fx.json`) | 458.2 s, 0 | 381.5 s, 0 |
| 120 class-level, 12 tricky class-level | unchanged vs 5b51ebc: 938.1 s / 245.0 s | 0 misses (older rule misses 3 tricky) |
| 135 older labeled patches | | 1864.8 s, 0 misses |
- The old rule's 2 misses on 30 random module-level mutants are the important part: **every build submitted so far, including
  the final pick BB-5E85C0A3 and BB-EA6DA914, can miss on a module-level edit** (about 7% of them here). None of the 40 practice
  commits triggered it. Module-level lines are ~8% of sqlglot's lines, headers ~10%, class-level ~27%.
- Fixture labels confirm the reading of the test code: a tpc-ds.sql edit fails `test_tpcds` and sometimes `test_execute_tpcds`,
  never the other 59 TestExecutor tests the reads map selects (possible further saving: 10.1 -> ~4.2 s).
- Sandbox with the image built from the staged tree before the header change (f170df7d): every set 0 misses; samples
  148.453 + 9.95 s CPU. Must be redone for the final tree (header handling was added afterwards).

**Final check of the whole branch (06:09, image `br-ml` built from the staged tree 52f5d3c9):** sandbox, 0 misses on every set:
samples 148.453 + 10.1 s CPU = 158.5; module 30: 461.44 + 28.4; header 30: 438.024 + 31.3; fixture 18: 381.477;
class 120: 938.139; tricky 12: 245.049; eval_big 906.62 (was 1015.0 on 5b51ebc); eval_proxy 298.289; eval_rare 403.028.
**It is staged, not committed.** Waiting for the user's go-ahead to commit, push and submit (slot open since 5:53 AM).
Suggested commit message: "Module-level assignments, function headers and the TPC-DS fixture through the import-state check".

## 15. `exp/module-level` SUBMITTED (BB-1E9B075A, Sat 11:43 AM): 214.9 s, worse than the best; what the data says next

User said "Yes, submit it". Commit `50833960f2e7c48edc64195b175a1bc92d56ef3a` (branch `exp/module-level`, pushed; clean
`--no-cache` build of the commit 27 s; sandbox samples 148.453 + 10.1 CPU, 0 misses). Practice, all 3 runs:
**194.714 s simulated, 18.68 / 20.95 / 20.16 s CPU, 0 missed -> 214.9 s.**
- vs BB-EA6DA914 (5b51ebc, 208.5): simulated -8.1 s, CPU +14.5 s -> **6.4 s WORSE**. Every import-state check costs ~0.82 s
  CPU here (more on the scorer) and now runs for module-level and header commits too; on practice that bought only 8 s.
- Final pick is still **BB-5E85C0A3** (page: "is not your best score: BB-EA6DA914 scored better"). Next slot 12:43 PM.
- Still true: 5b51ebc / 1a9a77b can miss on module-level edits (2 of 30 random ones), 5083396 cannot. Practice score alone
  does not show that.

**Where the samples' 148.5 s go (`work/exp_fronthalf/where_time.py`, `waste.py`; cheapest correct answer = 94.4 s):**
- Commits where some test fails (18): we cost 94.7 s vs 94.4 s perfect. Nothing to gain there.
- Commits where NOTHING fails but we select tests (8): 53.8 s, all waste: 66ks 24.8 (test_execute_tpcds, executor-side
  change), 4d63 9.1 (class attribute read by 273 tests), 2haz 8.7 (85 tests), five small ones 2.0-2.5 each.
- The whole suite is only **103 s of recorded time**: test_max_nodes 28.8, test_execute_tpcds 21.5, test_generator_memory
  6.7, test_execute_tpch 3.9, test_optimized_tpch 3.5, test_scope 2.5, test_tpcds 2.2, test_annotate_funcs 2.0; the other
  ~1610 tests total about 30 s.
- Leaders (138 on 40 commits = ~104 per 30) are within ~10 s of the perfect answer, so they detect "nothing fails".
  The kit README says: "Running sqlglot's tests while you decide is allowed, and you pay for the time."

**Measured on labels (`first_fail.py`, `what_if.py`, `slow_alone.py`), tests run cheapest-first, stop at the first failure:**
- Official samples: in all 18 failing commits the first failing test appears within 0.07 s of recorded test time.
  All 327 labeled code patches: within 0.34 s for 90%, but up to 19.7 s (huge selections where one late test fails).
- SOUND version ("run everything selected ourselves; a failure -> schedule as today; all pass -> select nothing"):
  samples 148.5 -> 94.7 simulated + ~36 s CPU if run blindly; gated (only when the run costs less than scheduling) about
  -10 s net for the small commits, plus test_execute_tpcds through `BR_EC_BACK=1` (measured in the sandbox, 0 misses:
  samples 158.5 -> 152.2, eval_big 994.4 -> 973.2, eval_proxy 321.1 -> 305.2, eval_rare 437.6 -> 383.7).
- RISKY shortcut ("all tests of at most 3 s pass -> drop the slower ones too, without running them"): on 327 labeled code
  patches it would apply 37 times and miss 0 times (a slow test failed alone 4 times, always with no cheap candidate at
  all, where the shortcut does not apply). With a 1.5 s limit it misses 2 (only test_annotate_funcs, 1.96 s, fails).
  Samples: 66ks alone is +23.8 s. 0 of 37 is not proof (95% bound about 8% per use; break-even is about 7%).
  **This is the user's decision, not made yet.** Never apply it to fixture edits (12 of 18 TPC fixture mutants would miss).
- A time budget ("run 3 s, then give up and select nothing") misses 12 of 225 failing labeled commits: rejected.

**Existing pieces to reuse:** `prerun.py` (fork + swap the patched function's code, run tests with unittest; 617 audited
verdicts, 0 errors) and `try_prerun` in selector.py (`BR_PRERUN`, off). Today on the samples it gains only 1.8 s because
(a) the image has no test packages (`pytz` missing -> whole commit kept; test_executor/test_optimizer need duckdb, numpy,
pandas; versions in `requirements.lock`), (b) a SKIPPED test counts as passed (must count as unknown before any use),
(c) the first import of a test module is charged to one commit, so optimizer tests are never tried, (d) it never stops at
the first failure, (e) class-level/module-level patches cannot be code-swapped (they need the patched import the
class-state child already does).

## 16. Branch `exp/cheaper-checks` (Sat ~12:35, from 5083396): validated, STAGED, not committed / pushed / submitted

Staged tree `efcd18f2b3c2b8b3f572f30a8471b08550aa1823` (image `br-cc` built from it, clean build 22 s). Three changes:
1. **Cheaper import-state check** (`classstate.py`, `earlycut.py`): 77% of a check was fingerprinting, over half of that
   spent walking every rule for plain constants, classes and enum members. Fast paths write the same text directly; functions
   and bound methods that sit in many tables are written once and replayed (text + the visited-object bookkeeping).
   Verified byte-identical: all 11,152 digests equal the old code's baseline, and the early-cutoff baseline is unchanged
   (`work/exp_fronthalf/time_classstate.py`, `cmp_ec_base.py`). Baseline build 0.777 -> 0.61 s CPU; a check ~0.91 -> ~0.79 s
   all-in (sandbox: class 120-set 125.1 -> 108.4 s CPU, module 28.3 -> 25.1, header 31.3 -> 28.0). What is left is fixed:
   importing sqlglot 0.21 s, fingerprint ~0.42 s, compiling the patched file, starting Python.
2. **Fingerprint hole closed** (class-state VERSION 5): a bound method answered `__code__` with its function's, so
   `{"ABS": Abs.from_arg_list}` and `{"ABS": Acos.from_arg_list}` had the same digest. Bound methods now include what they
   are bound to. 26 keys change (the `FUNCTIONS` tables of `Parser` and 25 dialect parsers); 0 unstable keys over 4 runs.
3. **`BR_EC_BACK` on by default**: a patch to code that only the executor half of test_execute_tpcds runs re-executes the
   affected queries and compares result fingerprints (~15-17 s CPU for all 57 queries against 21.5 s + a worker).

Sandbox, default flags, 345 labeled patches, **0 misses, valid output**: samples 126.999 + ~26-28 CPU (was 148.453 + 10.1);
eval_big 862.164 + 117.6; eval_proxy 255.381 + 54.8; eval_rare 317.212 + 71.9; module 461.44 + 25.1; header 438.024 + 28.0;
fixture 381.477 + 24.9; tricky 245.049 + 11.3; class 938.139 + 108.4. With `BR_EC_BACK=0` it prints exactly what 5083396
prints (samples 148.453, module 461.44, header 438.024). CPU readings drift ~10% with machine temperature: compare images
back to back (the same old image read 25.2 s at 12:00 and 28.0 s at 12:35).
Expected on practice: about 2-3 s less CPU from the checks, plus whatever the executor check finds there (unknown).

**Not built (needs the user's decision before any use): "trust the cheap tests"** = run every cheap candidate ourselves
(prerun.py, fork + code swap) and, when all pass, also drop the 5 giant tests without running them. Simulated with measured
run costs (`sim_try.py`): samples about -18 s per 30 commits, 0 misses on 327 labeled code patches; the careful version
(drop only what we ran) is worth about -3 s. To build: test packages in the image (pytz, tzdata, duckdb, numpy, pandas from
`requirements.lock`), skipped tests = unknown, stop at the first failure, amortise test-module imports, audit against labels.

# >>> END OF HANDOFF. Full history below. <<<


> **STATUS (Sat Nov 21 ~00:30 ET): FINAL ENTRY = BB-5E85C0A3 (baseline sub-01, 1a9a77b), chosen manually by the user.**
> Page confirms: "Your pick for the final, BB-5E85C0A3, is not your best score: BB-F7D24F70 scored better. The final
> enters your pick unless you choose another." genmem experiment STOPPED. Do not submit anything new without a
> deliberate decision, and re-check the pick afterwards.

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

## Probe vs baseline, final local comparison (Nov 21, ~02:30)

Scorer sandbox, 182 labeled patches (samples, slow-test 77, exotic 39, cache adv 2, proxy adv 14, rare-input adv 20):
baseline 2588.4 s, 0 misses; probe variant (exp/probe @ 1f0c94a) 2529.8 s, 0 misses (-58.6 s). Samples: 175.3 -> 154.6 s.
Probe audit on rare/proxy: every failing bug infected; 2 harmless calls, both correct (8/8 overall).
Correction: an earlier local check of the baseline on proxy/rare ran without its map (fell back to all tests). The
sandbox re-check found one apparent baseline miss on rare `m22003c`, traced to a crashed labeling run: 3 clean
re-runs all fail only test_execute_tpcds (selected). Label corrected in work/label_rare.json (note kept).
Open risk for any final use of the probe: build time (542 s under load vs 600 s limit) -> trim dependencies first.

## Probe practice result (Sat 12:01 AM): negligible gain

Submission BB-F7D24F70 = exp/probe @ 1f0c94a (tag sub-02-probe): **216.4 s**, 0 misses x3 (213.771 s simulated +
2.62 s CPU) vs baseline BB-5E85C0A3 216.8 s (215.778 + 1.06). Gain 0.4 s: not worth the build-time risk (542 s
under load vs 600 s), forks and test packages. DECIDED (Sat ~00:30): the user switched the final entry back to BB-5E85C0A3
(the page auto-entered BB-F7D24F70 as "best score").

## Waste by slow test (182 labeled patches, baseline)

test_execute_tpcds: selected 74, failed 22, wasted 882 s (mostly function-body edits); no safe fix found
(proxy refuted, slicing rejected, probe gain only 0.4 s on practice).
test_generator_memory: selected 31, failed 0, wasted 164 s (149 s from class-level dialect edits) -> NEXT EXPERIMENT.

## STOPPED: test_generator_memory, affected dialects only (stopped by user Sat ~00:30, nothing built)

Reason: only 1 of the 30 official samples schedules test_generator_memory (535z), so the expected gain on 60 final
commits is ~5 s net of our own CPU, against a 300 s miss risk and real design traps (dialects loaded earlier can mutate
shared tables; generator construction also reads Dialect/Tokenizer settings). The fake-bug sets overstate slow-test
savings (same lesson as the probe: -58.6 s locally, -0.4 s on practice). Plan kept below for reference only.

Goal: for a class-level edit in a dialect class, instead of scheduling test_generator_memory (6.7 s), run its REAL
logic ourselves (tests/test_generator.py::TestGenerator::test_generator_memory: for each dialect, build 100 generators,
gc, tracemalloc 1000 more, assertLess(after-before, 1024)) for only the affected dialects, against the PATCHED code.
- Branch: new `exp/genmem` from review/wip. Never touch selector/v2 (sub-01 @ 1a9a77b) or exp/probe (sub-02-probe @ 1f0c94a).
- Class-level edits can't be hot-swapped: run a subprocess with a patched sqlglot copy in /tmp (sqlglot pkg ~3.5 MB,
  /tmp is 64 MB) first on PYTHONPATH, then call the real test method with `Dialects` restricted to affected dialects
  (or run all dialects if unsure). Keep tracemalloc leak detection exactly as the test does it.
- Affected dialects: any dialect whose Dialect/Tokenizer/Parser/Generator class has the edited class in its MRO, PLUS any
  dialect whose classes copy the edited attribute at import (e.g. KEYWORDS = {**Other.Tokenizer.KEYWORDS}). If unsure,
  fall back to scheduling the test (never guess).
- Measure: real CPU per affected-dialect check in the scorer sandbox; score vs baseline on all labeled sets
  (sample, work/eval_big, eval_exotic, eval_adv, eval_proxy, eval_rare).
- Adversarial bugs to write and label (tools/mutate/label.py): dialect inheritance (edit a parent dialect's generator
  setting), shared settings copied across dialects, generator __init__ / construction code, a leak (e.g. a class-level
  list that grows per generator), and an exception in construction for one dialect only.
- Enable only if 0 misses on all labels + adversarial AND net CPU savings are real. Then practice-submit, and still
  choose the final deliberately.
- Image: the baseline image has no test packages; test_generator.py must import without duckdb/pandas (check its imports;
  if it needs packages, measure the build time, which must stay far below 600 s).

## CHECKLIST before Sun 11:00 AM ET

- [x] DONE Sat ~00:30 (user): final pick set to BB-5E85C0A3. Was: find the control on the signed-in submissions panel to choose the final entry; select BB-5E85C0A3 (baseline,
      1a9a77b) unless we decide otherwise. Probe BB-F7D24F70 (216.4 s) is currently auto-entered as "best score".
      If no control exists: ask organizers in the event chat to set the final to BB-5E85C0A3.
- [ ] Keep the repo public; never rewrite submitted commits (1a9a77b, 1f0c94a).

- [ ] Ask organizers whether the final rebuilds images or reuses the practice build (informational).
- [ ] Optional last review of both submissions; change the pick only by deliberate decision.

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
