"""CLI: verify a published score is the one an attested enclave actually produced.

  python -m scripts.verify_attestation --artifact result.json --evidence evidence.json \
      [--transcript transcript.json] [--quote-report-data <hex>] [--strict]

This is the script a skeptic — a validator, a contributor, the subnet — runs to check a published
`perf:*` score without trusting whoever ran it. It answers, in order:

  1. Is the published artifact the one the evidence bundle describes?  (artifact_digest)
  2. Is the evidence bundle self-consistent?                            (report_data)
  3. Is the published transcript the one that produced it?              (transcript_digest)
  4. Does a real TEE quote attest THIS run?                             (quote_binding)

Checks 1-3 are fully offline and need no special hardware, which is the point: most of the
verifiable value arrives before any TEE exists. Check 4 activates once a quote is published — pass
its ``report_data`` with ``--quote-report-data``.

What this deliberately does NOT do: validate a quote's signature chain to Intel/AMD/cloud roots.
That is format-specific and belongs with whichever TEE the deployment lands on; keeping it out
means this script never implies hardware attestation happened when it did not. ``quote_checked``
in the output reports honestly whether a quote took part.
"""

from __future__ import annotations

import argparse
import json
import sys

from benchmark.attestation import verify_evidence
from benchmark.transcript import TranscriptStore


def _load(path: str, what: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"verify_attestation: cannot read {what} ({path}): {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifact", required=True, help="the published run_eval result")
    ap.add_argument("--evidence", required=True, help="the published evidence bundle")
    ap.add_argument("--transcript", default="", help="the published LLM transcript (optional)")
    ap.add_argument("--quote-report-data", default=None,
                    help="report_data from a real TEE quote (optional, hex)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any check fails")
    args = ap.parse_args(argv)

    artifact = _load(args.artifact, "artifact")
    evidence = _load(args.evidence, "evidence")

    report = verify_evidence(artifact, evidence, args.quote_report_data)

    # The transcript check is separate from the evidence binding: it proves the *published*
    # transcript is the one whose digest was bound, which is what lets a third party re-derive the
    # score offline rather than taking the artifact on faith.
    if args.transcript:
        # Mirror `_load`: a bad --transcript path (missing, a directory, unreadable, non-UTF-8,
        # or malformed JSON) must exit 2 with an actionable message, not raise a raw traceback —
        # the artifact/evidence reads already do this, and TranscriptStore.load does no guarding
        # of its own (it only tolerates malformed *content*, not a bad *path* or non-JSON bytes).
        try:
            recorded = TranscriptStore.load(args.transcript).digest()
        except (OSError, ValueError) as exc:
            print(f"verify_attestation: cannot read transcript ({args.transcript}): {exc}",
                  file=sys.stderr)
            raise SystemExit(2) from exc
        inputs = evidence.get("inputs")
        if inputs is not None and not isinstance(inputs, dict):
            print(
                f"verify_attestation: evidence.inputs must be a mapping, got {type(inputs).__name__}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        claimed = (inputs or {}).get("transcript_digest")
        report["checks"]["transcript_digest"] = recorded == claimed
        report["ok"] = all(report["checks"].values())
        if recorded != claimed:
            report["detail"] = f"transcript_digest FAILED (recorded {recorded[:12]}, "
            report["detail"] += f"bound {str(claimed)[:12]})"

    print(json.dumps(report, indent=2, sort_keys=True))
    for name, passed in sorted(report["checks"].items()):
        print(f"  {'PASS' if passed else 'FAIL'}  {name}", file=sys.stderr)
    if not report.get("quote_checked"):
        print("  NOTE  no quote supplied -- binding verified, hardware attestation NOT checked",
              file=sys.stderr)
    print(f"verify_attestation: {'OK' if report['ok'] else 'FAILED'}", file=sys.stderr)

    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(run())
