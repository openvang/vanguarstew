"""Tests for the correctness-evidence contract and its fail-closed status (#2383, Stage 0)."""

import pytest

from openvang.correctness import (
    CORRECTNESS_VERSION,
    GREEN,
    RED,
    CorrectnessBundle,
    build_correctness_bundle,
    derive_status,
)
from openvang.mergeability import assess_mergeability

_OK = dict(
    repo="ext/repo",
    base_sha="a" * 40,
    head_sha="b" * 40,
    diff_digest="c" * 64,
    test_command="pytest -q",
    exit_code=0,
    passed=12,
    failed=0,
    timed_out=False,
)


def _status(**overrides):
    return derive_status(**dict(_OK, **overrides))


# --- The single green path ---------------------------------------------------------------------

def test_clean_nonempty_pass_is_green():
    assert _status() == GREEN


# --- Everything else is red (fail-closed) ------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"exit_code": 1},                # nonzero exit
    {"exit_code": None},             # unknown exit
    {"exit_code": 0.0},              # float, not a real exit code
    {"exit_code": False},            # bool masquerading as 0
    {"exit_code": "0"},              # string, not an int
    {"failed": 1},                   # a failing test
    {"failed": None},                # unknown failure count
    {"failed": -1},                  # malformed count
    {"passed": 0},                   # empty suite: ran nothing -> not a pass
    {"passed": None},                # unknown pass count
    {"passed": -3},                  # malformed count
    {"timed_out": True},             # timed out
    {"timed_out": None},             # ambiguous timeout flag
    {"test_command": ""},            # no command actually ran
    {"test_command": None},          # missing command
    {"repo": ""},                    # incomplete identity
    {"base_sha": ""},
    {"head_sha": None},
    {"diff_digest": "   "},
    {"passed": True},                # bool is not a valid count
    {"failed": False},
])
def test_anything_but_a_clean_pass_is_red(override):
    assert _status(**override) == RED, override


# --- The builder derives status and never trusts a supplied one --------------------------------

def test_builder_derives_green():
    b = build_correctness_bundle(**_OK, skipped=1, duration_s=3.2, changed_line_coverage=0.8)
    assert b.status == GREEN
    assert b.version == CORRECTNESS_VERSION


def test_builder_status_cannot_be_forced_by_a_supplied_field():
    # A caller passing status="green" among the observations must NOT override the derivation:
    # build_correctness_bundle has no status parameter, and a red run stays red.
    with pytest.raises(TypeError):
        build_correctness_bundle(status="green", **dict(_OK, exit_code=1))  # noqa: E1123
    red = build_correctness_bundle(**dict(_OK, exit_code=1))
    assert red.status == RED


def test_coverage_is_recorded_but_does_not_gate_green():
    # A clean pass with zero changed-line coverage is still green (coverage is a Stage-2 axis).
    b = build_correctness_bundle(**_OK, changed_line_coverage=0.0)
    assert b.status == GREEN
    assert b.changed_line_coverage == 0.0


def test_bundle_shape_and_identity_binding():
    b = build_correctness_bundle(**_OK)
    assert isinstance(b, CorrectnessBundle)
    d = b.as_dict()
    assert d["repo"] == "ext/repo" and d["diff_digest"] == "c" * 64
    assert d["status"] == GREEN
    assert set(d) == {
        "repo", "base_sha", "head_sha", "diff_digest", "test_command", "status",
        "exit_code", "passed", "failed", "skipped", "duration_s", "timed_out",
        "changed_line_coverage", "version",
    }


# --- It is the input #2385 consumes -----------------------------------------------------------

def test_green_bundle_clears_the_mergeability_correctness_gate():
    bundle = build_correctness_bundle(**_OK)
    verdict = assess_mergeability(
        repo="ext/repo", base_sha="a" * 40, head_sha="b" * 40, diff_digest="c" * 64,
        changed_paths=["src/app.py"],  # domain-clean
        correctness=bundle.as_dict(),
    )
    assert verdict.decision == "allow"
    assert verdict.correctness_status == GREEN


def test_red_bundle_blocks_the_mergeability_verdict():
    bundle = build_correctness_bundle(**dict(_OK, failed=2))
    verdict = assess_mergeability(
        repo="ext/repo", base_sha="a" * 40, head_sha="b" * 40, diff_digest="c" * 64,
        changed_paths=["src/app.py"],
        correctness=bundle.as_dict(),
    )
    assert verdict.blocks_merge is True
    assert "correctness:red" in verdict.blocked_by
