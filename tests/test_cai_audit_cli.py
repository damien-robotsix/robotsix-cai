"""Tests for the `cai audit <kind>` CLI wiring (issue #1191).

Pins:
  1. `cai audit <kind>` parses with args.command == "audit" and
     args.audit_kind == <kind> for every supported kind, including
     the new "health" leaf.
  2. `cai audit` with no kind errors out via argparse (SystemExit).
  3. The hidden back-compat aliases `cai audit-module --kind <kind>`
     and `cai audit-health` still parse so stale shell aliases from
     pre-issue-1191 installers keep working.
  4. `_dispatch_audit` routes `health` to cmd_audit_health and any
     other kind to cmd_audit_module, aliasing args.audit_kind onto
     args.kind.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai  # noqa: E402 — sys.path munging above


class TestAuditSubcommandParsing(unittest.TestCase):
    """`cai audit <kind>` — new nested subcommand grammar."""

    def test_audit_good_practices_parses(self):
        parser = cai._build_parser()
        args = parser.parse_args(["audit", "good-practices"])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.audit_kind, "good-practices")

    def test_audit_health_parses(self):
        parser = cai._build_parser()
        args = parser.parse_args(["audit", "health"])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.audit_kind, "health")

    def test_audit_all_kinds_parse(self):
        parser = cai._build_parser()
        for kind in (
            "good-practices",
            "code-reduction",
            "cost-reduction",
            "workflow-enhancement",
            "health",
        ):
            with self.subTest(kind=kind):
                args = parser.parse_args(["audit", kind])
                self.assertEqual(args.command, "audit")
                self.assertEqual(args.audit_kind, kind)

    def test_audit_missing_kind_errors(self):
        parser = cai._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["audit"])

    def test_audit_invalid_kind_errors(self):
        parser = cai._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["audit", "bogus-kind"])


class TestAuditLegacyAliases(unittest.TestCase):
    """Hidden back-compat aliases must keep parsing (stale shell aliases)."""

    def test_audit_module_legacy_parses(self):
        parser = cai._build_parser()
        args = parser.parse_args(["audit-module", "--kind", "good-practices"])
        self.assertEqual(args.command, "audit-module")
        self.assertEqual(args.kind, "good-practices")

    def test_audit_module_all_kinds_parse(self):
        parser = cai._build_parser()
        for kind in (
            "good-practices",
            "code-reduction",
            "cost-reduction",
            "workflow-enhancement",
        ):
            with self.subTest(kind=kind):
                args = parser.parse_args(["audit-module", "--kind", kind])
                self.assertEqual(args.command, "audit-module")
                self.assertEqual(args.kind, kind)

    def test_audit_health_legacy_parses(self):
        parser = cai._build_parser()
        args = parser.parse_args(["audit-health"])
        self.assertEqual(args.command, "audit-health")


class TestDispatchAudit(unittest.TestCase):
    """`_dispatch_audit` routes to the correct handler by audit_kind."""

    def test_health_routes_to_cmd_audit_health(self):
        calls = {}

        def fake_health(args):
            calls["target"] = "audit-health"
            calls["args"] = args
            return 0

        orig = cai.cmd_audit_health
        cai.cmd_audit_health = fake_health
        try:
            args = cai._build_parser().parse_args(["audit", "health"])
            rc = cai._dispatch_audit(args)
        finally:
            cai.cmd_audit_health = orig

        self.assertEqual(rc, 0)
        self.assertEqual(calls["target"], "audit-health")

    def test_kind_routes_to_cmd_audit_module_with_aliased_kind(self):
        calls = {}

        def fake_module(args):
            calls["target"] = "audit-module"
            calls["kind"] = args.kind
            return 0

        orig = cai.cmd_audit_module
        cai.cmd_audit_module = fake_module
        try:
            args = cai._build_parser().parse_args(["audit", "good-practices"])
            rc = cai._dispatch_audit(args)
        finally:
            cai.cmd_audit_module = orig

        self.assertEqual(rc, 0)
        self.assertEqual(calls["target"], "audit-module")
        self.assertEqual(calls["kind"], "good-practices")


if __name__ == "__main__":
    unittest.main()
