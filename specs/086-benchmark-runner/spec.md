# Spec 086 — replay orchestrator

- **Status:** draft (SDD Phase 1 — Specify)
- **Owner:** benchmark
- **Issue:** #1996
- **Constitution:** [`AGENTS.md`](../../AGENTS.md) → *Benchmark integrity (M1–M3)*
- **Methodology:** [`blog/spec-driven-development.md`](../../blog/spec-driven-development.md)
- **Related:** [`benchmark/runner.py`](../../benchmark/runner.py) (the module under test),
  [`specs/005-repo-set`](../005-repo-set/spec.md) (the repo-set entries `run_multi_replay`
  materializes), [`specs/077-benchmark-taskgen`](../077-benchmark-taskgen/spec.md) (`generate_tasks`,
  which `run_replay` calls but does not re-document here),
  [`specs/069-benchmark-generalization-gate`](../069-benchmark-generalization-gate/spec.md) (the gate
  that consumes `generalization_gap`), [`specs/073-benchmark-run-clean`](../073-benchmark-run-clean/spec.md)
  and [`specs/074-benchmark-repeatability`](../074-benchmark-repeatability/spec.md) (downstream
  consumers of the artifact shapes this spec pins, not producers of them)

This spec makes the **existing, implicit** replay-orchestration contract explicit. It describes the
as-built behavior of `benchmark/runner.py`; it introduces **no behavior change**. Where the as-built
behavior is narrower, wider, or stranger than a docstring implies, this spec states the as-built
behavior and flags the gap — it does not silently correct it.

## Why

`runner.py` is the last module on the freeze → taskgen → runner spine without a contract spec (005
curates the input repo sets, 077 pins the generator, 002/004 pin the scoring it calls, 069 gates its
generalization output) — and the only one that defines the **artifact shape** roughly forty gate,
integrity, and outlook modules across `benchmark/` all read: `composite_mean`, `composite_parts`,
`scored_repos`, `skipped`, `per_repo[].error`, `tasks`, `judge_report`, `generalization_gap`. Each of
those consumers independently re-derived rules this module never wrote down — most visibly the
unscored-placeholder convention (`composite_mean: 0.0` with `scored_repos: 0`), which `run_eval`,
`compare_eval`, `trend`, `promotion`, and `generalization_gate` each re-detect on their own. Writing
the contract down gives reviewers of a `runner.py` change something to check it against, instead of
re-deriving intent from the diff.

## User stories

1. **As a benchmark operator**, I can predict exactly what `run_replay`, `run_multi_replay`, and
   `run_generalization_report` return for a normal run, an unscoreable repo, and a partially-failed
   batch — including which fields are `0.0` placeholders versus real scores.
2. **As a gate maintainer**, I know which of `runner.py`'s behaviors are guarantees I can rely on
   (the `tasks > 0` aggregation gate, per-repo error isolation, `checkout_root` cleanup) versus which
   are as-built quirks a docstring overstates (the dead `cleanup` flag, the silent `weight_sweep`
   winner-skip, the `run_generalization_report` catch narrowed to `RepoSetError`).
3. **As a reviewer**, the agent-entrypoint loader's three distinct failure messages, the judged-
   submission projection, and the multi-repo merge-precedence rule (`{**meta, **res}`) are written
   down, so a `runner.py` change is checked against a stated contract instead of re-derived from the
   diff.

## Constants

