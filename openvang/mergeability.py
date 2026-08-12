"""Mergeability verdict for an external-repo PR that has no benchmark delta (#2385, Stage 1).

On vanguarstew's own repo, "should I merge this PR?" reduces to "did this change to the agent raise
the benchmark composite?" (``scripts/score_pr_delta``). An arbitrary PR on an external product repo
produces no baseline-agent/candidate-agent artifact pair, so that apparatus has nothing to consume.
This module defines the non-self-referential verdict that stands in its place.

It composes three independent signals with an **anti-Goodhart floor** — the same shape as
``score_pr_delta``'s Pareto floor: it blocks on ANY failing signal and never averages a bad signal
away. A strong correctness result can never buy back a domain-critical path.

1. **Correctness gate (hard, fail-closed).** The green/red correctness-evidence bundle from #2383
   (the diff-correctness harness). Anything other than an explicit ``green`` — red, error, or
   **absent** — blocks. This module does not run tests; it only reads the bundle's status. Until
   #2383 ships, the bundle is absent and every external PR blocks, which is the correct default.
2. **Domain veto (absolute).** ``openvang.domain_guardrail.classify`` over the changed paths. A
   critical match forces a block that **no other signal can override** (``absolute_veto``). This is
   the wired, shipped input today.
3. **Qualitative axes (judge).** Scope/readability/safety via the pairwise judge — consulted ONLY
   when ``judge_trusted`` is set. Per #2379 the judge saturates at ``judge_mean == 1.0`` and cannot
   discriminate, so it defaults untrusted: it can never by itself earn a merge, and it is not used
   to block until it discriminates (challenger-vs-king, #2382).

The verdict is advisory and keyed to ``{repo, base_sha, head_sha, diff_digest}`` so #2386 can bind
and publish it. This module performs no GitHub write and executes nothing. Wiring it into
``agent/review.py`` (replacing its ungrounded ``value_label``) and publishing the verdict object are
follow-ups on #2385.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from openvang.domain_guardrail import DomainVerdict, classify

MERGEABILITY_VERSION = "mergeability-v1"

# Mirrors scripts.score_pr_delta.DEFAULT_NOISE_FLOOR: a judge-axis move at or below this is noise.
DEFAULT_NOISE_FLOOR = 0.01

# The only correctness status that clears the hard gate.
_GREEN = "green"


@dataclass(frozen=True)
class MergeabilityVerdict:
    """A composed, advisory verdict for an external-repo PR, keyed to its identity."""

    repo: str
    base_sha: str
    head_sha: str
    diff_digest: str
    decision: str                    # "allow" | "block" (advisory only)
    blocks_merge: bool
    blocked_by: tuple[str, ...]      # every failing signal, never collapsed to one
    absolute_veto: bool              # a domain-critical match no score can override
    domain: dict[str, object]        # the DomainVerdict as_dict()
    correctness_status: str          # "green" | "red" | "error" | "absent" | ...
    judge_considered: bool           # whether the judge was trusted enough to consult
    value_label: str | None          # advisory; None whenever blocked
    rationale: str
    version: str = MERGEABILITY_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff_digest": self.diff_digest,
            "decision": self.decision,
            "blocks_merge": self.blocks_merge,
            "blocked_by": list(self.blocked_by),
            "absolute_veto": self.absolute_veto,
            "domain": self.domain,
            "correctness_status": self.correctness_status,
            "judge_considered": self.judge_considered,
            "value_label": self.value_label,
            "rationale": self.rationale,
            "version": self.version,
        }


def _identity_ok(*values) -> bool:
    return all(isinstance(v, str) and v.strip() for v in values)


def _correctness_status(correctness) -> str:
    """The correctness bundle's status, or ``"absent"`` when no usable bundle is supplied.

    Fail-closed: anything that is not a mapping carrying an explicit string ``status`` reads as
    ``absent`` (which blocks), never as green.
    """
    if not isinstance(correctness, Mapping):
        return "absent"
    status = correctness.get("status")
    if not isinstance(status, str) or not status.strip():
        return "absent"
    return status.strip().lower()


def _judge_regressed(judge, noise_floor: float) -> bool:
    """True when a trusted judge reports any qualitative axis regressed past the noise floor.

    Accepts ``{"axes": {name: delta}}`` and/or an explicit ``{"regressed": bool}``. Fail-closed: a
    malformed/unreadable trusted-judge payload counts as regressed rather than silently passing.
    """
    if not isinstance(judge, Mapping):
        return True
    if judge.get("regressed") is True:
        return True
    axes = judge.get("axes")
    if axes is None:
        return False
    if not isinstance(axes, Mapping):
        return True
    for delta in axes.values():
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            return True
        if delta < -noise_floor:
            return True
    return False


def assess_mergeability(
    *,
    repo: str,
    base_sha: str,
    head_sha: str,
    diff_digest: str,
    changed_paths,
    correctness=None,
    judge=None,
    judge_trusted: bool = False,
    extra_domains=None,
    noise_floor: float = DEFAULT_NOISE_FLOOR,
) -> MergeabilityVerdict:
    """Compose the mergeability verdict. Fail-closed: block unless every gate clears."""
    domain: DomainVerdict = classify(changed_paths, extra_domains=extra_domains)
    correctness_status = _correctness_status(correctness)
    judge_considered = bool(judge_trusted)

    blocked_by: list[str] = []

    # Identity must be present so the decision can be bound to the PR (feeds #2386). Fail-closed.
    if not _identity_ok(repo, base_sha, head_sha, diff_digest):
        blocked_by.append("identity:incomplete")

    # Domain veto is absolute: it is recorded regardless of every other signal.
    if domain.critical:
        detail = ",".join(domain.matched) if domain.matched else "unclassifiable"
        blocked_by.append(f"domain:{detail}")

    # Correctness gate is hard and fail-closed: only an explicit green clears it.
    if correctness_status != _GREEN:
        blocked_by.append(f"correctness:{correctness_status}")

    # Judge contributes only when trusted, and then only to BLOCK (never to earn a merge).
    if judge_considered and _judge_regressed(judge, noise_floor):
        blocked_by.append("judge:regressed")

    blocks = bool(blocked_by)
    decision = "block" if blocks else "allow"
    value_label = None if blocks else "mergeable-low-risk"
    if blocks:
        rationale = "blocked by: " + "; ".join(blocked_by)
    else:
        rationale = (
            "no subnet-critical domain path; correctness bundle green; "
            + ("no trusted-judge regression" if judge_considered else "judge not consulted (untrusted)")
        )

    return MergeabilityVerdict(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_digest=diff_digest,
        decision=decision,
        blocks_merge=blocks,
        blocked_by=tuple(blocked_by),
        absolute_veto=domain.critical,
        domain=domain.as_dict(),
        correctness_status=correctness_status,
        judge_considered=judge_considered,
        value_label=value_label,
        rationale=rationale,
    )
