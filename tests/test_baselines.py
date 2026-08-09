"""Tests for reference baselines (issue #12). Run:

    VANGUARSTEW_OFFLINE=1 python -m pytest -q
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["VANGUARSTEW_OFFLINE"] = "1"

from benchmark.baselines import (
    BASELINES,
    _baseline_list,
    _commit_subject,
    _infer_kind,
    _issue_title,
    _pr_title,
    _review_queue_items,  # noqa: E402
    _safe_backlog,
    empty_solve,
    get_baseline,
    heuristic_philosophy,
    heuristic_plan,
    heuristic_solve,
    queue_first_plan,
    queue_first_solve,
    stability_first_plan,
    stability_first_solve,
)
from benchmark.runner import run_replay  # noqa: E402
from benchmark.score import is_release_subject  # noqa: E402

CTX = {
    "frozen_at": {"commit": "abc0123456"},
    "recent_commits": [
        {"subject": "Fix crash in parser"},
        {"subject": "Add streaming API"},
        {"subject": "Refactor client internals"},
        {"subject": "Docs: document the config format"},
        {"subject": "Bump version to 1.2.0; update changelog"},
    ],
    "open_issues": [
        {"title": "Memory leak under load"},
        {"title": "Support YAML config"},
    ],
}


def test_registry_selection_and_unknown():
    assert get_baseline("empty") is empty_solve
    assert get_baseline("heuristic") is heuristic_solve
    assert get_baseline("queue_first") is queue_first_solve
    assert get_baseline("stability_first") is stability_first_solve
    assert set(BASELINES) >= {"empty", "heuristic", "queue_first", "stability_first"}
    with pytest.raises(ValueError):
        get_baseline("does-not-exist")


# --- stability_first baseline: stabilize bugfix/refactor before greenfield --------------------

def test_stability_first_same_action_set_as_heuristic():
    """Reprioritization only: nothing added or dropped relative to heuristic_plan."""
    for n in range(1, 10):
        heuristic = heuristic_plan(CTX, n)
        stable = stability_first_plan(CTX, n)
        assert len(heuristic) == len(stable)
        assert sorted(heuristic, key=lambda i: i["title"]) == sorted(
            stable, key=lambda i: i["title"]
        )


def test_stability_first_prioritizes_bugfix_and_refactor_before_features():
    plan = stability_first_plan(CTX, n=5)
    kinds = [item["kind"] for item in plan]
    stabilization = {"bugfix", "refactor"}
    greenfield = {"feature", "docs", "dep"}
    last_stab = max((i for i, k in enumerate(kinds) if k in stabilization), default=-1)
    first_green = min((i for i, k in enumerate(kinds) if k in greenfield), default=len(kinds))
    assert last_stab < first_green


def test_stability_first_release_after_stabilization_before_features():
    plan = stability_first_plan(CTX, n=8)
    kinds = [item["kind"] for item in plan]
    assert "release" in kinds
    release_idx = kinds.index("release")
    assert all(k in {"bugfix", "refactor"} for k in kinds[:release_idx])
    assert all(k in {"feature", "docs", "dep", "release", "triage"} for k in kinds[release_idx:])
    greenfield = {"feature", "docs", "dep"}
    if any(k in greenfield for k in kinds):
        assert release_idx < min(i for i, k in enumerate(kinds) if k in greenfield)


def test_stability_first_triage_sorts_last():
    ctx = {
        "open_issues": [
            {"title": "Mystery widget regression"},
            {"title": "Fix crash in loader"},
        ],
        "recent_commits": [],
    }
    stable = stability_first_plan(ctx, n=5)
    kinds = [item["kind"] for item in stable]
    if "triage" in kinds:
        assert kinds.index("triage") > kinds.index("bugfix")


def test_stability_first_preserves_within_tier_order():
    heuristic = heuristic_plan(CTX, n=5)
    stable = stability_first_plan(CTX, n=5)
    for kind in {"bugfix", "refactor", "feature", "docs", "release", "dep", "triage"}:
        h_titles = [i["title"] for i in heuristic if i["kind"] == kind]
        s_titles = [i["title"] for i in stable if i["kind"] == kind]
        assert h_titles == s_titles


def test_stability_first_solve_is_well_formed():
    out = stability_first_solve(context=CTX, n=5)
    assert isinstance(out["philosophy"], dict) and isinstance(out["plan"], list)
    assert out["action"] == "plan"
    assert "stability-first baseline" in out["rationale"]


def test_stability_first_caps_at_horizon():
    plan = stability_first_plan(CTX, n=3)
    assert len(plan) == 3
    assert len(heuristic_plan(CTX, n=3)) == 3


def test_stability_first_tolerates_malformed_context():
    out = stability_first_solve(context={"open_issues": 42, "recent_commits": []}, n=3)
    assert out["plan"] == []


# --- queue_first baseline: clear the open-PR review queue before greenfield work ------------

_CTX_WITH_QUEUE = {
    "recent_commits": [{"subject": "Add streaming API"}, {"subject": "Fix parser crash"}],
    "open_issues": [{"title": "Memory leak under load"}],
    "open_prs": [
        {"number": 7, "title": "Add streaming export"},
        {"number": 9, "title": "Fix flaky CI"},
    ],
}


def test_queue_first_leads_with_the_review_queue():
    plan = queue_first_solve(context=_CTX_WITH_QUEUE, n=5)["plan"]
    # The first items clear the open PRs, in queue order, as concrete triage items.
    assert plan[0]["title"] == "Review and merge PR: Add streaming export (#7)"
    assert plan[0]["kind"] == "triage" and plan[0]["theme"] == "PR review queue"
    assert plan[1]["title"] == "Review and merge PR: Fix flaky CI (#9)"
    # Remaining horizon is filled by the ordinary heuristic backlog/momentum plan.
    assert any("Memory leak" in item["title"] for item in plan[2:])


def test_queue_first_solve_is_well_formed_and_reports_queue_size():
    out = queue_first_solve(context=_CTX_WITH_QUEUE, n=5)
    assert isinstance(out["philosophy"], dict) and isinstance(out["plan"], list)
    assert out["action"] == "plan"
    assert "clear 2 open PR(s)" in out["rationale"]


def test_queue_first_degrades_to_heuristic_when_no_queue():
    # With no open PRs, queue_first_plan is exactly the heuristic plan (never a weaker bar).
    ctx = {"recent_commits": _CTX_WITH_QUEUE["recent_commits"], "open_issues": _CTX_WITH_QUEUE["open_issues"]}
    assert queue_first_plan(ctx, 5) == heuristic_plan(ctx, 5)


def test_queue_first_caps_review_items_at_the_horizon():
    ctx = {"open_prs": [{"number": i, "title": f"PR {i}"} for i in range(1, 11)]}
    plan = queue_first_plan(ctx, 3)
    assert len(plan) == 3
    assert all(item["theme"] == "PR review queue" for item in plan)   # queue fills the horizon
    assert plan[0]["title"] == "Review and merge PR: PR 1 (#1)"


def test_queue_first_skips_malformed_and_titleless_prs():
    ctx = {"open_prs": [
        "not-a-dict",
        {"number": 1},                       # no title
        {"title": "   "},                    # blank title
        {"title": 123},                      # non-string title
        {"number": 4, "title": "Real PR"},
    ]}
    plan = queue_first_plan(ctx, 5)
    review = [p for p in plan if p["theme"] == "PR review queue"]
    assert len(review) == 1
    assert review[0]["title"] == "Review and merge PR: Real PR (#4)"


def test_queue_first_omits_number_ref_when_absent_or_non_int():
    ctx = {"open_prs": [{"title": "No number"}, {"number": True, "title": "Bool number"}]}
    titles = [p["title"] for p in queue_first_plan(ctx, 5) if p["theme"] == "PR review queue"]
    assert "Review and merge PR: No number" in titles          # no "(#...)"
    assert "Review and merge PR: Bool number" in titles         # bool is not an int ref


def test_pr_title_helper_guards_non_dict_and_non_string():
    assert _pr_title({"title": "  hi  "}) == "hi"
    assert _pr_title({"title": 5}) == ""
    assert _pr_title({}) == ""
    assert _pr_title("nope") == ""
    assert _pr_title(None) == ""


def test_queue_first_tolerates_non_list_open_prs():
    # A malformed frozen context (open_prs not a list) must not crash the baseline.
    out = queue_first_solve(context={"open_prs": {"title": "oops"}, "recent_commits": []}, n=3)
    assert isinstance(out["plan"], list)


# --- #722/#957: the baseline is the last _issues_truncated consumer; it must fail closed too. ---
# fetch_context_at zeroes a truncated backlog at the source, but a frozen artifact can still carry
# a partial open_issues/open_prs alongside _issues_truncated=True (the "older frozen artifacts"
# case context_for_agent, planner._safe_prs, and open_issues_from_context all guard).

_CTX_TRUNCATED = {
    "recent_commits": [{"subject": "fix: patch the loader"}],
    "open_issues": [{"title": "Memory leak"}],
    "open_prs": [{"number": 7, "title": "Add streaming export"}],
    "_issues_truncated": True,
}


def test_safe_backlog_returns_empty_for_non_dict_or_truncated_context():
    # Robust to a non-dict context (None/list/str/int) — [] instead of AttributeError — and
    # fails closed on the flag while leaving a normal context untouched.
    for bad in (None, ["x"], "str", 42):
        assert _safe_backlog(bad, "open_prs") == []
    assert _safe_backlog(_CTX_TRUNCATED, "open_prs") == []
    assert _safe_backlog(_CTX_WITH_QUEUE, "open_prs") == _CTX_WITH_QUEUE["open_prs"]


def test_truncated_flag_fails_closed_only_when_boolean_true():
    # Mirrors #957: only a literal True fails closed; a truthy non-bool (1/"true"/[1]) is not the
    # flag and must NOT blank the queue, so a malformed value can't silently disarm the opponent.
    assert not any(p["theme"] == "PR review queue" for p in queue_first_plan(_CTX_TRUNCATED, 5))
    for not_true in (1, "true", [1]):
        ctx = {**_CTX_WITH_QUEUE, "_issues_truncated": not_true}
        assert any(p["theme"] == "PR review queue" for p in queue_first_plan(ctx, 5)), not_true


def test_all_baselines_fail_closed_on_truncation():
    # Both plan helpers and every solve entrypoint drop the partial backlog/queue on truncation.
    assert queue_first_plan(_CTX_TRUNCATED, 5) == heuristic_plan(_CTX_TRUNCATED, 5)
    for solve in (heuristic_solve, stability_first_solve, queue_first_solve):
        plan = solve(context=_CTX_TRUNCATED, n=5)["plan"]
        assert not any(p.get("theme") in ("PR review queue", "issue backlog") for p in plan), solve
    assert "0 open issue(s)" in heuristic_philosophy(_CTX_TRUNCATED)["summary"]


def test_non_truncated_baseline_still_uses_queue_and_backlog():
    # Control: without the flag the baseline reviews the queue and addresses the backlog, isolating
    # _issues_truncated as the sole cause of the fail-closed behavior above.
    assert any(p["theme"] == "PR review queue" for p in queue_first_plan(_CTX_WITH_QUEUE, 5))
    assert any(p["theme"] == "issue backlog" for p in heuristic_plan(_CTX_WITH_QUEUE, 5))


def test_empty_baseline_proposes_nothing():
    out = empty_solve(context=CTX, n=5)
    assert out["plan"] == []
    assert out["philosophy"] == {}


def test_default_baseline_is_a_real_opponent_not_the_empty_floor():
    """Regression guard for the judge saturation (#2379).

    If the default opponent is the ``empty`` floor, any non-trivial challenger beats it on every
    task, ``_JUDGE_COMPONENT[challenger]`` pins ``judge_mean`` at 1.0, and the judge half of the
    composite stops discriminating (it also inflates ``composite_mean``). The default MUST be a
    real maintainer that proposes concrete work a challenger has to out-reason to beat.
    """
    from benchmark.baselines import DEFAULT_BASELINE

    assert DEFAULT_BASELINE != "empty"
    default_opponent = get_baseline(DEFAULT_BASELINE)
    out = default_opponent(context=CTX, n=5)
    assert isinstance(out.get("plan"), list) and len(out["plan"]) > 0, (
        "the default judge opponent must propose concrete work, not the empty floor"
    )


def test_heuristic_baseline_derives_a_real_plan():
    out = heuristic_solve(context=CTX, n=5)
    plan = out["plan"]
    assert 0 < len(plan) <= 5
    for item in plan:
        assert {"title", "kind", "rationale", "theme"} <= set(item)
    # open issues are addressed...
    assert any("Memory leak" in item["title"] for item in plan)
    # ...and the philosophy reflects the repo's own signals
    phil = out["philosophy"]
    assert phil["summary"] and phil["values"] and phil["evidence"]
    # the release cadence in history is anticipated
    assert any(item["kind"] == "release" for item in plan) or len(plan) == 5


def test_heuristic_is_stronger_than_empty_offline():
    # Given the same context, the heuristic proposes more than the empty floor.
    assert len(heuristic_solve(context=CTX, n=5)["plan"]) > len(empty_solve(context=CTX)["plan"])


def test_infer_kind_does_not_misclassify_incidental_versions_as_release():
    # A version mention that isn't a genuine release cut (bugfix mentioning a version,
    # a dependency bump) must not be swept into "release" by a crude substring match.
    assert _infer_kind("fix crash in v1.2.0 parser") == "bugfix"
    assert _infer_kind("bump lodash to v4.17.21") == "dep"


def test_infer_kind_recognizes_genuine_release_subjects():
    assert _infer_kind("Release v1.2.0") == "release"
    assert _infer_kind("Bump version to 1.2.0; update changelog") == "release"


def test_infer_kind_matches_scoring_release_detection():
    """Regression guard: would fail if baseline and scoring release detection diverge."""
    subjects = [
        "fix crash in v1.2.0 parser",
        "bump lodash to v4.17.21",
        "Release v1.2.0",
        "Bump version to 1.2.0; update changelog",
        "v2.0.0",
        "add streaming API",
        "docs: document v1 config format",
    ]
    for subject in subjects:
        assert (_infer_kind(subject) == "release") == is_release_subject(subject), subject


def test_infer_kind_uses_shared_release_detection_not_substring_needles():
    """Regression (#129): baseline release classification must follow is_release_subject."""
    assert _infer_kind("Bump dependency to v10.0") == "dep"
    assert not is_release_subject("Bump dependency to v10.0")
    assert _infer_kind("Add v2 endpoint") == "feature"
    assert not is_release_subject("Add v2 endpoint")
    for subject in ("v1.2.0", "Release v2.0"):
        assert is_release_subject(subject)
        assert _infer_kind(subject) == "release"


def test_infer_kind_maps_ci_and_test_commits_to_refactor_not_triage():
    # Regression (#270): the dead "test" keyword bucket used to collapse into "triage".
    assert _infer_kind("ci: pin runner os version") == "refactor"
    assert _infer_kind("test: add fixture for loader") == "refactor"
    ctx = {
        "recent_commits": [
            {"subject": "ci: add windows runner"},
            {"subject": "test: add fixture for loader"},
        ],
        "open_issues": [],
    }
    assert "triage work" not in heuristic_philosophy(ctx)["summary"].lower()
    assert "refactor work" in heuristic_philosophy(ctx)["summary"].lower()


def test_issue_title_tolerates_non_string_fields():
    assert _issue_title({"title": "Fix loader"}) == "Fix loader"
    assert _issue_title({"title": ["Fix", "loader"]}) == ""
    assert _issue_title({"title": 42}) == ""
    assert _issue_title({"title": None}) == ""
    assert _issue_title({"title": "   "}) == ""


def test_heuristic_plan_skips_issues_with_non_string_title():
    plan = heuristic_plan({
        "open_issues": [
            {"title": ["Fix", "loader"]},
            {"title": "Support YAML config"},
        ],
        "recent_commits": [],
    })
    assert any("YAML config" in item["title"] for item in plan)
    assert not any("loader" in item["title"] for item in plan)


def test_commit_subject_tolerates_non_dict_entries():
    assert _commit_subject({"subject": "Fix loader"}) == "Fix loader"
    assert _commit_subject("a bare string commit") == ""
    assert _commit_subject(None) == ""
    assert _commit_subject(42) == ""
    assert _commit_subject(["Fix", "loader"]) == ""


def test_commit_subject_tolerates_a_non_string_subject():
    # A dict entry whose subject isn't a string (a number, list, or nested object from an
    # imperfect context producer) must resolve to "", never leaking a non-string into the
    # kind heuristic. Mirrors _issue_title's handling of non-string titles.
    assert _commit_subject({"subject": 123}) == ""
    assert _commit_subject({"subject": ["Fix", "loader"]}) == ""
    assert _commit_subject({"subject": None}) == ""
    assert _commit_subject({"subject": {"nested": "obj"}}) == ""
    assert _commit_subject({"subject": "Fix loader"}) == "Fix loader"


def test_heuristic_baseline_survives_a_non_string_commit_subject():
    # Regression: a non-string subject used to crash the kind heuristic with
    # "AttributeError: 'int' object has no attribute 'lower'". It must instead be treated as
    # empty (triage) while the well-formed commits around it are still classified and counted.
    ctx = {
        "recent_commits": [
            {"subject": "Add release v1.2.0"},
            {"subject": 123},
            {"subject": "Fix parser crash"},
        ],
        "open_issues": [],
    }
    philosophy = heuristic_philosophy(ctx)
    assert philosophy["values"]                       # did not crash
    plan = heuristic_plan(ctx)
    assert plan                                       # did not crash
    # The malformed entry is counted as triage, not as a release/fix it isn't.
    assert any(item["kind"] == "release" for item in plan)


def test_commit_subject_logs_a_warning_for_non_dict_entries(caplog):
    with caplog.at_level(logging.WARNING, logger="benchmark.baselines"):
        assert _commit_subject("not a dict") == ""
    assert any("non-dict recent_commits" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="benchmark.baselines"):
        assert _commit_subject({"subject": "Fix loader"}) == "Fix loader"
    assert not caplog.records


def test_heuristic_baseline_skips_non_dict_commit_entries():
    # A malformed recent_commits entry (e.g. a bare string from an imperfect context
    # producer) must not crash the heuristic baseline, and the well-formed commits
    # around it must still be correctly classified and counted.
    ctx = {
        "recent_commits": [
            {"subject": "Fix crash in parser"},
            "a malformed string entry instead of a dict",
            {"subject": "Fix another bug"},
        ],
        "open_issues": [],
    }
    kinds = heuristic_plan(ctx)
    bugfix_items = [i for i in kinds if i["kind"] == "bugfix"]
    assert len(bugfix_items) == 1
    assert "2 recent" in bugfix_items[0]["rationale"]  # both good commits counted

    phil = heuristic_philosophy(ctx)
    assert "Fix crash in parser" in phil["evidence"]
    assert "Fix another bug" in phil["evidence"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_replay_selects_baseline_and_tallies():
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
        for i in range(20):
            with open(os.path.join(d, f"f{i}.py"), "w", encoding="utf-8") as f:
                f.write(f"x = {i}\n")
            subprocess.run(["git", "-C", d, "add", "-A"], check=True)
            subprocess.run(["git", "-C", d, "commit", "-q", "-m", f"add feature {i}"], check=True)
        res = run_replay(d, agent_file=os.path.join(ROOT, "agent.py"),
                         n_tasks=2, horizon=3, baseline="heuristic")
        assert res["baseline"] == "heuristic"
        tally = res["tally"]
        # every task is decided; the counts are consistent with the number of tasks
        assert tally["challenger"] + tally["baseline"] + tally["tie"] == res["tasks"]
        assert res["tasks"] >= 1
    finally:
        shutil.rmtree(d, ignore_errors=True)

# --- #515: truthy non-list context containers must not abort baselines -----------

_MALFORMED_LIST_FIELDS = [42, 3.14, True, {"title": "Fix bug"}, "not a list"]


def test_baseline_list_accepts_only_real_lists():
    rows = [{"subject": "Fix bug"}]
    for bad in _MALFORMED_LIST_FIELDS:
        assert _baseline_list(bad, "recent_commits") == [], bad
    assert _baseline_list(rows, "recent_commits") == rows
    assert _baseline_list(None, "open_issues") == []


def test_heuristic_plan_survives_non_list_recent_commits_and_open_issues():
    for field, bad in (("recent_commits", 42), ("open_issues", 42)):
        ctx = {"recent_commits": [], "open_issues": [], field: bad}
        assert heuristic_plan(ctx, 5) == [], field
        assert heuristic_philosophy(ctx)["evidence"] == [], field


def test_heuristic_plan_honors_valid_rows_when_one_list_field_is_malformed():
    ctx = {
        "recent_commits": [{"subject": "Fix crash in parser"}],
        "open_issues": 42,
    }
    plan = heuristic_plan(ctx, 5)
    assert any(item["kind"] == "bugfix" for item in plan)
    assert heuristic_philosophy(ctx)["evidence"] == ["Fix crash in parser"]


def test_queue_first_plan_survives_non_list_open_prs():
    ctx = {"open_prs": 42, "recent_commits": [], "open_issues": []}
    assert queue_first_plan(ctx, 3) == []


def test_baseline_list_logs_warning_for_non_list_field(caplog):
    with caplog.at_level(logging.WARNING, logger="benchmark.baselines"):
        assert _baseline_list(42, "open_issues") == []
    assert any("open_issues is int" in r.message for r in caplog.records)


def test_heuristic_functions_handle_non_dict_context():
    """heuristic_philosophy and heuristic_plan must not crash on non-dict context."""
    assert heuristic_philosophy(None)["summary"]
    assert heuristic_philosophy("not a dict")["summary"]
    assert heuristic_plan(None, 3) == []
    assert heuristic_plan(42, 3) == []


def test_review_queue_items_handles_non_dict_context():
    assert _review_queue_items(None, 3) == []
    assert _review_queue_items("str", 3) == []