- `run_replay` defaults SHALL be `n_tasks=3`, `horizon=5`, `seed=0`, `baseline="heuristic"`
  (`DEFAULT_BASELINE`), `w_judge=0.6`, `w_objective=0.4`, `dual_order_judge=True`, `min_history=10`.
  The default opponent SHALL NOT be `empty`: against the empty floor every non-trivial challenger
  wins every task, pinning the judge component at `1.0` so it cannot discriminate (#2379). `empty`
  remains selectable via `--baseline empty` as the explicit calibration floor.
- `CLONE_TIMEOUT_SECONDS` SHALL be `300` — the wall-clock bound on `git clone` inside
  `_materialize_repo_source`.
- The challenger-perspective judge component map (`_JUDGE_COMPONENT`) SHALL be
  `{"challenger": 1.0, "tie": 0.5, "baseline": 0.0}`.
- `WEIGHT_SWEEP_GRID` SHALL be the five `(w_judge, w_objective)` pairs
  `(0.2, 0.8), (0.4, 0.6), (0.5, 0.5), (0.6, 0.4), (0.8, 0.2)`, in that order.
- The revealed-record SHA prefix on a `run_replay` row (`row["freeze"]`) SHALL be 10 characters
  (`task["freeze_commit"][:10]`).

## Acceptance criteria (EARS)

### Agent entrypoint loading (`load_solve`)

- WHEN `agent_file` does not exist or is not a regular file THEN `load_solve` SHALL raise
  `RuntimeError` naming the file and stating it "does not exist or is not a regular file" —
  distinct from the two failure messages below.
- WHEN `importlib.util.spec_from_file_location` returns `None` (unsupported file type or missing
  loader) THEN `load_solve` SHALL raise `RuntimeError` stating "unsupported file type or missing
  loader".
- WHEN executing the loaded module raises any `Exception` THEN `load_solve` SHALL raise
  `RuntimeError` wrapping the original exception via `from exc`, distinct from the two messages
  above.
- WHEN the loaded module has no callable `solve` attribute THEN `load_solve` SHALL raise
  `RuntimeError` stating the file "does not define a callable 'solve' entrypoint".
- `load_solve` SHALL insert `os.path.dirname(os.path.abspath(agent_file))` into `sys.path` (at
  index 0) before loading, and SHALL NOT insert it a second time on a later call whose
  `agent_file` resolves to the same directory (`root not in sys.path` guard) — a **persistent
  process-wide side effect** across calls, not scoped to one `load_solve` invocation.

### Judged-submission projection (`_submission`)

- `_submission(out)` SHALL return exactly `{"philosophy", "plan", "rationale"}`, dropping every
  other key an agent's output dict carries (e.g. `action`, `version_bump`) — those keys reach
  `objective_score` from the raw `challenger` dict, never through the judge's view.
- WHEN `out` is not a `dict` THEN `_submission` SHALL return `{"philosophy": None, "plan": None,
  "rationale": None}` rather than raising.

### Repo-source materialization (`_materialize_repo_source`)

- WHEN `source` is a placeholder (`is_placeholder_source`) THEN `_materialize_repo_source` SHALL
  raise `RepoSetError` naming the source, regardless of `checkout_root`.
- WHEN `source` is a local directory (`os.path.isdir`) THEN the function SHALL return
  `(source, False)` without touching `checkout_root` — a local path is never cloned nor marked for
  cleanup.
- WHEN `source` is not a local directory and `checkout_root is None` THEN the function SHALL raise
  `RepoSetError` stating the source was "not found locally".
- OTHERWISE the function SHALL `git clone -q --` the source into
  `{checkout_root}/repo_{len(os.listdir(checkout_root))}` and return `(dest, True)`.
- A clone exceeding `CLONE_TIMEOUT_SECONDS` SHALL raise `RepoSetError` naming the timeout; a
  non-zero clone exit SHALL raise `RepoSetError` carrying the process's stripped stderr.
- **As-built (dead flag):** the second tuple element is documented as "whether it should be
  cleaned up" and is threaded through `run_multi_replay` into `selected[i]["cleanup"]`, but its
  **only** remaining use is exclusion from the per-repo `meta` dict
  (`{k: v for k, v in repo.items() if k not in ("repo_path", "cleanup")}`). No code path reads
  `selected[i]["cleanup"]` to decide whether to remove `dest` individually — every clone under a
  given `checkout_root` is removed only because `run_multi_replay` deletes the **entire**
  `checkout_root` in its `finally`. A caller that stopped removing `checkout_root` wholesale, or
  called `_materialize_repo_source` outside `run_multi_replay`, would silently leak every `True`
  clone; the flag reads like a live per-clone cleanup guarantee and is not one.

### Single-repo replay artifact (`run_replay`)

- WHEN `solve_fn` is given and callable THEN it SHALL be used as the challenger in place of
  `load_solve(agent_file)`; WHEN `solve_fn` is given and not callable THEN `run_replay` SHALL raise
  `TypeError`. The `agent_file` path is not consulted at all when `solve_fn` is supplied.
- WHEN `generate_tasks(...)` returns an empty list THEN `run_replay` SHALL return exactly
  `{"error": "no usable tasks (repo too small for horizon/min_history)", "tasks": 0}` — no other
  key is present on this branch.
- WHEN a challenger's `solve` call returns a non-`dict` THEN `run_replay` SHALL substitute `{}` for
  it (not raise) before computing `_submission`, `objective_score`, and the row's `overlap`.
- Each row SHALL carry exactly `task`, `freeze` (10-char SHA prefix), `winner`, `judge_order`,
  `overlap`, `objective`, `composite`; `winner` SHALL be one of `"challenger"`, `"baseline"`,
  `"tie"` per the `{"A": "challenger", "B": "baseline", "tie": "tie"}` decode of the judge's
  verdict.
- The returned dict SHALL carry exactly the keys `tasks`, `baseline`, `tally`, `decisive_margin`,
  `composite_mean`, `composite_parts` (`judge_mean`, `objective_mean`), `foresight`, `weights`,
  `rows`, `judge_order_stats`, `judge_report`, `offline`, `github_enriched`, `judge_dual_order` —
  on a run that produced at least one task.
- `decisive_margin` SHALL be `tally["challenger"] - tally["baseline"]` (ties excluded from the
  difference on both sides).
- The `work_dir`/`tempfile.mkdtemp` scratch root SHALL be removed in a `finally` — but **only**
  when the caller did not pass its own `work_dir`; a caller-supplied `work_dir` is left in place
  for the caller to manage.

### Weight sweep (`weight_sweep`)

- A row SHALL be included in the sweep's `scored` set iff it is a `dict` **and** its `winner` is a
  key of `_JUDGE_COMPONENT`.
- WHEN a row is not a `dict` (and is not `None`) THEN `weight_sweep` SHALL `logger.warning` and
  skip it.
- **As-built (asymmetric skip):** WHEN a row **is** a `dict` but its `winner` is missing or not a
  recognized key, `weight_sweep` SHALL skip it **silently** — no warning is emitted for this case,
  unlike the non-dict case above. A partially-corrupt artifact (well-formed rows with a bad
  `winner`) therefore sweeps as if it had fewer tasks, with no log trail distinguishing "fewer
  tasks were run" from "some rows were dropped".
- **As-built (zero-sum blend does not raise):** `total = (w_judge + w_objective) or 1.0` — a grid
  entry of `(0.0, 0.0)` SHALL yield `composite_mean: 0.0` for that entry (via the `or 1.0`
  fallback), not a `ZeroDivisionError`. `WEIGHT_SWEEP_GRID`'s shipped entries never hit this case;
  it is only reachable with a caller-supplied `grid`.
- WHEN the scored set is empty (no row qualified) THEN every grid entry's `composite_mean` SHALL
  be `0.0`.
- The sweep's per-task blend SHALL match `benchmark.score.composite_score` at that same
  `(w_judge, w_objective)` pair for every already-scored row, so sweeping at a run's own weights
  reproduces that run's reported `composite_mean`.

### Multi-repo aggregation (`run_multi_replay`)

- Exactly one of `repos`/`repo_set` SHALL be given; both `None` or both not-`None` SHALL raise
  `ValueError`.
- WHEN `repo_set` is given, selection SHALL follow `repo_set_partition` (if truthy) over
  `held_out` (if `True` and no partition given) over the default `"tuned"` — `repo_set_meta`
  records whichever `selection` string was actually used.
- WHEN the selected partition is empty THEN `run_multi_replay` SHALL raise `RepoSetError` naming
  the repo set and the empty selection, **before** any `checkout_root` is created.
- A materialization failure (placeholder rejection, clone failure/timeout, missing source) raised
  while building `selected` SHALL propagate after removing `checkout_root` — cleanup on this path
  is explicit (`except BaseException: shutil.rmtree(...); raise`), separate from the replay loop's
  own `finally`.
- **As-built (merge precedence):** each `per_repo` entry SHALL be `{**meta, **res}`, where `meta`
  is the repo-set entry with `repo_path`/`cleanup` excluded and `res` is that repo's `run_replay`
  (or synthesized error) result — a key present in **both** (there is none today, but the dict
  merge order means a future same-named key in `res` would win) is not merged, it is
  **overwritten** by `res`. `per_repo[i]["repo"]` reflects `meta["repo"]` only because `res` never
  defines a `"repo"` key today; that is convention, not an enforced contract.
- A repo whose `run_replay` call raises `RuntimeError` SHALL be recorded in `per_repo` as
  `{"error": str(exc), "tasks": 0}` merged under `meta`, and SHALL NOT abort the batch — logged at
  `logger.warning` naming the repo (`repo.get("repo") or repo.get("repo_path")`) and the exception.
  Only `RuntimeError` is caught this way; any other exception type from `run_replay` propagates
  and aborts `run_multi_replay`.
- A repo SHALL be counted toward `scored_repos`/aggregated into the composite means iff
  `res.get("tasks", 0) > 0` — a repo returning `tasks: 0` (too small, or the synthesized
  `RuntimeError` record above) is kept in `per_repo` but contributes to neither the mean nor
  `scored_repos`; `skipped` SHALL be `len(per_repo) - len(composites)`.
- `tally` SHALL sum each repo's own `tally` dict (defaulting missing/non-dict to `0` per outcome)
  across **every** repo in `per_repo`, scored or not — unlike the composite means, the tally is not
  gated on `tasks > 0`.
- The returned dict's aggregate fields (`composite_mean`, `composite_parts.judge_mean`,
  `composite_parts.objective_mean`) SHALL each be `0.0` when no repo scored (`_mean([])`) — the
  same unscored-placeholder convention `run_replay` uses for a single unscoreable repo.
