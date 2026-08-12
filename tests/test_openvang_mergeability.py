"""Tests for the external-repo mergeability verdict (#2385, Stage 1)."""

import pytest

from openvang.mergeability import (
    MERGEABILITY_VERSION,
    MergeabilityVerdict,
    assess_mergeability,
)

_ID = dict(repo="ext/repo", base_sha="a" * 40, head_sha="b" * 40, diff_digest="c" * 64)
_GREEN = {"status": "green"}


def _assess(**overrides):
    kwargs = dict(_ID, changed_paths=["docs/readme.md"], correctness=_GREEN)
    kwargs.update(overrides)
    return assess_mergeability(**kwargs)


# --- The happy path clears only when every gate clears ----------------------------------------

def test_allow_when_domain_clean_and_correctness_green_and_judge_untrusted():
    v = _assess()
    assert v.decision == "allow"
    assert v.blocks_merge is False
    assert v.blocked_by == ()
    assert v.value_label == "mergeable-low-risk"
    assert v.judge_considered is False  # untrusted by default (#2379)


# --- Correctness gate is hard and fail-closed -------------------------------------------------

def test_block_when_correctness_absent_by_default():
    # No bundle supplied -> absent -> block (the correct default until #2383 ships).
    v = assess_mergeability(**_ID, changed_paths=["src/app.py"])
    assert v.blocks_merge is True
    assert "correctness:absent" in v.blocked_by
    assert v.value_label is None


@pytest.mark.parametrize("bad", [{"status": "red"}, {"status": "error"}, {"status": ""}, {}, "nope", 3])
def test_block_when_correctness_not_green(bad):
    v = _assess(correctness=bad)
    assert v.blocks_merge is True
    assert any(b.startswith("correctness:") for b in v.blocked_by)


# --- Domain veto is absolute ------------------------------------------------------------------

def test_domain_critical_is_an_absolute_veto_no_signal_overrides():
    # Correctness green AND a trusted, clean judge -> still blocked by the domain veto.
    v = _assess(
        changed_paths=["neurons/set_weights.py"],
        correctness=_GREEN,
        judge={"axes": {"scope": 0.5}},
        judge_trusted=True,
    )
    assert v.blocks_merge is True
    assert v.absolute_veto is True
    assert any(b.startswith("domain:emission-weights") for b in v.blocked_by)
    assert v.value_label is None


def test_domain_fail_closed_propagates_from_malformed_paths():
    v = _assess(changed_paths=["/etc/passwd"], correctness=_GREEN)  # unparseable -> critical
    assert v.blocks_merge is True
    assert v.absolute_veto is True
    assert "domain:unclassifiable" in v.blocked_by


# --- Identity must be present (fail-closed; feeds #2386) --------------------------------------

@pytest.mark.parametrize("field", ["repo", "base_sha", "head_sha", "diff_digest"])
def test_block_when_identity_incomplete(field):
    v = _assess(**{field: ""})
    assert v.blocks_merge is True
    assert "identity:incomplete" in v.blocked_by


# --- Judge is gated off while it saturates (#2379) --------------------------------------------

def test_untrusted_judge_is_not_consulted_even_if_regressed():
    # A regressed judge does NOT block while untrusted; the allow rests on correctness+domain only.
    v = _assess(judge={"regressed": True}, judge_trusted=False)
    assert v.decision == "allow"
    assert v.judge_considered is False
    assert "judge:regressed" not in v.blocked_by


def test_a_saturated_judge_cannot_by_itself_produce_a_merge_verdict():
    # Judge glowing-positive but correctness absent -> still blocked (the judge can't earn a merge).
    v = assess_mergeability(
        **_ID, changed_paths=["src/app.py"], judge={"axes": {"scope": 1.0}}, judge_trusted=True
    )
    assert v.blocks_merge is True
    assert "correctness:absent" in v.blocked_by


def test_trusted_judge_regression_blocks_anti_goodhart():
    # Correctness green + domain clean, but a trusted judge axis regressed -> block, never averaged.
    v = _assess(judge={"axes": {"readability": -0.2}}, judge_trusted=True)
    assert v.blocks_merge is True
    assert "judge:regressed" in v.blocked_by


def test_trusted_judge_with_malformed_payload_fails_closed():
    v = _assess(judge={"axes": "not-a-mapping"}, judge_trusted=True)
    assert v.blocks_merge is True
    assert "judge:regressed" in v.blocked_by


# --- Anti-Goodhart: never average, list every blocker -----------------------------------------

def test_multiple_blockers_are_all_listed_not_collapsed():
    v = assess_mergeability(
        **_ID, changed_paths=["wallet/coldkey.rs"], correctness={"status": "red"},
        judge={"regressed": True}, judge_trusted=True,
    )
    assert v.blocks_merge is True
    kinds = {b.split(":")[0] for b in v.blocked_by}
    assert {"domain", "correctness", "judge"} <= kinds


# --- Shape / identity binding -----------------------------------------------------------------

def test_verdict_shape_and_identity_binding():
    v = _assess()
    assert isinstance(v, MergeabilityVerdict)
    assert v.version == MERGEABILITY_VERSION
    d = v.as_dict()
    assert {"repo", "base_sha", "head_sha", "diff_digest"} <= set(d)
    assert d["repo"] == _ID["repo"] and d["diff_digest"] == _ID["diff_digest"]
    assert isinstance(d["blocked_by"], list) and isinstance(d["domain"], dict)
