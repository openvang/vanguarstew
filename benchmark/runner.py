"""Orchestrate the time-travel replay: freeze -> run agents -> pairwise judge -> tally.

The agent entrypoint is loaded by file path (as ninja's validator loads `agent.py`), so the
top-level `agent.py` module and the `agent/` package don't collide. The challenger is judged
against a reference opponent (`DEFAULT_BASELINE`, now the deterministic `heuristic` maintainer —
against the old `empty` floor every real challenger won 100%, saturating the judge component at
1.0, #2379); in M2+ this becomes challenger-vs-king.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile

from agent.context import CONTEXT_FILE
from agent.llm import LLM
from benchmark.baselines import DEFAULT_BASELINE, empty_solve, get_baseline
from benchmark.freeze import write_frozen
from benchmark.github_context import enrich_context, open_issues_from_context
from benchmark.judge import build_judge_report, judge_verbose, summarize_judge_orders
from benchmark.leakage import scrub_context
from benchmark.memory import (
    attach_memory_view,
    combine_memory_commitments,
    frozen_context_timestamp,
    memory_commitment,
    verify_memory_view,
)
from benchmark.repo_set import RepoSetError, is_placeholder_source, load_repo_set
from benchmark.score import (
    base_from_releases,
    combine_foresight_breakdowns,
    composite_score,
    foresight_breakdown,
    objective_component,
    objective_score,
    trajectory_overlap,
)
from benchmark.taskgen import generate_tasks

logger = logging.getLogger(__name__)

# Challenger-perspective judge outcome per row (mirrors score._JUDGE_OUTCOME, keyed by the
# runner's decoded winner label): a win is 1.0, a tie 0.5, a loss 0.0.
_JUDGE_COMPONENT = {"challenger": 1.0, "tie": 0.5, "baseline": 0.0}

# Wall-clock cap for cloning a repo-set source. `git clone` reaches out over the network, so
# without a bound a slow, stalled, or unresponsive remote hangs the entire replay indefinitely.
# Fail the source closed (RepoSetError) instead, so the run records a clean per-repo error.
CLONE_TIMEOUT_SECONDS = 300


def load_solve(agent_file: str = "agent.py"):
    if not os.path.isfile(agent_file):
        raise RuntimeError(
            f"agent file {agent_file!r} does not exist or is not a regular file"
        )
    root = os.path.dirname(os.path.abspath(agent_file))
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("vanguarstew_entry", agent_file)
    if spec is None:
        raise RuntimeError(
            f"cannot load agent file {agent_file!r}: unsupported file type or missing loader"
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RuntimeError(
            f"cannot load agent file {agent_file!r}: {exc}"
        ) from exc
    solve = getattr(module, "solve", None)
    if not callable(solve):
        raise RuntimeError(
            f"agent file {agent_file!r} does not define a callable 'solve' entrypoint"
        )
    return solve


# Backwards-compatible alias; opponents now live in benchmark.baselines.
baseline_solve = empty_solve


def _submission(out: dict) -> dict:
    """The judged view of an agent's output: philosophy + plan + reasoning."""
    if not isinstance(out, dict):
        return {"philosophy": None, "plan": None, "rationale": None}
    return {
        "philosophy": out.get("philosophy"),
        "plan": out.get("plan"),
        "rationale": out.get("rationale"),
    }


