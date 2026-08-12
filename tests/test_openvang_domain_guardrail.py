"""Tests for the permanent, fail-closed domain guardrail (#2384, Stage 1)."""

import pytest

from openvang.domain_guardrail import (
    GUARDRAIL_VERSION,
    DomainVerdict,
    builtin_domains,
    classify,
)

# --- Each built-in critical domain class must match a representative path ----------------------

@pytest.mark.parametrize(
    "path, expected_class",
    [
        ("neurons/set_weights.py", "emission-weights"),
        ("src/emission/schedule.rs", "emission-weights"),
        ("validator/consensus.py", "consensus"),
        ("core/yuma_consensus.rs", "consensus"),
        ("scoring/reward_model.py", "scoring-rewards"),
        ("miner/incentive.rs", "scoring-rewards"),
        ("services/payment_processor.go", "payment"),
        ("billing/invoice.ts", "payment"),
        ("wallet/hotkey_manager.py", "wallet-keys"),
        ("keys/coldkey.rs", "wallet-keys"),
        ("chain/subtensor_client.py", "onchain-governance"),
        ("staking/dividend.rs", "onchain-governance"),
    ],
)
def test_each_builtin_domain_class_is_critical(path, expected_class):
    verdict = classify([path])
    assert verdict.critical is True
    assert expected_class in verdict.matched
    assert expected_class in verdict.reason


def test_all_six_builtin_classes_are_covered_by_the_table():
    # Guards against a class being dropped from the built-in set without a test noticing.
    assert set(builtin_domains()) == {
        "emission-weights", "consensus", "scoring-rewards",
        "payment", "wallet-keys", "onchain-governance",
    }


# --- Clean, low-risk paths must NOT be critical -----------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/getting-started.md",
        "tests/test_cli.py",
        "src/cli/parser.ts",
        "core/lightweight.py",       # 'weight' is mid-word -> not matched (prefix, not substring)
        "app/validation.py",         # 'validation' does not prefix-match 'validator'
        "utils/payload.rs",          # 'payload' does not prefix-match 'payment'
        "pyproject.toml",
    ],
)
def test_low_risk_paths_are_not_critical(path):
    verdict = classify([path])
    assert verdict.critical is False, f"{path} should not be domain-critical: {verdict.reason}"
    assert verdict.matched == ()


# --- Fail-closed on unclassifiable input ------------------------------------------------------

def test_fail_closed_on_none():
    assert classify(None).critical is True


def test_fail_closed_on_empty():
    v = classify([])
    assert v.critical is True
    assert v.matched == ()


@pytest.mark.parametrize(
    "bad",
    [
        ["ok.py", 123],            # non-string element
        ["/etc/passwd"],           # absolute path
        ["../secrets.py"],         # parent traversal
        ["a/../b.py"],             # embedded traversal
        ["bad\tpath.py"],          # control character
        [" leading-space.py"],     # not stripped
        "not-a-list",              # wrong container type
    ],
)
def test_fail_closed_on_malformed(bad):
    assert classify(bad).critical is True


# --- Add-only override invariant --------------------------------------------------------------

def test_override_can_add_a_new_domain_class():
    v = classify(["core/frobnicate.py"], extra_domains={"repo-critical": ["frobnicate"]})
    assert v.critical is True
    assert "repo-critical" in v.matched


def test_override_can_extend_a_builtin_class():
    # A repo whose key material lives under 'secrets/' can add that token to wallet-keys.
    v = classify(["secrets/store.rs"], extra_domains={"wallet-keys": ["secret"]})
    assert v.critical is True
    assert "wallet-keys" in v.matched


def test_override_cannot_remove_or_weaken_a_builtin_class():
    # An override that maps a built-in class to an empty keyword list must NOT disarm it.
    v = classify(["wallet/hotkey.py"], extra_domains={"wallet-keys": []})
    assert v.critical is True
    assert "wallet-keys" in v.matched


def test_override_omitting_a_builtin_leaves_it_intact():
    # An override that only adds an unrelated class still leaves every built-in armed.
    v = classify(["neurons/set_weights.py"], extra_domains={"unrelated": ["frobnicate"]})
    assert v.critical is True
    assert "emission-weights" in v.matched


@pytest.mark.parametrize("bad_override", [42, "weights", {"": ["x"]}, {"c": "weights"}, {"c": [""]}, {"c": [1]}])
def test_malformed_override_raises_rather_than_silently_disarming(bad_override):
    with pytest.raises(ValueError):
        classify(["src/app.py"], extra_domains=bad_override)


# --- Determinism + shape ----------------------------------------------------------------------

def test_matched_is_sorted_and_deterministic():
    # A single change set touching two domains reports both, sorted, every time.
    paths = ["wallet/coldkey.rs", "neurons/set_weights.py"]
    first = classify(paths)
    second = classify(list(reversed(paths)))
    assert first.matched == second.matched == ("emission-weights", "wallet-keys")


def test_verdict_shape_and_version():
    v = classify(["wallet/hotkey.py"])
    assert isinstance(v, DomainVerdict)
    assert v.version == GUARDRAIL_VERSION
    d = v.as_dict()
    assert set(d) == {"critical", "matched", "reason", "version"}
    assert isinstance(d["matched"], list)


def test_any_critical_path_in_a_mixed_set_makes_the_whole_set_critical():
    # A PR that is mostly docs but also edits emission logic is critical.
    v = classify(["README.md", "docs/x.md", "neurons/set_weights.py"])
    assert v.critical is True
    assert v.matched == ("emission-weights",)


def test_builtin_domains_returns_a_copy():
    d = builtin_domains()
    d["emission-weights"] = ()
    assert classify(["neurons/set_weights.py"]).critical is True  # mutation did not leak
