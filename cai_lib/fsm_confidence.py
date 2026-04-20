"""FSM confidence parsing for the auto-improve lifecycle.

Defines :class:`Confidence` and the helper functions that extract
confidence signals from agent structured output.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class Confidence(Enum):
    """Qualitative confidence level emitted by agents.

    Ordered so ``Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH`` —
    use comparison operators to gate transitions rather than comparing
    raw ints.
    """
    LOW    = 1
    MEDIUM = 2
    HIGH   = 3

    def __lt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value <= other.value

    def __gt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value > other.value

    def __ge__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value >= other.value


_CONFIDENCE_RE = re.compile(
    r"^[^\w\n]*Confidence[^\w\n]*[:=][^\w\n]*(LOW|MEDIUM|HIGH)[^\w\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

_CONFIDENCE_REASON_RE = re.compile(
    r"^Confidence reason:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

_REQUIRES_HUMAN_REVIEW_RE = re.compile(
    r"^Requires human review:\s*(true|false)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_APPROVABLE_AT_MEDIUM_RE = re.compile(
    r"^Approvable at medium:\s*(true|false)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_confidence(text: str) -> Optional[Confidence]:
    """Extract ``Confidence: LOW|MEDIUM|HIGH`` from agent structured output.

    Returns the parsed level, or ``None`` when no well-formed line is
    present. Callers must treat ``None`` as "missing" and divert to
    HUMAN_NEEDED — never assume a default level.
    """
    if not text:
        return None
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return None
    return Confidence[m.group(1).upper()]


def parse_confidence_reason(text: str) -> Optional[str]:
    """Extract ``Confidence reason: <text>`` from a plan block body.

    Returns the reason string, or ``None`` when the line is absent.
    Backward-compatible: existing plan blocks without this line return
    ``None`` and callers must treat that as "no reason available".
    """
    if not text:
        return None
    m = _CONFIDENCE_REASON_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


def parse_requires_human_review(text: str) -> bool:
    """Extract ``Requires human review: true|false`` from a plan block body.

    Returns ``True`` only when a well-formed ``Requires human review: true``
    line is present (case-insensitive). Any other case — the line absent,
    explicitly ``false``, or malformed — returns ``False`` so the gate
    falls through to its normal confidence-based routing.

    Used by :func:`cai_lib.actions.plan.handle_plan_gate` to surface a
    bespoke divert message when ``cai-select`` knowingly chose a plan
    that diverges from the refined-issue's stated preference (#982).
    """
    if not text:
        return False
    m = _REQUIRES_HUMAN_REVIEW_RE.search(text)
    if not m:
        return False
    return m.group(1).lower() == "true"


def parse_approvable_at_medium(text: str) -> bool:
    """Extract ``Approvable at medium: true|false`` from a plan block body.

    Returns ``True`` only when a well-formed ``Approvable at medium: true``
    line is present (case-insensitive). Any other case — the line absent,
    explicitly ``false``, or malformed — returns ``False`` so the gate
    falls through to the default HIGH-threshold routing.

    Used by :func:`cai_lib.actions.plan.handle_plan_gate` to route a
    MEDIUM-confidence plan through the relaxed
    ``planned_to_plan_approved_approvable`` transition when ``cai-select``
    explicitly flagged the plan's residual risks as soft / non-blocking
    (#1008).
    """
    if not text:
        return False
    m = _APPROVABLE_AT_MEDIUM_RE.search(text)
    if not m:
        return False
    return m.group(1).lower() == "true"


_RESUME_RE = re.compile(
    r"^\s*ResumeTo\s*[:=]\s*([A-Z_]+)\s*$",
    re.MULTILINE,
)


def parse_resume_target(text: str) -> Optional[str]:
    """Extract ``ResumeTo: <STATE_NAME>`` from a cai-unblock agent reply.

    Returns the raw state name as written by the agent (uppercased per
    our structured-output convention) or ``None`` if the marker is
    missing. The caller decides whether the returned name maps to a
    real IssueState/PRState member.
    """
    if not text:
        return None
    m = _RESUME_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()