- `repo_set_meta` (key `"repo_set"` on the result) SHALL be present iff `repo_set` was given (never
  present on the `repos=` list path).
- The `checkout_root` (when created) SHALL be removed in a `finally` covering the **entire** replay
  loop — a per-repo `RuntimeError` does not skip this cleanup, and it runs whether or not any repo
  in the loop itself raised.

### Generalization report (`run_generalization_report`)

- `run_generalization_report` SHALL call `run_multi_replay(repo_set=repo_set,
  repo_set_partition="tuned", **kwargs)` and the same with `"held_out"`, independently.
- **As-built (narrow catch):** WHEN a partition's `run_multi_replay` call raises `RepoSetError`
  THEN that partition's result SHALL be substituted with
  `{"error": str(exc), "scored_repos": 0, "composite_mean": 0.0}`, and the **other** partition
  SHALL still be attempted. Any exception type **other than** `RepoSetError` (for example, a
  `RuntimeError` that escaped `run_multi_replay`'s own per-repo catch, or a `ValueError` from the
  ambiguous-args check) SHALL propagate uncaught and abort the whole report — the docstring's claim
  that a partition failure "is recorded with its error rather than aborting the whole report" holds
  only for `RepoSetError`.
- `generalization_gap` SHALL be `round(tuned["composite_mean"] - held_out["composite_mean"], 3)`
  when **both** `tuned["scored_repos"]` and `held_out["scored_repos"]` are truthy (nonzero); it
  SHALL be `None` otherwise — including when exactly one side scored, so the gap is never reported
  from a single side.
