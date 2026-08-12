"""Tests for per-decision attestation of live review decisions (#2386).

Includes regression tests for the adversarial-review findings: the artifact/input injection
bypass, prose leakage, nested-domain extras, and quote-honesty.
"""

import json
import re

import pytest

from benchmark.decision_attestation import (
    DECISION_EVIDENCE_VERSION,
    build_decision_evidence,
    verify_decision_evidence,
)
from openvang.mergeability import assess_mergeability

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_INPUTS = {
    "repo": "ext/repo",
    "pr_number": 42,
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
    "diff_digest": "c" * 64,
    "model": "deepseek-chat",
    "agent_commit": "d" * 40,
    "eval_image": "sha256:" + "e" * 64,
    "transcript_digest": "f" * 64,
}
# A decision artifact carries only sanitized decision fields — never identity (that lives in inputs).
_ARTIFACT = {
    "decision": "block",
    "blocks_merge": True,
    "absolute_veto": True,
    "correctness_status": "absent",
    "judge_considered": False,
    "blocked_by": ["domain:emission-weights", "correctness:absent"],
    "value_label": None,
    "domain": {"critical": True, "matched": ["emission-weights"],
               "reason": "touches permanently human-gated domain(s): emission-weights",
               "version": "domain-guardrail-v1"},
    "version": "mergeability-v1",
}


def test_build_shape_and_report_data_is_sha256():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    assert ev["version"] == DECISION_EVIDENCE_VERSION
    assert set(ev) == {"version", "inputs", "artifact", "artifact_digest", "report_data"}
    assert _SHA256.match(ev["report_data"]) and _SHA256.match(ev["artifact_digest"])
    assert set(ev["inputs"]) == set(_INPUTS)


def test_binding_is_deterministic():
    a = build_decision_evidence(_ARTIFACT, _INPUTS)
    b = build_decision_evidence(dict(_ARTIFACT), dict(_INPUTS))
    assert a["report_data"] == b["report_data"]


@pytest.mark.parametrize("field", list(_INPUTS))
def test_changing_any_bound_input_changes_report_data(field):
    base = build_decision_evidence(_ARTIFACT, _INPUTS)
    mutated = dict(_INPUTS, **{field: 999 if field == "pr_number" else "MUTATED"})
    assert build_decision_evidence(_ARTIFACT, mutated)["report_data"] != base["report_data"], field


def test_changing_a_decision_field_changes_artifact_digest_and_report_data():
    base = build_decision_evidence(_ARTIFACT, _INPUTS)
    changed = build_decision_evidence(dict(_ARTIFACT, blocks_merge=False), _INPUTS)
    assert changed["artifact_digest"] != base["artifact_digest"]
    assert changed["report_data"] != base["report_data"]


# --- Identity lives only in the inputs, never duplicated into the artifact --------------------

def test_identity_is_not_duplicated_into_the_artifact():
    ev = build_decision_evidence(
        dict(_ARTIFACT, repo="x", base_sha="y", head_sha="z", diff_digest="d", pr_number=1), _INPUTS
    )
    for field in ("repo", "base_sha", "head_sha", "diff_digest", "pr_number"):
        assert field not in ev["artifact"]


# --- The private-content invariant: prose / raw content is dropped at build -------------------

def test_private_artifact_fields_are_never_bound_or_published():
    poisoned = dict(
        _ARTIFACT,
        raw_diff="SECRET diff body",
        model_response="SECRET chain-of-thought",
        rationale="long model prose about the diff",
    )
    ev = build_decision_evidence(poisoned, _INPUTS)
    assert "raw_diff" not in ev["artifact"] and "rationale" not in ev["artifact"]
    blob = json.dumps(ev)
    assert "SECRET" not in blob and "model prose" not in blob
    # Dropping them did not change the binding vs the clean artifact (they were never bound).
    assert ev["report_data"] == build_decision_evidence(_ARTIFACT, _INPUTS)["report_data"]


def test_prose_value_label_is_dropped_but_a_controlled_label_is_kept():
    prose = build_decision_evidence({"decision": "block", "value_label": "risky because the diff..."}, _INPUTS)
    assert "value_label" not in prose["artifact"]  # has spaces -> rejected by the label pattern
    assert "risky because" not in json.dumps(prose)
    controlled = build_decision_evidence({"decision": "allow", "value_label": "mergeable-low-risk"}, _INPUTS)
    assert controlled["artifact"]["value_label"] == "mergeable-low-risk"


