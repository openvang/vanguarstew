"""Correctness-evidence contract for a PR diff (#2383, Stage 0).

The mergeability verdict (#2385) has a hard, fail-closed correctness gate: only an explicit
``green`` bundle clears it. This module defines that bundle and — crucially — the **fail-closed
derivation of its ``status``**, so ``green`` means exactly one thing: the target repo's own test
suite was actually run over the diff and passed cleanly.

A bundle is keyed to ``{repo, base_sha, head_sha, diff_digest}`` and records what an execution
observed: the test command, exit code, pass/fail/skip counts, duration, whether it timed out, and
(optionally) the fraction of changed lines exercised by tests. ``status`` is **derived here, never
supplied by the caller** — an untrusted producer cannot hand us ``status="green"`` directly.

``status`` is ``green`` only when every one of these holds; anything else (including any malformed,
missing, or ambiguous field) is ``red``:

- identity is complete (``repo``/``base_sha``/``head_sha``/``diff_digest`` all present);
- a real test command was run;
- the run did not time out;
- ``exit_code == 0``;
- zero failing tests; and
- **at least one test actually ran** (``passed >= 1``) — a suite that executed nothing is not a
  pass, closing the "no tests, therefore vacuously green" hole.

Changed-line coverage is recorded but does NOT gate ``green`` here — "were the changed lines
exercised?" is the Stage-2 claim-verification axis, kept separate from "did the suite pass".

Stage 1 — actually checking out at ``base_sha``, applying the diff, and running the suite in a
sandbox — is a follow-up; it will populate this bundle by reusing the approval-binding + namespace +
resource-limit pattern of ``benchmark/sealed_execution.py`` (whose own docstring is explicit it is
"not an adversarial filesystem sandbox": the host FS is still mounted and the net namespace gives no
egress) and ``openvang/isolated.py`` (execution bound to an approved ``request_sha256``). Those two
isolation gaps — an untrusted-code boundary and a controlled dependency-provisioning path — are
required Stage-1 design work, not assumed solved.
"""

from __future__ import annotations

from dataclasses import dataclass

CORRECTNESS_VERSION = "correctness-evidence-v1"

GREEN = "green"
RED = "red"


@dataclass(frozen=True)
class CorrectnessBundle:
    """A serializable record of a test run over a diff, with a fail-closed derived ``status``."""

    repo: str
    base_sha: str
    head_sha: str
    diff_digest: str
    test_command: str
    status: str
    exit_code: int | None
    passed: int | None
    failed: int | None
    skipped: int | None
    duration_s: float | None
    timed_out: bool
    changed_line_coverage: float | None
    version: str = CORRECTNESS_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff_digest": self.diff_digest,
            "test_command": self.test_command,
            "status": self.status,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "changed_line_coverage": self.changed_line_coverage,
            "version": self.version,
        }


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def derive_status(
    *,
    repo,
    base_sha,
    head_sha,
    diff_digest,
    test_command,
    exit_code,
    passed,
    failed,
    timed_out,
) -> str:
    """Return ``green`` only for a clean, non-empty pass over complete identity; else ``red``.

    Fail-closed: every check must hold, and any malformed/missing field falls through to ``red``.
    """
    if not all(_nonempty_str(v) for v in (repo, base_sha, head_sha, diff_digest, test_command)):
        return RED
    if timed_out is not False:  # anything but an explicit "did not time out" is unsafe
        return RED
    if not _nonneg_int(exit_code) or exit_code != 0:  # int 0 only; reject 0.0 / False / None
        return RED
    if not _nonneg_int(failed) or failed != 0:
        return RED
    if not _nonneg_int(passed) or passed < 1:  # a suite that ran nothing is not a pass
        return RED
    return GREEN


def build_correctness_bundle(
    *,
    repo: str,
    base_sha: str,
    head_sha: str,
    diff_digest: str,
    test_command: str,
    exit_code: int | None,
    passed: int | None,
    failed: int | None,
    skipped: int | None = None,
    duration_s: float | None = None,
    timed_out: bool = False,
    changed_line_coverage: float | None = None,
) -> CorrectnessBundle:
    """Build a bundle from an execution's observations, deriving ``status`` fail-closed.

    ``status`` is computed, never accepted from the caller, so an untrusted producer cannot assert a
    green run.
    """
    status = derive_status(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_digest=diff_digest,
        test_command=test_command,
        exit_code=exit_code,
        passed=passed,
        failed=failed,
        timed_out=timed_out,
    )
    return CorrectnessBundle(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_digest=diff_digest,
        test_command=test_command,
        status=status,
        exit_code=exit_code,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_s=duration_s,
        timed_out=bool(timed_out),
        changed_line_coverage=changed_line_coverage,
    )