def _materialize_repo_source(source: str, checkout_root: str | None) -> tuple[str, bool]:
    """Return a local repo path for a repo-set source plus whether it should be cleaned up."""
    if is_placeholder_source(source):
        raise RepoSetError(
            f"repo-set source {source!r} is a placeholder (OWNER/...); "
            "copy the example config and replace placeholder sources with vetted repos"
        )
    if os.path.isdir(source):
        return source, False
    if checkout_root is None:
        raise RepoSetError(f"repo-set source not found locally: {source}")
    dest = os.path.join(checkout_root, f"repo_{len(os.listdir(checkout_root))}")
    try:
        # `--` ends option parsing so a source beginning with `-` is treated as a repo, never a
        # git flag; `timeout` bounds a network clone so a stalled remote can't hang the replay.
        subprocess.run(["git", "clone", "-q", "--", source, dest], check=True,
                       capture_output=True, text=True, timeout=CLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RepoSetError(
            f"timed out cloning repo-set source {source!r} after {CLONE_TIMEOUT_SECONDS}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RepoSetError(f"failed to clone repo-set source {source!r}: {exc.stderr.strip()}") from exc
    return dest, True


def run_replay(repo_path, agent_file="agent.py", n_tasks=3, horizon=5,
               model=None, api_base=None, api_key=None, work_dir=None, seed=0,
               enrich_github=False, github_token=None,
               recent_bias=False, rotation_seed=None, baseline=DEFAULT_BASELINE,
               w_judge=0.6, w_objective=0.4, dual_order_judge=True,
               min_history=10, after=None, before=None, horizon_days=None,
               solve_fn=None, memory_provider=None, tasks_override=None) -> dict:
    """Run one replay using a local agent file or a trusted caller-supplied adapter.

    ``solve_fn`` is the isolation seam used by deployment: the trusted evaluator can keep
    task generation, judging, and secrets outside an untrusted candidate sandbox while the
    adapter exchanges only the frozen task and candidate result.  The legacy ``agent_file``
    path remains the local-development default.
    """
    if solve_fn is None:
        solve = load_solve(agent_file)
    elif callable(solve_fn):
        solve = solve_fn
    else:
        raise TypeError("solve_fn must be callable")
    if memory_provider is not None and not callable(memory_provider):
        raise TypeError("memory_provider must be callable")
    opponent = get_baseline(baseline)
    llm = LLM(model=model, api_base=api_base, api_key=api_key)
    if tasks_override is None:
        tasks = generate_tasks(
            repo_path, n_tasks, horizon, min_history=min_history,
            recent_bias=recent_bias, rotation_seed=rotation_seed, after=after, before=before,
            horizon_days=horizon_days)
    else:
        if not isinstance(tasks_override, list) or not tasks_override:
            raise TypeError("tasks_override must be a non-empty controller task list")
        tasks = []
        for task in tasks_override:
            if (
                not isinstance(task, dict)
                or not isinstance(task.get("freeze_commit"), str)
                or not task["freeze_commit"]
                or not isinstance(task.get("revealed"), list)
            ):
                raise TypeError("tasks_override contains an invalid controller task")
            tasks.append(dict(task))
    if not tasks:
        return {"error": "no usable tasks (repo too small for horizon/min_history)", "tasks": 0}

    rng = random.Random(seed)
    tally = {"challenger": 0, "baseline": 0, "tie": 0}
    rows = []
    memory_commitments = []
    base = work_dir or tempfile.mkdtemp(prefix="vanguarstew_work_")
    try:
        for k, task in enumerate(tasks):
            dest = os.path.join(base, f"task_{k}")
            if os.path.exists(dest):
                shutil.rmtree(dest)
            ctx = write_frozen(repo_path, task["freeze_commit"], dest)
            if enrich_github:
                ctx = scrub_context(enrich_context(ctx, repo_path, token=github_token))
                with open(os.path.join(dest, CONTEXT_FILE), "w", encoding="utf-8") as f:
                    json.dump(ctx, f, indent=1)
            # A time-horizon run asks for the same JUDGMENT over a period rather than a count of
            # actions — "what lands in the next N days" is the question the ground truth answers.
            request = (f"plan the maintainer actions for the next {horizon_days} days"
                       if horizon_days else f"plan the next {horizon} maintainer actions")
            solve_kwargs = {
                "repo_path": dest,
                "request": request,
                "model": model or "validator-managed-model",
                "api_base": api_base or "",
                "api_key": api_key or "offline",
                "n": horizon,
            }
            memory_view = None
            if memory_provider is not None:
                memory_view = memory_provider(
                    task=task, context=ctx, request=request, task_index=k,
                )
                if not verify_memory_view(memory_view):
                    raise RuntimeError("memory_provider returned an invalid memory view")
                freeze_timestamp = frozen_context_timestamp(ctx)
                if (
                    memory_view["mode"] != "benchmark"
                    or memory_view["boundary"]["frozen_at"] != freeze_timestamp
                    or memory_view["boundary"]["public_only"] is not True
                    or any(
                        item["observed_at"] > freeze_timestamp
                        or item["created_at"] > freeze_timestamp
                        for item in memory_view["items"]
                    )
                ):
                    raise RuntimeError("memory_provider crossed a benchmark memory boundary")
                # Keep solve()'s miner-facing signature fixed.  The trusted controller places
                # only the bounded view into the frozen read-only context the candidate receives.
                with open(os.path.join(dest, CONTEXT_FILE), "w", encoding="utf-8") as handle:
                    json.dump(attach_memory_view(ctx, memory_view), handle, indent=1)
            challenger = solve(**solve_kwargs)
            if not isinstance(challenger, dict):
                challenger = {}  # a miner agent may return a non-dict; degrade to empty, don't crash
            baseline_out = opponent(dest, request, context=ctx, n=horizon)
            winner, judge_order = judge_verbose(
                ctx, _submission(challenger), _submission(baseline_out),
                task["revealed"], llm, rng, dual_order=dual_order_judge)
            who = {"A": "challenger", "B": "baseline", "tie": "tie"}[winner]
            tally[who] += 1
            obj = objective_score(
                challenger.get("plan"), task["revealed"],
                version_bump=challenger.get("version_bump"),
                base_version=base_from_releases(ctx.get("releases")),
                open_issues=open_issues_from_context(ctx),
            )
            row = {
                "task": k,
                "freeze": task["freeze_commit"][:10],
                "winner": who,
                "judge_order": judge_order,
                "overlap": trajectory_overlap(challenger.get("plan"), task["revealed"]),
                "objective": obj,
                "composite": composite_score(winner, obj, w_judge, w_objective),
            }
            if memory_view is not None:
                # Only commitments enter the replay artifact.  The store, snapshot, and raw
                # recalled evidence stay with the trusted controller/task sandbox.
                row["memory_commitment"] = memory_commitment(memory_view)
                memory_commitments.append(row["memory_commitment"])
            rows.append(row)
    finally:
        if not work_dir:
            shutil.rmtree(base, ignore_errors=True)

    # The single-repo composite output contract: the mean blended score, plus the two
    # component means it blends (judge outcome + objective anchor) so the number is
    # inspectable and the multi-repo aggregate has explicit parts to average.
    composites = [r["composite"] for r in rows]
    judge_parts = [_JUDGE_COMPONENT[r["winner"]] for r in rows]
    objective_parts = [objective_component(r["objective"]) for r in rows]
    judge_order_stats = summarize_judge_orders(r.get("judge_order") for r in rows)
    result = {
        "tasks": len(tasks),
        "baseline": baseline,
        "tally": tally,
        "decisive_margin": tally["challenger"] - tally["baseline"],
        "composite_mean": round(sum(composites) / len(composites), 3) if composites else 0.0,
        "composite_parts": {
            "judge_mean": round(sum(judge_parts) / len(judge_parts), 3) if judge_parts else 0.0,
            "objective_mean": (
                round(sum(objective_parts) / len(objective_parts), 3) if objective_parts else 0.0
            ),
        },
        "foresight": foresight_breakdown([r["objective"] for r in rows]),
        "weights": {"judge": w_judge, "objective": w_objective},
        "rows": rows,
        "judge_order_stats": judge_order_stats,
        "judge_report": build_judge_report(tally, judge_order_stats),
        "offline": llm.offline,
        "github_enriched": enrich_github,
        "judge_dual_order": dual_order_judge,
    }
    if memory_commitments:
        # Per-task rows retain their matching view commitment for local audit.  The top-level
        # artifact additionally has an order-independent commitment that can be TEE-bound.
        result["memory_commitment"] = combine_memory_commitments(memory_commitments)
    return result


# A small default grid of (w_judge, w_objective) blends for `weight_sweep`. Spans a
# judge-heavy to objective-heavy range around the production default (0.6 / 0.4).
WEIGHT_SWEEP_GRID = ((0.2, 0.8), (0.4, 0.6), (0.5, 0.5), (0.6, 0.4), (0.8, 0.2))


def _rows_list(rows, field: str = "rows") -> list:
    """Return ``rows`` when it is a list; otherwise treat as no per-task replay rows."""
    if isinstance(rows, list):
        return rows
    if rows is not None:
        logger.warning(
            "runner: %s is %s, not a list; treating as empty",
            field,
            type(rows).__name__,
        )
    return []


def _freeze_window_dict(freeze_window, field: str = "freeze_window") -> dict:
    """Return ``freeze_window`` when it is a dict; otherwise ignore replay hints."""
    if isinstance(freeze_window, dict):
        return freeze_window
    if freeze_window is not None:
        logger.warning(
            "runner: %s is %s, not a dict; ignoring freeze_window hints",
            field,
            type(freeze_window).__name__,
        )
    return {}


def _sweep_rows(rows) -> list:
    """Return ``rows`` when it is a list; otherwise treat as no scored tasks.

    A truthy non-list must not reach ``for r in rows`` or malformed replay output aborts
    the weight-sweep helper (#561).
    """
    return _rows_list(rows, "weight_sweep rows")


def weight_sweep(rows, grid=WEIGHT_SWEEP_GRID) -> list:
    """Recompute `composite_mean` across a grid of judge/objective blend weights (#53).

    Takes the already-scored per-task ``rows`` from :func:`run_replay` (each carrying a
    ``winner`` and an ``objective``) and re-blends them at each ``(w_judge, w_objective)`` pair,
    so the blend can be tuned without re-running the expensive replay. Only the weights vary;
    each task's judge outcome and objective anchor are fixed.

    The per-task blend mirrors :func:`benchmark.score.composite_score` exactly (weights are
    normalized, each task's composite is rounded to 3 places, then averaged), so sweeping at a
    run's own weights reproduces that run's reported ``composite_mean``.

    Returns a list of ``{"w_judge", "w_objective", "composite_mean"}`` in grid order.
    """
    scored = []
    for r in _sweep_rows(rows):
        if not isinstance(r, dict):
            if r is not None:
                logger.warning(
                    "weight_sweep: skipping a non-dict row (%s: %r)",
                    type(r).__name__,
                    r,
                )
            continue
        winner = r.get("winner")
        if winner in _JUDGE_COMPONENT:
            scored.append(
                (_JUDGE_COMPONENT[winner], objective_component(r.get("objective") or {}))
            )
    sweep = []
    for w_judge, w_objective in grid:
        total = (w_judge + w_objective) or 1.0
        per_task = [round((w_judge * j + w_objective * o) / total, 3) for j, o in scored]
        mean = round(sum(per_task) / len(per_task), 3) if per_task else 0.0
        sweep.append({"w_judge": w_judge, "w_objective": w_objective, "composite_mean": mean})
    return sweep


def run_multi_replay(repos=None, repo_set=None, held_out=False, repo_set_partition=None,
                     **kwargs) -> dict:
    """Replay several repos and aggregate their composites (proposal §4 / M3 generalization).

    Runs `run_replay` once per repo — preserving every per-repo result — and averages each
    repo's own `composite_mean` into an overall cross-repo `composite_mean`. This is the
    generalization signal: how the agent scores *across* codebases, not just within one.

    Only repos that actually produced tasks are aggregated — gated on `tasks > 0`, so a short
    repo (which returns `tasks == 0` and no real composite) can't dilute the mean or be
    miscounted as scored.

    Deterministic given a fixed `seed` (passed through to each run and the judge's RNG).
    A repo that fails to score — too small to yield tasks, or unusable (missing path, not a git
    repo, freeze failure) — is kept in `per_repo` with its error and counted in `skipped`, so one
    bad repo can neither abort the batch nor dilute the aggregate.
    """
    if (repos is None) == (repo_set is None):
        raise ValueError("pass exactly one of 'repos' or 'repo_set'")

    repo_set_meta = None
    selected = []
    checkout_root = None
    if repo_set is not None:
        rs = load_repo_set(repo_set)
        if repo_set_partition:
            entries = rs.partition(repo_set_partition)
            selection = repo_set_partition
        elif held_out:
            entries = rs.held_out()
            selection = "held_out"
        else:
            entries = rs.tuned()
            selection = "tuned"
        if not entries:
            raise RepoSetError(f"repo set {repo_set!r} has no {selection} repos to replay")
        repo_set_meta = {
            "path": repo_set,
            "name": rs.name,
            "selection": selection,
        }
        checkout_root = tempfile.mkdtemp(prefix="vanguarstew_repo_set_")
        try:
            for entry in entries:
                repo_path, cleanup = _materialize_repo_source(entry.source, checkout_root)
                selected.append({
                    "repo": entry.source,
                    "repo_name": entry.name,
                    "tier": entry.tier,
                    "held_out": entry.held_out,
                    "freeze_window": dict(entry.freeze_window),
                    "repo_path": repo_path,
                    "cleanup": cleanup,
                })
        except BaseException:
            # Materialization (placeholder rejection, clone failure/timeout, missing source)
            # raises before the replay loop's try/finally is entered, so clean up the checkout
            # root here — otherwise a failed setup leaks the temp dir and any repos already
            # cloned into it.
            shutil.rmtree(checkout_root, ignore_errors=True)
            raise
    else:
        selected = [{"repo": repo, "repo_path": repo, "cleanup": False} for repo in repos]

    per_repo = []
    composites = []
    judge_parts = []
    objective_parts = []
    foresight_parts = []
    judge_orders = []
    tally = {"challenger": 0, "baseline": 0, "tie": 0}
    try:
        for repo in selected:
            meta = {k: v for k, v in repo.items() if k not in ("repo_path", "cleanup")}
            try:
                repo_kwargs = dict(kwargs)
                for key, value in _freeze_window_dict(repo.get("freeze_window")).items():
                    repo_kwargs[key] = value
                res = run_replay(repo["repo_path"], **repo_kwargs)
            except RuntimeError as exc:
                # A repo-level failure (missing path, not a git repo, freeze error) surfaces from
                # freeze._git as RuntimeError; record it like a zero-task repo so the batch keeps
                # going and the tasks > 0 gate below leaves it out of the mean.
                logger.warning("runner: replay failed for %s: %s",
                               repo.get("repo") or repo.get("repo_path"), exc)
                res = {"error": str(exc), "tasks": 0}
            per_repo.append({**meta, **res})
            for outcome in tally:
                tally[outcome] += int((res.get("tally") or {}).get(outcome, 0))
            if res.get("tasks", 0) > 0:
                composites.append(res["composite_mean"])
                parts = res.get("composite_parts", {})
                judge_parts.append(parts.get("judge_mean", 0.0))
                objective_parts.append(parts.get("objective_mean", 0.0))
                foresight_parts.append(res.get("foresight"))
                judge_orders.extend(
                    r.get("judge_order")
                    for r in _rows_list(res.get("rows"), "replay rows")
                )
    finally:
        if checkout_root:
            shutil.rmtree(checkout_root, ignore_errors=True)

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    judge_order_stats = summarize_judge_orders(judge_orders)
    result = {
        "repos": len(per_repo),
        "scored_repos": len(composites),
        "skipped": len(per_repo) - len(composites),
        "composite_mean": _mean(composites),
        "composite_parts": {
            "judge_mean": _mean(judge_parts),
            "objective_mean": _mean(objective_parts),
        },
        "foresight": combine_foresight_breakdowns(foresight_parts),
        "judge_order_stats": judge_order_stats,
        "judge_report": build_judge_report(tally, judge_order_stats),
        "per_repo": per_repo,
    }
    if repo_set_meta is not None:
        result["repo_set"] = repo_set_meta
    return result


def run_generalization_report(repo_set, **kwargs) -> dict:
    """Replay a repo set's tuned and held-out slices and report the generalization gap (M3).

    `run_multi_replay` scores one partition at a time; this runs both the `tuned` and
    `held_out` partitions and contrasts them in a single call. `generalization_gap` is the
    tuned composite mean minus the held-out composite mean — positive means the agent does
    worse on repos it was never tuned against. That gap is the M3 acceptance signal: held-out
    performance should not collapse relative to tuned. It is None unless BOTH partitions
    actually scored a repo, so it is never reported from a single side; a partition the config
    does not define (no tuned, or no held-out, repos) is recorded with its error rather than
    aborting the whole report.

    Deterministic given a fixed `seed` (threaded through both partition runs).
    """
    def _partition(which):
        try:
            return run_multi_replay(repo_set=repo_set, repo_set_partition=which, **kwargs)
        except RepoSetError as exc:
            return {"error": str(exc), "scored_repos": 0, "composite_mean": 0.0}

    tuned = _partition("tuned")
    held_out = _partition("held_out")

    gap = None
    if tuned.get("scored_repos") and held_out.get("scored_repos"):
        gap = round(tuned["composite_mean"] - held_out["composite_mean"], 3)

    return {
        "repo_set": repo_set,
        "tuned": tuned,
        "held_out": held_out,
        "generalization_gap": gap,
    }
