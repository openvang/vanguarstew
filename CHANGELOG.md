# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Benchmark: the default pairwise-judge opponent (`DEFAULT_BASELINE`) is now `heuristic`, not
  `empty`. Against the `empty` floor every non-trivial challenger won every task, pinning the
  judge component at `1.0` so it could not discriminate and inflating `composite_mean` toward the
  ceiling; the deterministic, LLM-free `heuristic` maintainer is a real bar a challenger must
  out-reason (#2379). `empty` remains selectable via `--baseline empty` as the calibration floor.
  **Note:** leaderboard scores and anchors produced against `empty` are not comparable to new
  ones and must be regenerated.

## [0.9.0] - 2026-08-07

This release also rolls up entries that had accumulated under _Unreleased_ since 0.3.0.

### Added
- **Persistent agent memory** — a trust-boundary-aware memory layer (`benchmark/memory.py`,
  `benchmark/source_memory.py`, `benchmark/memory_coverage.py`) threaded through the agent
  context/runner, with a receipt-safe `memory_commitment` bound into the attestation. Opt-in
  via the `private-memory` (`cryptography`) extra (#2374, #2235).
- **Private product runtime** — a self-hostable maintainer-assist service: the openvang agent
  factory (`openvang/`) and `vanguarstew_runtime` CLI/config/service, a systemd unit, and
  `Dockerfile` / `docker-compose.yml` / `.env.example` (#2372, #2371).
- Repo-set tooling: **freeze-window value validation** (`min_history >= 1`, non-empty
  `after`/`before`) and `scripts/validate_repo_set.py` CLI to check a repo-set JSON before
  replay (#325).
- Generalization (M3): **tuned vs held-out generalization report** — `run_generalization_report`
  / `scripts/run_eval.py --repo-set <cfg> --generalization` replays both the tuned and held-out
  partitions of a repo set in one call and reports a `generalization_gap` (tuned minus held-out
  composite mean). This is the M3 acceptance signal — held-out performance should not collapse
  relative to tuned — which the per-partition `run_multi_replay` left to manual comparison. A
  partition the config does not define is recorded with its error, so the report never aborts
  and the gap is reported only when both partitions scored a repo (#208).

### Changed
- **Rebrand to the `openvang` org** — repository transferred from `gittensor-vanguard` to
  `openvang`; `pyproject` authors/URLs and site links updated (#2374).
- `docker`: split the attested eval-image build-context allowlist into its own
  `docker/eval.Dockerfile.dockerignore`, so the root `.dockerignore` becomes the product ignore
  and the product image ships the `openvang/` + `vanguarstew_runtime/` packages. The attested
  eval TCB is unchanged, just relocated (#2375).

### Fixed
- Agent: the git-only context fallback omitted commit dates, silently disabling date-based
  release-timing prediction; dates are now carried on the fallback path (#2362).
- Benchmark gates (`benchmark/judge_gate.py`, `benchmark/regression.py`): the order-disagreement
  recompute accepted an incoherent telemetry block where `disagree > dual_order_tasks` — impossible,
  since `disagree` is a subset of `dual_order_tasks` (stale/hand-edited data). It produced a rate
  above 1.0 (`_disagreement_rate_from_telemetry`) and, for a `--generalization` artifact, pooled the
  fabricated count into a plausible-but-wrong overall rate that could flip `check_regression`'s
  `no_judge_instability_increase` verdict and block an otherwise-fine candidate. The recompute now
  requires `0 <= disagree <= dual_order_tasks`; `_disagreement_rate_from_telemetry` falls back to a
  literal `disagreement_rate` (else `None`) on impossible counts, and `_partition_disagreement_counts`
  signals the incoherent partition so `_disagreement` fails the pooled rate closed to `None` (the
  gate then passes vacuously per Spec 016, which is updated to match) (#1283).
- Benchmark reporting (`benchmark/judge_wlt.py`): `summarize_judge_wlt` read only the top-level
  `judge_report`, so a `--generalization` artifact (which nests a report under `tuned`/`held_out`
  and emits none at the top level) reported `unavailable` for every generalization run — while its
  sibling `win_rate` sums the partitions. It now sums the `tuned`/`held_out` W-L-T reports for the
  overall and exposes a `partitions` map (Spec 025 updated to match). Single- and multi-repo
  artifacts are unchanged.
- Benchmark reporting (`benchmark/dual_order_coverage.py`): `_task_total` read only the
  top-level `tasks` field, which a multi-repo run and each generalization partition never emit
  (task counts live under `per_repo[*].tasks`), so coverage was `n/a` for every aggregate run —
  contradicting the module's own docstring and its sibling `dual_order_share` (which reports a
  value on the same artifact). It now falls back to summing `per_repo[*].tasks` (fail-closed on
  a malformed entry), mirroring `coverage`/`sample_adequacy`/`repo_task_mean`; Spec 047 and its
  tests are updated to match.
- Benchmark reporting (`benchmark/gap_outlook.py`): the favorability verdict was sign-inverted.
  `generalization_gap = tuned - held_out`, so a **positive** gap means held-out performance
  dropped (worse generalization) — the sign `acceptance`, `runner`, and `gap_integrity` all use —
  yet `gap_outlook` reported `"favorable"` for `gap >= 0`. It labelled exactly the runs the
  `acceptance` gate *rejects* as favorable (and good generalization as unfavorable). The verdict
  is now `"favorable"` when `gap <= 0`; Spec 052 and its tests are corrected to match.
- Benchmark reporting (`benchmark/win_rate.py`): `summarize_win_rate` read only the artifact's
  top-level `tally`, so a `--generalization` artifact (which carries a tally under
  `tuned`/`held_out` only) reported `n/a` for every generalization run while sibling utilities
  (`offline_share`, `dual_order_share`, `order_agree_rate`, …) summed the partitions.
  Generalization artifacts now sum the `tuned`/`held_out` tallies for the overall and expose a
  `partitions` map, and the summary carries a `kind` field like its siblings.
- Benchmark reporting (`benchmark/order_agree_rate.py`): `summarize_order_agree_rate` read
  only the artifact's top-level `judge_order_stats`, so a `--generalization` artifact (which
  carries stats under `tuned`/`held_out` only) reported `agree_rate: n/a` for every
  generalization run while sibling utilities (`offline_share`, `dual_order_share`, …) correctly
  summed the partitions. Generalization artifacts now derive the overall rate from
  `_combined(tuned, held_out)`.
- Benchmark gates (`benchmark/repeatability.py`): `assess_repeatability` computed the
  coefficient of variation from the *population* standard deviation (`pstdev`) rather than the
  *sample* standard deviation. The repeats are a sample of a noisy run, so the CV must use the
  Bessel-corrected sample stddev; `pstdev` underestimates run-to-run spread by
  `sqrt(n/(n-1))` (~1.41x at n=2), biasing the reproducibility gate too lenient and flipping the
  `stable` verdict near the `max_cv` boundary (e.g. runs `[0.96, 1.04]` scored a CV of 0.04 and
  passed at `max_cv=0.05`, when the true sample CV is 0.057 and should fail).
- Benchmark gates (`benchmark/sample_adequacy.py`): `_partition_entries` collected the
  top-level `per_repo` list **and** the `tuned`/`held_out` partition lists additively, so an
  artifact carrying both shapes had every task counted twice (`_total_tasks` returned 14 for a
  7-task run), which then failed `all_tasks_decided` even when the tally accounted for every
  real task. It is now mutually exclusive — top-level `per_repo` wins, else the partitions —
  mirroring the sibling gates (`coverage`, `tally_integrity`, `weight_integrity`, ...). Stock
  multi-repo and generalization artifacts are unaffected.
- Leakage / fail-closed (`benchmark/github_context.py`): `_issue_timeline` swallowed a
  transient error on a page *after* the first and returned the partial events with
  `truncated=False`, so `_issue_record_at` trusted an incomplete timeline and reported
  `labels_as_of_t=True` — potentially asserting an as-of-T label that a later (unfetched)
  `unlabeled` event removed before T. A mid-pagination error now marks the timeline
  `truncated`, so the caller fails closed (omits labels) exactly like the page-cap case; a
  first-page error still yields `([], False)`. Extends the fail-closed guard from #345 (#865).
- Scoring correctness (`benchmark/score.py`): the objective anchor now recognizes the
  version-cut commit that release tooling authors under a chore/build Conventional-Commit type,
  such as `chore(release): 1.4.0` (standard-version), `chore(main): release 1.2.3`
  (release-please), and `build(release): 2.0.0`. The #431 "CC prefix is authoritative" rule
  had dropped these as plain chores, so `is_release_subject` returned False, `commit_kind`
  returned `chore`, and `released_version` returned None on a real release: a challenger that
  correctly anticipated the release, its bump level, and the `release` kind earned zero credit
  on all three axes of `objective_component`. Recognition is body-gated (the text after the
  prefix must itself be a release-tag subject), so `ci(release): update pipeline` and
  `docs: changelog` edits remain non-releases and the #431 posture is preserved (#753).
- Leakage: ``agent/context.py::_mask_forward_refs`` (the git-only fallback used when
  ``.vanguarstew_context.json`` is absent) now masks GitHub deep-links and raw commit SHAs
  in README/commit text, matching ``benchmark/leakage.strip_forward_refs`` — completing the
  remaining scope of #283 after #312 added ``#N`` masking only.
- Tooling: ``scripts/compare_eval`` no longer diffs a placeholder ``composite_mean`` of
  ``0.0`` on partitions or multi-repo runs with ``scored_repos: 0`` as if it were a real
  score — the delta is ``None`` instead of a misleading ``+0.600``-style swing, mirroring
  the unscored guard already used by ``benchmark/trend.py`` and ``benchmark/report.py``.
- Leakage / context completeness (`benchmark/github_context.py`): the as-of-T `milestones`
  and `releases` were read from only the first API page, so a repo with more than 100 of
  either silently dropped the rest — which can hide a milestone that was open at T or an
  older release that sets the frozen base version. Both now paginate (bounded by
  `max_list_pages`), matching how issues/PRs already walk their history (#209).
- Leakage: `agent/context.py::_context_from_git` (the fallback context builder used when
  `.vanguarstew_context.json` is absent) now filters tags with `--merged HEAD`, so a tag
  reachable only from an unmerged branch can no longer leak into `releases` as knowable-at-T.
  Mirrors the reachability guard `benchmark/freeze.py::build_context` already applies (#256).
- `agent/philosophy.py::infer_philosophy` now coerces a non-dict LLM response (e.g. a
  top-level JSON array) back to the offline stub, mirroring the guard already used by
  `decider.decide` and `review.review_pr`. Previously a substantive-but-list philosophy
  silently forfeited the offline judge's `philosophy_signal` tiebreaker (#190).
- Benchmark hygiene: `benchmark/taskgen.py::revealed_window` now parses changed-file
  lists from NUL-delimited `git show --name-only -z` output via a reusable
  `benchmark.freeze.parse_path_list` helper, instead of whitespace `.split()`. Filenames
  containing spaces or shell-sensitive characters are no longer corrupted (which would
  mis-attribute file-weighted scoring); first-parent merges still yield an empty file
  list as before. Adds `tests/test_taskgen.py` regression coverage (#137).

## [0.3.0] - 2026-07-03

### Added
- Objective anchor: open-issue **backlog recall** (`benchmark/score.py`) — when frozen
  `open_issues` are knowable at T, `objective_score` reports `backlog_recall`,
  `addressed_issue_numbers`, and `matched_issue_numbers`, scoring whether a plan anticipated
  issues the revealed window actually addressed (title ↔ commit-subject overlap). Also reports
  `addressed_backlog_diagnostics` — the issue number, title, and matched commit subject behind
  each addressed issue — for maintainer-facing inspection; purely additive, doesn't change
  scoring. Git-only runs or an empty backlog degrade gracefully (#44, #135).
- Generalization (M3): **multi-repo replay** — `run_multi_replay` / `scripts/run_eval.py --repos`
  runs several repos and averages each repo's own `composite_mean` into one cross-repo number
  (per-repo results retained; too-small repos skipped), so the agent is scored on breadth rather
  than a single tuned repo (#51).
- Generalization (M3): **leakage-safe repo-set config + loader** (`benchmark/repo_set.py`,
  `benchmark/repo_sets/`) — the replay repo list is a checked-in, strictly-validated JSON config
  (recent/obscure tier, `held_out`, freeze-window hints) instead of a hardcoded array, so the
  curated selection is reviewable and versioned (#55).
- Composite: **file-weighted module recall** now feeds the composite score — the objective anchor
  weights modules by how much of the revealed maintainer effort landed in each, so the blended
  score better reflects where the work actually concentrated (#91).
- Judge integrity: the pairwise judge now defends against LLM **position bias** with dual-order
  consistency — it asks both presentation orders and awards a win only if it survives the swap,
  otherwise a tie. A position-biased judge can no longer earn a spurious win, and per-task
  variance drops. Default on; opt out via `run_replay(dual_order_judge=False)` /
  `--single-order-judge`; the replay result reports `judge_dual_order` (#87).
- Planner queue reconciliation (`agent/planner.py`): a deterministic pass makes the plan honor
  the open-PR queue even when the LLM disregards it — an item that restates an open PR's work
  is down-weighted to a `triage` review item and flagged with `restates_pr`, redundant items
  targeting the same PR are collapsed, and if the plan ignores the queue entirely a review
  item for the top PR is prepended. Keeps the output coherent and de-duplicated regardless of
  model quality (#68).
- Development backend: `tools/codex_llm.py`, an optional `agent.llm.LLM`-compatible LLM backed
  by the local `codex` CLI (ChatGPT / OAuth, e.g. gpt-5.5), for running the benchmark and
  maintenance tooling **without an API key**. Dev/ops only — it is deliberately kept out of the
  scored `agent.solve` path, which still uses only validator-supplied inference per the
  managed-inference contract (`agent/llm.py`).
- Objective scoring: **commit-kind recall** (`benchmark/score.py`) — `objective_score` now
  reports `kind_recall`, `actual_kinds`, and `matched_kinds`, grading whether a plan
  anticipated the *kind* of maintainer work (feat/fix/docs/refactor/…/release) that the
  revealed window actually did, parsed deterministically from Conventional-Commit subjects
  (#41).
- Maintainer-assist mode (`agent/review.py`, `scripts/review_pr.py`): the same agent the
  benchmark scores, applied to a **live** PR — it reads the PR and outputs a maintainer review
  (recommended action, best-fit `mult:*` value tier, scope/tests checks, concerns, advice).
  This is the "how it helps a maintainer" side: real triage/review assistance, not just scoring.
- Composite score: the pairwise judge (trajectory + decision process) and the objective anchor
  (module recall, release/bump correctness) are now blended into a single per-task and mean
  score in [0, 1], with tunable weights (`--w-judge` / `--w-objective`, default 0.6 / 0.4).
- Objective anchor: semver-aware release-bump scoring — when a genuine release appears in the
  revealed window, `objective_score` derives the actual bump level (major/minor/patch) from
  the semver delta between the frozen base version and the released version, and reports
  `bump_actual` / `bump_match` against the agent's predicted `version_bump` (tags with or
  without a leading `v`, and missing-patch/pre-release forms, all parse). The released version
  is read only from genuine release subjects, so a dependency bump can't skew the bump level.

### Fixed
- Planner PR matching (`agent/planner.py`): when several open-PR titles are quoted in a
  plan and one nests inside another (e.g. `Add streaming export` inside `Add streaming
  export docs`), `_matched_pr` now prefers the longest matching title instead of the first
  in queue order, so the more specific PR wins regardless of queue order. Explicit `#N`
  references still take priority (#104).
- Judge robustness (follow-up to #54): the offline substance heuristic keyed only on
  `title`/`theme` *presence*, so a plan stuffed with generic filler titles (`misc`, `updates`,
  `various`, …) could still out-rank a shorter, concrete one. Substance is now a weighted score
  — filler/blank items count for nothing, and each structured action field (`kind`, `files`,
  per-item `rationale`) beyond a real title adds weight — so length/filler never beats
  substance (#70).
- Judge robustness: the offline pairwise stand-in ranked submissions by raw plan **length**,
  so a plan padded with empty-of-substance items could beat a shorter, substantive one. It now
  ranks by the count of items that actually name something (non-empty `title`/`theme`), so
  length alone can't win over substance (#54).
- Objective anchor: `release_signaled`/`release_predicted` no longer fire on an incidental
  version mentioned mid-subject (e.g. `chore(deps): bump lodash to v4.17.21`, `fix crash in
  v1.2.0 parser`). Release detection now requires explicit release wording or a version-tag
  subject, so dependency bumps no longer inflate the release-prediction signal (#57).
- Task generation: `revealed_window` (`benchmark/taskgen.py`) reported **zero changed files**
  for merge commits (a plain `git show` of a clean merge yields an empty combined diff),
  silently depressing module-recall scoring for any repo that merges via PRs (#113). It also
  split file lists on whitespace, corrupting attribution for paths containing spaces (#116).
  Both are fixed by diffing merges against their first parent and splitting on lines instead.
  Regression coverage added in `tests/test_taskgen.py` via a reusable merge-history fixture
  (#117).
- Leakage: frozen milestone `state` is now computed as-of-T from `closed_at` instead of copying
  the milestone's present-day state, so a milestone that existed at T but was closed *after* T
  is no longer leaked into the context as completed (#77).

## [0.2.0] - 2026-07-03

### Added
- M2: robust judge winner parsing — tolerant to truncated/verbose model output (found via
  live verification against a real model).
- M2: leakage — also scrub GitHub release `name` fields in the frozen context.
- M2: maintainer-philosophy inference now uses few-shot examples for steadier, more
  evidence-based output (#33, thanks @real-venus).
- M2: selectable reference baselines for the pairwise judge — a deterministic `heuristic`
  opponent (continue dominant themes + clear the backlog) alongside `empty`, via `--baseline`
  (#34, thanks @real-venus).
- Auto-label workflow: applies organizational **area/type** labels (agent/benchmark/leakage/
  tests/ci/docs; enhancement/bug/refactor/chore) from changed paths and the PR title. Never
  touches `mult:*` value multipliers — those stay maintainer-applied.
- CI split into focused jobs (**lint** / **validate** / **test**), with a new config-validation
  step that parses every workflow YAML and JSON config.
- M2: GitHub issue/PR fetch now paginates back toward the freeze time T (bounded, with an
  `_issues_truncated` flag), so open-at-T reconstruction is complete regardless of how old T
  is — not just the newest page.
- M2: leakage hardening — forward-reference scrubbing (mask `#N` back-references, GitHub
  issue/PR/commit links, and raw SHAs in the frozen context) and recent-window + deterministic
  rotation for freeze-point selection (`--recent-bias`, `--rotation-seed`).
- M2: the pairwise judge now evaluates the **decision process** — the agent's inferred
  maintainer philosophy and reasoning are passed to the judge and weighed alongside
  trajectory/direction match, so when two plans point the same way the sounder reasoning wins.

## [0.1.0] - 2026-07-02

### Added
- M2 (start): objective scoring anchor — a deterministic, structural signal that grades a
  plan against ground truth from the revealed window (which top-level modules actually
  changed; whether a release happened), reported per task alongside the pairwise judge.
- M1: GitHub-API context enrichment — freeze-time snapshots can now include the maintainer's
  real working surface (open issues, open PRs, labels, milestones, releases) reconstructed as
  of time T, with strict "knowable at T" filtering. Enabled with `--enrich`; degrades to
  git-only context when offline.
- M0 scaffold: maintainer agent with a fixed `solve()` entrypoint (philosophy → plan →
  decide → implement) and an OpenAI-compatible managed-inference client with an offline mode.
- Time-travel replay benchmark: freeze a repo at a point in time, generate tasks from git
  history, and score plans with a pairwise LLM judge.
- Open-source project scaffolding: license, contributing guide, code of conduct, security
  policy, issue/PR templates, and CI.

## [0.0.1] - 2026-07-01

- Initial project structure.
