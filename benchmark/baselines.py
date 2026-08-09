"""Reference baseline maintainers — the opponents a challenger is judged against.

The pairwise judge only means something relative to an opponent. Four are provided:

- ``empty``     — proposes nothing concrete. The floor: any real plan should beat it.
- ``heuristic`` — a deterministic, LLM-free maintainer that extrapolates the repo's own
                  recent behavior: it addresses the open-issue backlog and continues the
                  themes that dominate recent commit history. A stronger, harder-to-beat
                  bar than ``empty`` — a challenger has to actually out-reason "keep doing
                  what this repo has been doing."
- ``queue_first`` — like ``heuristic`` but clears the open-PR **review queue** first, mirroring
                  the planner's own guidance that a strong maintainer clears or explicitly
                  schedules the review queue before unrelated greenfield work. On a repo with a
                  live queue it is the hardest bar; with no queue it degrades to exactly
                  ``heuristic``, so it is never a weaker opponent.
- ``stability_first`` — like ``heuristic`` but **reorders** the same candidate actions by a
                  fixed stability priority: bugfix/refactor before release, release before
                  feature/docs, triage last. Models a conservative maintainer who stabilizes
                  before shipping greenfield work.

Each baseline exposes the same shape as the agent's ``solve`` output (philosophy + plan +
rationale), so it can flow through ``_submission`` and the judge unchanged. Select one by
name via :func:`get_baseline`; the runner exposes this as ``--baseline``.
"""

from __future__ import annotations

import logging
from collections import Counter

from agent.context import load_context
from benchmark.score import commit_kind, is_release_subject

logger = logging.getLogger(__name__)

# Map normalized commit_kind values onto the planner's baseline vocabulary.
_COMMIT_KIND_TO_BASELINE = {
    "feat": "feature",
    "fix": "bugfix",
    "docs": "docs",
    "refactor": "refactor",
    "perf": "refactor",
    "release": "release",
    "chore": "dep",
    "ci": "refactor",
    "test": "refactor",
    "build": "refactor",
    "style": "refactor",
    "revert": "bugfix",
}

# Map a free-text title/subject to one of the planner's kinds. Order matters: earlier
# entries win, so dep is checked before the broader "feature" verbs. Release detection
# itself is NOT here: it defers to score.is_release_subject (the canonical helper) so
# baseline classification can't drift from scoring semantics.
_KIND_KEYWORDS = (
    ("dep", ("bump", "dependency", "dependencies", "deps", "upgrade", "dependabot")),
    ("docs", ("doc", "docs", "readme", "document", "guide", "example", "comment")),
    ("bugfix", ("fix", "bug", "patch", "regression", "hotfix", "error", "crash")),
    ("refactor", ("refactor", "cleanup", "clean up", "simplify", "rename", "restructure")),
    ("feature", ("add", "feature", "support", "implement", "introduce", "enable", "new")),
    ("test", ("test", "coverage", "ci")),
)
# planner's allowed kinds; anything else collapses to "triage"
_ALLOWED = {"feature", "bugfix", "refactor", "docs", "release", "dep", "triage"}


def _issue_title(issue) -> str:
    """Return a stripped issue title when it is a string; else empty."""
    if not isinstance(issue, dict):
        return ""
    title = issue.get("title")
    return title.strip() if isinstance(title, str) else ""


def _baseline_list(items, field: str) -> list:
    """Return ``items`` when it is a list; otherwise treat as no entries.

    A truthy non-list must not reach ``for item in items`` or malformed frozen context
    aborts the heuristic baseline replay path (#515).
    """
    if isinstance(items, list):
        return items
    if items is not None:
        logger.warning(
            "heuristic baseline: %s is %s, not a list; treating as empty",
            field,
            type(items).__name__,
        )
    return []