- The returned dict SHALL carry exactly `repo_set`, `tuned`, `held_out`, `generalization_gap`.

## Out of scope

- `generate_tasks` and the freeze-point selection rules it implements (Spec 077).
- The judge's pairwise verdict mechanics (`judge_verbose`, order sensitivity) and the scoring
  formulas `objective_score`/`composite_score`/`foresight_breakdown` compute (Specs 002/004 and
  the score-family specs).
- Repo-set config validation and the `tuned`/`held_out`/tier partitioning `RepoSet` itself performs
  (Spec 005) — this spec treats `load_repo_set` and its returned entries as given.
- Downstream gates that read this module's artifact shape (`run_clean`, `repeatability`,
  `generalization_gate`, `trend`, `promotion`, and the rest of the ~40 consumers) — Specs 069/073/074
  and siblings.
- GitHub context enrichment and leakage scrubbing (`enrich_context`, `scrub_context`) — consumed
  as-is, not re-specified here.
- Tuning any default, threshold, or the weight-sweep grid's values.
- Fixing any of the as-built gaps this spec documents (dead `cleanup` flag, `weight_sweep`'s silent
  skip, the zero-sum blend fallback, the `run_generalization_report` catch narrowed to
  `RepoSetError`) — this spec pins current behavior; it does not propose or make a change.

## Verification

- `tests/test_spec_086_runner.py` exercises each EARS block above against in-memory fakes and tiny
  throwaway git repos (no network, no real repo clones): the three distinct `load_solve` failure
  messages plus its one-time `sys.path` insertion; `_submission`'s key projection and non-dict
  fallback; `_materialize_repo_source`'s placeholder/local/missing-root/clone branches and the dead
  `cleanup` flag's actual (non-)use; `run_replay`'s empty-task shortcut, non-dict-challenger
  degrade, and full key set; `weight_sweep`'s non-dict-warns-vs-bad-winner-silent asymmetry and the
  zero-sum grid entry; `run_multi_replay`'s partition selection precedence, empty-selection and
  materialization-failure cleanup, `{**meta, **res}` merge precedence, `RuntimeError` isolation and
  the `tasks > 0` gate, and `checkout_root` cleanup; and `run_generalization_report`'s
  `RepoSetError`-only catch plus the both-sides-scored `generalization_gap` rule. `run_replay` and
  `run_multi_replay`'s own end-to-end behavior over real tiny git repos stays covered by
  `tests/test_runner.py`; this file pins the contract with literal expected values, including the
  as-built gaps, rather than re-deriving them from the module.
