"""Git credential helper that returns an installation token for github.com.

Wired up by `cai-app-init` as `credential.https://github.com.helper`.
"""
from __future__ import annotations

import sys

from .bot import CaiBot


def _read_request() -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    return fields


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "get"
    if action != "get":
        # store/erase: cache is owned by CaiBot, not by git.
        return
    fields = _read_request()
    if fields.get("host") != "github.com":
        return
    full_name = fields.get("path", "").strip("/")
    if full_name.endswith(".git"):
        full_name = full_name[:-4]
    if "/" not in full_name:
        return
    try:
        token = CaiBot().token_for(full_name)
    except Exception as exc:
        # Empty stdout = "no credentials, fall through". Surface the
        # reason on stderr so failures aren't silent.
        print(f"cai-git-credential: {exc}", file=sys.stderr)
        return
    sys.stdout.write(f"username=x-access-token\npassword={token}\n")


if __name__ == "__main__":
    main()