def _safe_backlog(context, key: str) -> list:
    """Return the frozen ``open_issues``/``open_prs`` list, or ``[]`` when it must not be trusted.

    Fail-closed on ``_issues_truncated is True`` (#722/#957). ``fetch_context_at`` zeroes a
    truncated backlog at the source, but a frozen ``.vanguarstew_context.json`` can still carry a
    partial ``open_issues``/``open_prs`` alongside the flag — the "older frozen artifacts that
    still carry a partial backlog" case that ``context_for_agent`` (defense in depth), the
    planner's ``_safe_prs`` (#975), and ``open_issues_from_context`` (scoring) all already guard.
    The reference opponent is the last consumer of the flag; without this guard it would plan
    "review PR" / "address issue" items for a partial backlog the challenger's context zeroes out,
    and the pairwise judge would score the challenger worse for backlog/queue work it was
    structurally denied.

    A non-dict ``context`` (``None`` or any malformed value) yields ``[]`` rather than raising,
    matching ``_baseline_list``'s fail-soft posture and the ``isinstance`` guard the rest of this
    module applies before reading a context."""
    if not isinstance(context, dict):
        return []
    if context.get("_issues_truncated") is True:
        return []
    return _baseline_list(context.get(key), key)


def _commit_subject(commit) -> str:
    """Return a commit's ``subject`` when the entry is a dict with a string subject; else empty.

    ``recent_commits`` entries come from the (unvalidated) frozen context; a malformed entry
    that isn't a dict — or whose ``subject`` isn't a string — must not crash the heuristic
    baseline. A non-dict entry is logged and skipped; a non-string subject resolves to "" so it
    never reaches the kind heuristic (which would raise on, e.g., an ``int``).
    """
    if not isinstance(commit, dict):
        logger.warning(
            "heuristic baseline: skipping a non-dict recent_commits entry (%s: %r)",
            type(commit).__name__, commit,
        )
        return ""
    subject = commit.get("subject", "")
    return subject if isinstance(subject, str) else ""


def _infer_kind(text: str) -> str:
    if is_release_subject(text):
        return "release"
    ck = commit_kind(text)
    if ck:
        return _COMMIT_KIND_TO_BASELINE.get(ck, "triage")
    low = (text or "").lower()
    for kind, needles in _KIND_KEYWORDS:
        if any(n in low for n in needles):
            # The planner has no "test" kind; CI/test hardening is infra momentum, not triage.
            if kind == "test":
                return "refactor"
            return kind if kind in _ALLOWED else "triage"
    return "triage"


def _commit_kinds(context: dict) -> Counter:
    return Counter(
        _infer_kind(_commit_subject(c))
        for c in _baseline_list(context.get("recent_commits"), "recent_commits")
    )


def heuristic_philosophy(context: dict) -> dict:
    if not isinstance(context, dict):
        context = {}
    kinds = _commit_kinds(context)
    dominant = kinds.most_common(1)[0][0] if kinds else "triage"
    n_issues = len(_safe_backlog(context, "open_issues"))
    return {
        "summary": f"Recent activity is dominated by {dominant} work; "
                   f"{n_issues} open issue(s) await triage.",
        "values": [k for k, _ in kinds.most_common(3)] or ["triage"],
        "merge_bar": "inferred from recent commit patterns (no explicit signal)",
        "direction": f"continue {dominant}-oriented work and clear the issue backlog",
        "evidence": [
            _commit_subject(c)
            for c in _baseline_list(context.get("recent_commits"), "recent_commits")[:5]
        ],
    }


