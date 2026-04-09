"""cai.py — subcommand dispatcher.

Each subcommand lives in its own module (cai_init, cai_analyze, ...).
This file is the entry-point the container runs via
`python /app/cai.py <subcommand>`.
"""

import argparse
import sys

from cai_common import check_gh_auth
from cai_init import cmd_init
from cai_analyze import cmd_analyze
from cai_fix import cmd_fix
from cai_verify import cmd_verify
from cai_audit import cmd_audit
from cai_confirm import cmd_confirm
from cai_revise import cmd_revise


COMMANDS = {
    "init": cmd_init,
    "analyze": cmd_analyze,
    "fix": cmd_fix,
    "verify": cmd_verify,
    "audit": cmd_audit,
    "confirm": cmd_confirm,
    "revise": cmd_revise,
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Smoke test if no transcripts exist")
    sub.add_parser("analyze", help="Run the analyzer + publish findings")

    fix_parser = sub.add_parser("fix", help="Run the fix subagent")
    fix_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of picking the oldest",
    )

    sub.add_parser("revise", help="Iterate on open PRs based on review comments")
    sub.add_parser("verify", help="Update labels based on PR merge state")
    sub.add_parser("audit", help="Run the queue/PR consistency audit")
    sub.add_parser("confirm", help="Verify merged issues are actually solved")

    args = parser.parse_args()

    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