def test_domain_nested_extras_are_dropped():
    ev = build_decision_evidence(
        {"decision": "block", "domain": {"critical": True, "matched": ["x"], "reason": "r",
                                         "version": "v", "raw_paths": ["src/secret/pricing.py"],
                                         "notes": "model prose"}},
        _INPUTS,
    )
    assert set(ev["artifact"]["domain"]) == {"critical", "matched", "reason", "version"}
    blob = json.dumps(ev)
    assert "raw_paths" not in blob and "secret" not in blob


def test_blocked_by_keeps_tokens_and_drops_prose():
    ev = build_decision_evidence(
        {"decision": "block", "blocked_by": ["domain:emission-weights", "free prose with spaces"]}, _INPUTS
    )
    assert ev["artifact"]["blocked_by"] == ["domain:emission-weights"]


def test_non_mapping_artifact_and_inputs_degrade_safely():
    ev = build_decision_evidence("not-a-dict", None)
    assert ev["artifact"] == {}
    assert set(ev["inputs"]) == set(_INPUTS) and all(v is None for v in ev["inputs"].values())
    assert _SHA256.match(ev["report_data"])


# --- Verification: valid receipts pass, tampered/injected ones fail ---------------------------

def test_verify_accepts_a_valid_receipt_offline():
    report = verify_decision_evidence(build_decision_evidence(_ARTIFACT, _INPUTS))
    assert report["ok"] is True
    assert report["quote_checked"] is False


def test_verify_detects_a_tampered_decision():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    ev["artifact"] = dict(ev["artifact"], blocks_merge=False)  # flip the decision after the fact
    report = verify_decision_evidence(ev)
    assert report["ok"] is False
    assert report["checks"]["artifact_digest"] is False


def test_verify_rejects_an_injected_artifact_key():
    # The core review finding: an added key (even re-introduced private content) must NOT verify.
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    ev["artifact"] = dict(ev["artifact"], raw_diff="SECRET injected after the fact")
    report = verify_decision_evidence(ev)
    assert report["ok"] is False
    assert report["checks"]["artifact_canonical"] is False
    assert "SECRET" in json.dumps(ev) and report["ok"] is False  # present but rejected, not blessed


def test_verify_rejects_an_injected_input_key():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    ev["inputs"] = dict(ev["inputs"], sneaky="x")
    report = verify_decision_evidence(ev)
    assert report["ok"] is False
    assert report["checks"]["inputs_canonical"] is False


def test_verify_detects_a_tampered_report_data():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    ev["report_data"] = "0" * 64
    assert verify_decision_evidence(ev)["checks"]["report_data"] is False


def test_verify_binds_a_matching_quote_and_rejects_a_mismatch():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    good = verify_decision_evidence(ev, quote_report_data=ev["report_data"])
    assert good["ok"] is True and good["quote_checked"] is True
    bad = verify_decision_evidence(ev, quote_report_data="1" * 64)
    assert bad["ok"] is False and bad["checks"]["quote_binding"] is False


def test_empty_quote_is_not_reported_as_checked():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    report = verify_decision_evidence(ev, quote_report_data="")
    assert report["quote_checked"] is False  # honest: an empty string is not a quote
    assert report["ok"] is True


def test_verify_fail_closed_on_non_mapping_evidence():
    report = verify_decision_evidence("nope")
    assert report["ok"] is False and report["quote_checked"] is False


def test_unserializable_bound_value_fails_closed_not_crash():
    ev = build_decision_evidence(_ARTIFACT, _INPUTS)
    cyclic: dict = {}
    cyclic["self"] = cyclic
    ev["inputs"] = dict(ev["inputs"], model=cyclic)  # a bound field carrying a cycle
    report = verify_decision_evidence(ev)  # must not raise
    assert report["ok"] is False


# --- Round-trip with a real #2385 mergeability verdict ----------------------------------------

def test_round_trip_with_a_real_mergeability_verdict():
    verdict = assess_mergeability(
        repo="ext/repo", base_sha="a" * 40, head_sha="b" * 40, diff_digest="c" * 64,
        changed_paths=["neurons/set_weights.py"],  # domain-critical -> block
    )
    ev = build_decision_evidence(verdict.as_dict(), _INPUTS)
    assert verify_decision_evidence(ev)["ok"] is True
    assert ev["artifact"]["decision"] == "block"
    assert ev["artifact"]["absolute_veto"] is True
    assert set(ev["artifact"]["domain"]) == {"critical", "matched", "reason", "version"}
    assert "repo" not in ev["artifact"] and "base_sha" not in ev["artifact"]  # identity in inputs only