def heuristic_plan(context: dict, n: int = 5) -> list:
    """Extrapolate recent behavior: address open issues, then continue dominant themes."""
    if not isinstance(context, dict):
        context = {}
    items = []

    # 1. The backlog the maintainer can see right now.
    for issue in _safe_backlog(context, "open_issues"):
        title = _issue_title(issue)
        if not title:
            continue
        items.append({
            "title": f"Address issue: {title}",
            "kind": _infer_kind(title),
            "rationale": "open issue awaiting maintainer action",
            "theme": "issue backlog",
        })

    # 2. Continue whatever the recent history has been about, in frequency order.
    for kind, count in _commit_kinds(context).most_common():
        items.append({
            "title": f"Continue {kind} work",
            "kind": kind,
            "rationale": f"recent history is dominated by {kind} changes ({count} recent)",
            "theme": f"{kind} momentum",
        })

    # 3. If the repo has been cutting releases, expect another.
    if any(
        _infer_kind(_commit_subject(c)) == "release"
        for c in _baseline_list(context.get("recent_commits"), "recent_commits")
    ):
        items.append({
            "title": "Prepare the next release",
            "kind": "release",
            "rationale": "recent history shows a release cadence",
            "theme": "release cadence",
        })

    return items[:n]


def _pr_title(pr) -> str:
    """A stripped open-PR title when the entry is a dict with a string title, else empty.

    ``open_prs`` entries come from the (unvalidated) frozen context; a malformed entry that
    isn't a dict, or whose ``title`` isn't a string, is skipped rather than crashing the
    baseline (mirrors :func:`_issue_title`).
    """
    if not isinstance(pr, dict):
        return ""
    title = pr.get("title")
    return title.strip() if isinstance(title, str) else ""


def _review_queue_items(context: dict, limit: int) -> list:
    """Plan items that clear the open-PR review queue, in the order the queue is given.

    A strong maintainer clears (or explicitly schedules) the open review queue before starting
    unrelated greenfield work — the same guidance the planner's system prompt encodes. Each open
    PR with a usable title becomes one concrete triage item, capped at ``limit``. Malformed or
    titleless PR entries are skipped.
    """
    if not isinstance(context, dict):
        context = {}
    items = []
    for pr in _safe_backlog(context, "open_prs"):
        title = _pr_title(pr)
        if not title:
            continue
        number = pr.get("number")
        ref = f" (#{number})" if isinstance(number, int) and not isinstance(number, bool) else ""
        items.append({
            "title": f"Review and merge PR: {title}{ref}",
            "kind": "triage",
            "rationale": "open pull request awaiting review; clear the queue before greenfield work",
            "theme": "PR review queue",
        })
        if limit is not None and len(items) >= limit:
            break
    return items


# Stability-first tier: lower rank sorts earlier. A stable sort preserves heuristic order
# within each tier so this is a pure reprioritization of the same action set.
_STABILITY_KIND_RANK = {
    "bugfix": 0,
    "refactor": 0,
    "release": 1,
    "feature": 2,
    "docs": 2,
    "dep": 2,
    "triage": 3,
}


def _stability_rank(kind: str) -> int:
    return _STABILITY_KIND_RANK.get(kind, 3)


def stability_first_plan(context: dict, n: int = 5) -> list:
    """Reprioritize the heuristic candidate set: stabilize before greenfield.

    Takes the same actions :func:`heuristic_plan` would propose for ``n``, then stable-sorts
    them: bugfix/refactor first, release after stabilization but before features/docs, triage
    last. Nothing is added or dropped — only the order changes.
    """
    items = heuristic_plan(context, n)
    return sorted(items, key=lambda item: _stability_rank(item.get("kind", "triage")))


def queue_first_plan(context: dict, n: int = 5) -> list:
    """Clear the open-PR review queue first, then fall back to the heuristic backlog plan.

    Fills up to ``n`` items: review items for the open-PR queue, then — only if the queue leaves
    room — the ordinary :func:`heuristic_plan` (backlog issues, recent-theme momentum, release
    cadence). When the queue already fills the horizon, only review items are returned; when the
    queue is empty this is exactly ``heuristic_plan``.
    """
    reviews = _review_queue_items(context, n)
    if len(reviews) >= n:
        return reviews[:n]
    return reviews + heuristic_plan(context, n - len(reviews))


