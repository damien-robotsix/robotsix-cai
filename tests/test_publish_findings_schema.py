"""Regression test: audit-health findings schema matches publish.py expectations.

Verifies that a findings.json shaped exactly as documented in
.claude/agents/audit/cai-audit-audit-health.md (key, confidence,
evidence, remediation) is accepted in full by load_findings_json —
no "missing required field" skips.  Prevents silent schema drift
from breaking the audit-health pipeline (issue #1287).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.publish import load_findings_json  # noqa: E402


class TestAuditHealthFindingsSchema(unittest.TestCase):
    """load_findings_json accepts the canonical audit-health schema."""

    def _write_findings(self, findings: list[dict]) -> str:
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"findings": findings}, fh)
        fh.close()
        return fh.name

    def _sample_findings(self) -> list[dict]:
        return [
            {
                "title": "code-reduction/actions: error rows present",
                "category": "audit-health",
                "key": "code-reduction-actions-error",
                "confidence": "high",
                "evidence": "3 error rows found between 2026-04-17 and 2026-04-24.",
                "remediation": "Inspect the code-reduction/actions audit log and restart the audit run.",
            },
            {
                "title": "cost-reduction/cai: stale audit",
                "category": "audit-health",
                "key": "cost-reduction-cai-stale",
                "confidence": "medium",
                "evidence": "No finish row in the last 7 days; last run was 2026-04-10.",
                "remediation": "Re-trigger the cost-reduction audit for the cai module.",
            },
            {
                "title": "external-libs/fsm: degenerate zero-findings",
                "category": "audit-health",
                "key": "external-libs-fsm-degenerate",
                "confidence": "low",
                "evidence": "All finish rows over the last 14 days report findings_count=0.",
                "remediation": "Check whether the external-libs audit for fsm is misconfigured.",
            },
        ]

    def test_all_findings_accepted(self):
        """Every well-formed audit-health finding must parse without skips."""
        sample = self._sample_findings()
        path = self._write_findings(sample)
        try:
            parsed = load_findings_json(path, valid_categories={"audit-health"})
            self.assertEqual(
                len(parsed),
                len(sample),
                f"Expected {len(sample)} findings; got {len(parsed)}. "
                "Schema mismatch — check key/confidence/evidence/remediation fields.",
            )
        finally:
            os.unlink(path)

    def test_old_schema_rejected(self):
        """Findings using the old fingerprint/severity/body schema must be rejected."""
        old_schema_findings = [
            {
                "title": "code-reduction/actions: error rows present",
                "body": "3 error rows found.",
                "category": "audit-health",
                "fingerprint": "code-reduction-actions-error",
                "severity": "high",
            }
        ]
        path = self._write_findings(old_schema_findings)
        try:
            parsed = load_findings_json(path, valid_categories={"audit-health"})
            self.assertEqual(
                len(parsed),
                0,
                "Old schema (fingerprint/severity/body) should produce 0 parsed findings.",
            )
        finally:
            os.unlink(path)

    def test_empty_findings_returns_empty_list(self):
        """An empty findings list produces an empty result (healthy audit)."""
        path = self._write_findings([])
        try:
            parsed = load_findings_json(path, valid_categories={"audit-health"})
            self.assertEqual(parsed, [])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
