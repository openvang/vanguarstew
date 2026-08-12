"""Per-decision attestation for live review decisions (#2386).

``benchmark/attestation.py`` binds a scored benchmark **run**. This binds a live merge/close
**decision** the same way, so an external repo owner can verify offline that a specific decision on
their PR was made over the inputs claimed and was not edited after the fact.

The chain: the decision inputs (``repo``, ``pr_number``, ``base_sha``, ``head_sha``, ``diff_digest``,
``model``, ``agent_commit``, ``eval_image``, ``transcript_digest``) plus the public decision artifact
are folded into a ``report_data`` recomputable by anyone holding the published receipt.

Raw content never enters the binding, and this is enforced by construction rather than by trust:

- The diff and the model call are represented only by their digests (``diff_digest``,
  ``transcript_digest``).
- The artifact is passed through a **sanitizing projection** (:func:`_safe_artifact`) before it is
  bound *and* published — a content filter, not just a key-name allowlist. Identity lives only in
  the inputs (never duplicated into the artifact); free-form prose (a ``rationale``, or a
  model-authored ``value_label``) is dropped; ``blocked_by`` is kept only as structured tokens;
  ``domain`` is re-projected to its four derived fields, dropping any nested extras.
- :func:`verify_decision_evidence` binds the artifact **as published** and additionally requires the
  published artifact and inputs to equal their canonical sanitized form, so a receipt with an
  injected key (even one the sanitizer would strip) fails verification instead of being blessed.

Offline verification proves *internal consistency* only. The hardware quote (Stage 4) is the trust
root that binds ``report_data`` to an enclave; when no quote is supplied that is reported honestly.
The same ``report_data`` binds into a real TDX quote unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from benchmark.transcript import digest

DECISION_EVIDENCE_VERSION = "vanguarstew-decision-evidence-v1"

# The fields a live decision is bound to: which PR, at which commits, reviewed by which
# model/agent/eval-image, over which recorded transcript. Content-free by construction (digests/ids).
# Identity lives here and ONLY here — it is never duplicated into the artifact.
_DECISION_INPUT_FIELDS = (
    "repo",
    "pr_number",
    "base_sha",
    "head_sha",
    "diff_digest",
    "model",
    "agent_commit",
    "eval_image",
    "transcript_digest",
)

# The four derived, content-free fields of a domain verdict (see openvang.domain_guardrail).
_DOMAIN_FIELDS = ("critical", "matched", "reason", "version")

# A short controlled label (e.g. "mergeable-low-risk"), never prose. Rejects anything with a space.
_LABEL_RE = re.compile(r"^[a-z][a-z0-9:_-]{0,48}$")
# A structured blocked-by token, e.g. "domain:emission-weights,wallet-keys" / "correctness:absent".
# The no-whitespace charset is what keeps model/bundle prose out of a published token.
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*:[a-z0-9,_./-]{0,200}$")


def _safe_artifact(decision_artifact) -> dict:
    """Sanitizing projection of a decision artifact: the ONLY content that may be bound/published.

    A content filter, not a key-name pass-through. Idempotent, so a published artifact can be
    checked against its own sanitized form. Fail-safe: a non-mapping yields ``{}`` rather than
    leaking or raising. Never carries a raw diff, PR body, model prose, identity, or nested extras.
    """
    if not isinstance(decision_artifact, Mapping):
        return {}
    art = decision_artifact
    out: dict[str, object] = {}
    if isinstance(art.get("decision"), str):
        out["decision"] = art["decision"]
    for field in ("blocks_merge", "absolute_veto", "judge_considered"):
        if isinstance(art.get(field), bool):
            out[field] = art[field]
    for field in ("correctness_status", "version"):
        if isinstance(art.get(field), str):
            out[field] = art[field]
    if isinstance(art.get("blocked_by"), list):
        out["blocked_by"] = [t for t in art["blocked_by"] if isinstance(t, str) and _TOKEN_RE.match(t)]
    label = art.get("value_label")
    if label is None or (isinstance(label, str) and _LABEL_RE.match(label)):
        out["value_label"] = label
    if isinstance(art.get("domain"), Mapping):
        out["domain"] = {f: art["domain"][f] for f in _DOMAIN_FIELDS if f in art["domain"]}
    return out


def _bound_inputs(inputs) -> dict:
    if not isinstance(inputs, Mapping):
        inputs = {}
    return {field: inputs.get(field) for field in _DECISION_INPUT_FIELDS}


def build_decision_evidence(decision_artifact, inputs) -> dict:
    """The publish-ready evidence receipt for one live decision, including the ``report_data``.

    Binds and publishes the *sanitized* artifact projection (not the raw ``decision_artifact``), so
    the returned bundle is safe to publish as-is: everything it contains is exactly what
    ``report_data`` commits to.
    """
    bound_inputs = _bound_inputs(inputs)
    safe_artifact = _safe_artifact(decision_artifact)
    artifact_digest = digest(safe_artifact)
    return {
        "version": DECISION_EVIDENCE_VERSION,
        "inputs": bound_inputs,
        "artifact": safe_artifact,
        "artifact_digest": artifact_digest,
        "report_data": digest({"inputs": bound_inputs, "artifact_digest": artifact_digest}),
    }


def verify_decision_evidence(evidence, quote_report_data: str | None = None) -> dict:
    """Recompute the binding from a published decision receipt (checks 1-3). Never raises.

    Beyond the digest checks, it requires the published ``artifact`` and ``inputs`` to equal their
    canonical sanitized form, so a receipt carrying an injected key (including re-introduced private
    content) fails rather than being blessed. ``ok`` is true only when every runnable check passed;
    the quote check is *skipped, not failed,* when no non-empty ``quote_report_data`` is supplied.
    """
    if not isinstance(evidence, Mapping):
        return {
            "ok": False,
            "checks": {"evidence_shape": False},
            "quote_checked": False,
            "detail": f"evidence is {type(evidence).__name__}, not a mapping",
        }

    quote = quote_report_data if isinstance(quote_report_data, str) and quote_report_data.strip() else None

    try:
        recomputed = build_decision_evidence(evidence.get("artifact"), evidence.get("inputs"))
    except Exception as exc:  # e.g. an unserializable/cyclic value inside a hostile receipt
        return {
            "ok": False,
            "checks": {"recompute": False},
            "quote_checked": False,
            "detail": f"receipt could not be recomputed: {exc}",
        }

    checks: dict[str, bool] = {
        # The published objects must already BE their sanitized form — no injected/unsafe extras.
        "artifact_canonical": evidence.get("artifact") == recomputed["artifact"],
        "inputs_canonical": evidence.get("inputs") == recomputed["inputs"],
        "artifact_digest": evidence.get("artifact_digest") == recomputed["artifact_digest"],
        "report_data": evidence.get("report_data") == recomputed["report_data"],
    }
    if quote is not None:
        checks["quote_binding"] = quote == recomputed["report_data"]

    ok = all(checks.values())
    detail = "all checks passed" if ok else "; ".join(
        f"{name} FAILED" for name, passed in checks.items() if not passed
    )
    return {
        "ok": ok,
        "checks": checks,
        "quote_checked": quote is not None,
        "expected_report_data": recomputed["report_data"],
        "detail": detail,
    }
