"""Permanent, fail-closed domain guardrail for subnet-critical code paths (#2384).

A pure, dependency-free classifier: given the repository-relative paths a change touches, it
decides whether the change reaches a **permanently human-gated** subnet-critical domain
(emission/weight-setting, validator consensus, scoring/rewards, payment, wallet/keys, on-chain
governance). It renders a verdict only — it performs no I/O, holds no GitHub or chain client, and
never executes anything. Callers (the review recommendation in #2385, and any future executor)
consult it as an **absolute veto**: a critical match can never be overridden by a benchmark band,
a judge verdict, or operator convenience.

Design posture — **fail closed, and deliberately conservative but not total**:

- Input that cannot be parsed safely (malformed, non-string, absolute, ``..``, control chars) or an
  empty change set resolves to ``critical=True`` — an unclassifiable change is treated as critical,
  never waved through.
- The built-in domain set is matched by **token prefix** on separator-split path segments, so
  ``neurons/set_weights.py`` matches ``weight`` while ``core/lightweight.py`` does not. Matching is
  intentionally generous within a domain (plurals/suffixes hit) because a borderline path routed to
  a human is safe, whereas a money/consensus path let through is not. It is *not* a bare-substring
  match, because marking every ``payload.py`` payment-critical would erode trust in the gate without
  buying safety.

The built-in classes are a **versioned, forever-human-gated set** (``GUARDRAIL_VERSION``). A per-repo
override may only **add** patterns/classes; it can never remove or weaken a built-in class — a
malicious or careless override that drops ``wallet-keys`` still leaves wallet paths critical.

This module is Stage 1 of #2384 (the classifier). Wiring it into the review recommendation (Stage 2),
publishing the set through ``openvang.factory.FactoryPolicy.public_contract`` (Stage 3), and the CI
enforcement (Stage 4) are separate follow-ups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

GUARDRAIL_VERSION = "domain-guardrail-v1"

# Built-in critical domain classes -> keyword prefixes matched against separator-split path
# tokens (case-insensitive). PERMANENTLY human-gated; this table is the versioned contract.
_BUILTIN_DOMAINS: dict[str, tuple[str, ...]] = {
    "emission-weights": ("weight", "emission", "emit", "reweight"),
    "consensus": ("consensus", "validator", "yuma", "vtrust", "bond"),
    "scoring-rewards": ("scoring", "reward", "incentive", "payout"),
    "payment": ("payment", "billing", "invoice", "settlement", "escrow"),
    "wallet-keys": ("wallet", "hotkey", "coldkey", "keypair", "keystore", "mnemonic", "privkey",
                    "signer", "signing"),
    "onchain-governance": ("subtensor", "substrate", "extrinsic", "metagraph", "staking", "stake",
                           "governance", "proposal", "netuid", "dividend"),
}

_SEPARATORS = re.compile(r"[/_.\-]+")


@dataclass(frozen=True)
class DomainVerdict:
    """A deterministic classification of a change set against the critical-domain set."""

    critical: bool
    matched: tuple[str, ...]
    reason: str
    version: str = GUARDRAIL_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "critical": self.critical,
            "matched": list(self.matched),
            "reason": self.reason,
            "version": self.version,
        }


def _normalized_paths(paths) -> tuple[str, ...] | None:
    """Return safe repository-relative paths, or ``None`` for malformed input.

    Mirrors ``scripts.benchmark_pr_policy._normalized_paths`` so the guardrail rejects exactly the
    same shapes the contributor-PR policy already treats as untrusted; kept as an inlined copy so
    this module stays dependency-free.
    """
    if not isinstance(paths, (list, tuple, set, frozenset)):
        return None
    normalized_paths = []
    for path in paths:
        if not isinstance(path, str):
            return None
        if path != path.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            return None
        normalized = path
        while normalized.startswith("./"):
            normalized = normalized[2:]
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            return None
        normalized_paths.append(normalized)
    return tuple(normalized_paths)


def _tokens(path: str) -> set[str]:
    return {token for token in _SEPARATORS.split(path.lower()) if token}


def _match_domains(path: str, domains: Mapping[str, Iterable[str]]) -> set[str]:
    tokens = _tokens(path)
    return {
        name
        for name, keywords in domains.items()
        if any(token.startswith(keyword) for token in tokens for keyword in keywords)
    }


def _validated_extra(extra) -> dict[str, tuple[str, ...]]:
    """Validate an operator's add-only override, or raise ``ValueError``.

    Operator misconfiguration is a hard error (not a runtime input), so it fails loudly rather than
    fail-closed-to-critical — the caller must fix the config, not have every PR silently blocked.
    """
    if not isinstance(extra, Mapping):
        raise ValueError("extra_domains must be a mapping of {class_name: [keyword, ...]}")
    cleaned: dict[str, tuple[str, ...]] = {}
    for name, keywords in extra.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"extra_domains class name must be a non-empty string, got {name!r}")
        if isinstance(keywords, str) or not isinstance(keywords, Iterable):
            raise ValueError(f"extra_domains[{name!r}] must be an iterable of keyword strings")
        kws = []
        for keyword in keywords:
            if not isinstance(keyword, str) or not keyword.strip():
                raise ValueError(f"extra_domains[{name!r}] keywords must be non-empty strings")
            kws.append(keyword.strip().lower())
        cleaned[name] = tuple(kws)
    return cleaned


def _effective_domains(extra_domains) -> dict[str, tuple[str, ...]]:
    """The built-in set with an add-only override merged in.

    Every built-in class is always present and only ever grows: the override is unioned onto the
    built-ins, so it can add a class or extend one, but can never remove a class or drop a built-in
    keyword. This is the invariant that makes the critical set permanent.
    """
    domains: dict[str, tuple[str, ...]] = {name: tuple(kws) for name, kws in _BUILTIN_DOMAINS.items()}
    if extra_domains:
        for name, kws in _validated_extra(extra_domains).items():
            domains[name] = tuple(sorted(set(domains.get(name, ())) | set(kws)))
    return domains


def builtin_domains() -> dict[str, tuple[str, ...]]:
    """The versioned, permanently human-gated built-in domain set (a copy)."""
    return {name: tuple(kws) for name, kws in _BUILTIN_DOMAINS.items()}


def classify(paths, *, extra_domains: Mapping[str, Iterable[str]] | None = None) -> DomainVerdict:
    """Classify a change set against the critical-domain set. Fail-closed on unclassifiable input."""
    domains = _effective_domains(extra_domains)
    normalized = _normalized_paths(paths)
    if normalized is None:
        return DomainVerdict(True, (), "unparseable or malformed changed-path set (fail-closed)")
    if not normalized:
        return DomainVerdict(True, (), "empty changed-path set: nothing to classify (fail-closed)")
    matched: set[str] = set()
    for path in normalized:
        matched |= _match_domains(path, domains)
    if matched:
        names = tuple(sorted(matched))
        return DomainVerdict(
            True, names, "touches permanently human-gated domain(s): " + ", ".join(names)
        )
    return DomainVerdict(False, (), "no subnet-critical domain path matched")
