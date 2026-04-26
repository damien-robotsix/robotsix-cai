"""``cai-issue`` CLI: pull/push GitHub issues as JSON+MD file pairs."""
from __future__ import annotations

import argparse
from pathlib import Path

from .bot import CaiBot
from .issues import pull, push
from .repo import parse_issue_ref


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-issue",
        description="Round-trip GitHub issues as JSON+MD file pairs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser("pull", help="Download an issue to <dir>/<n>.{json,md}")
    p_pull.add_argument("target", help="owner/repo#number")
    p_pull.add_argument("--dir", type=Path, default=Path("."))

    p_push = sub.add_parser(
        "push",
        help="Apply local issue. Creates if number is null, else updates.",
    )
    p_push.add_argument("path", type=Path, help="path to issue .json")

    args = parser.parse_args()
    bot = CaiBot()

    if args.cmd == "pull":
        ref = parse_issue_ref(args.target)
        if ref is None:
            parser.error(f"expected owner/repo#number, got {args.target!r}")
        repo, number = ref
        json_path, md_path = pull(bot, repo, number, args.dir)
        print(json_path)
        print(md_path)
    elif args.cmd == "push":
        issue = push(bot, args.path)
        print(f"#{issue.number} {issue.html_url}")


if __name__ == "__main__":
    main()