def empty_solve(repo_path=None, request="", context=None, n=5, **_kw) -> dict:
    """A naive maintainer that proposes nothing concrete — the bar to beat."""
    return {"plan": [], "philosophy": {}, "action": "plan", "rationale": "baseline"}


def queue_first_solve(repo_path=None, request="", context=None, n=5, **_kw) -> dict:
    """A reference maintainer that clears the open-PR review queue before greenfield work.

    On a repo with a live review queue this is a stronger, more realistic opponent than
    ``heuristic``: it mirrors the planner's own guidance that a strong maintainer clears or
    schedules the queue first. With an empty queue it degrades to exactly the ``heuristic``
    backlog plan, so it is never a weaker bar than ``heuristic`` for lack of a queue.
    """
    ctx = context if context is not None else load_context(repo_path)
    plan = queue_first_plan(ctx, n)
    n_prs = sum(
        1 for pr in _safe_backlog(ctx, "open_prs") if _pr_title(pr)
    )
    return {
        "philosophy": heuristic_philosophy(ctx),
        "plan": plan,
        "action": "plan",
        "rationale": (
            f"queue-first baseline: clear {n_prs} open PR(s) in the review queue, "
            "then continue the dominant recent themes"
        ),
    }


def heuristic_solve(repo_path=None, request="", context=None, n=5, **_kw) -> dict:
    """Deterministic reference maintainer derived from the repo's own recent patterns."""
    ctx = context if context is not None else load_context(repo_path)
    plan = heuristic_plan(ctx, n)
    n_issues = len(_safe_backlog(ctx, "open_issues"))
    return {
        "philosophy": heuristic_philosophy(ctx),
        "plan": plan,
        "action": "plan",
        "rationale": (
            "heuristic baseline: extrapolate the dominant recent themes and address "
            f"{n_issues} open issue(s)"
        ),
    }


def stability_first_solve(repo_path=None, request="", context=None, n=5, **_kw) -> dict:
    """A reference maintainer that stabilizes (bugfix/refactor) before greenfield work.

    Proposes the same concrete actions as ``heuristic`` but reorders them by a fixed
    stability priority so bugfix/refactor precede feature/docs and release sits after
    stabilization but before features.
    """
    ctx = context if context is not None else load_context(repo_path)
    plan = stability_first_plan(ctx, n)
    n_issues = len(_safe_backlog(ctx, "open_issues"))
    return {
        "philosophy": heuristic_philosophy(ctx),
        "plan": plan,
        "action": "plan",
        "rationale": (
            f"stability-first baseline: stabilize before greenfield across "
            f"{n_issues} open issue(s) and recent-theme momentum"
        ),
    }


BASELINES = {
    "empty": empty_solve,
    "heuristic": heuristic_solve,
    "queue_first": queue_first_solve,
    "stability_first": stability_first_solve,
}
# The default opponent every challenger is judged against. It is DELIBERATELY not ``empty``:
# against the empty floor any non-trivial challenger wins every task, so the pairwise judge
# component (``_JUDGE_COMPONENT[challenger] == 1.0``) saturates at 1.0 and stops discriminating —
# it adds nothing on top of the objective anchor, and inflates ``composite_mean`` toward the
# ceiling (#2379). ``heuristic`` — a deterministic, LLM-free maintainer that extrapolates the
# repo's own recent behavior — is a real bar: a challenger has to actually out-reason "keep doing
# what this repo has been doing" to win, so ``judge_mean`` can move. ``empty`` stays available via
# ``--baseline empty`` as the explicit floor for calibration.
DEFAULT_BASELINE = "heuristic"


def get_baseline(name: str):
    """Resolve a baseline by name, or raise ValueError listing the valid choices."""
    try:
        return BASELINES[name]
    except KeyError:
        raise ValueError(
            f"unknown baseline {name!r}; choose from {sorted(BASELINES)}"
        ) from None
