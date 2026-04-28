"""`cai-app-init`: configure the current clone to push as cai[bot]."""
from __future__ import annotations

import argparse
import re
import sys
from urllib.parse import urlparse

from git import InvalidGitRepositoryError, Repo

from cai.git import add_local, set_local, unset_all_local

from .bot import CaiBot
from cai.github.labels import LabelSpec, ensure_labels

_GH_SSH = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def _detect_origin() -> str:
    try:
        url = Repo(".").remotes.origin.url
    except InvalidGitRepositoryError as exc:
        raise ValueError("not inside a git repository") from exc
    if m := _GH_SSH.match(url):
        return f"{m['owner']}/{m['repo']}"
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        raise ValueError(f"origin {url!r} is not a github.com remote")
    path = parsed.path.strip("/").removesuffix(".git")
    if path.count("/") != 1:
        raise ValueError(f"unexpected origin path {path!r}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-app-init",
        description="Configure the current clone to commit and push as cai[bot].",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="owner/repo (defaults to inferring from `origin`)",
    )
    args = parser.parse_args()

    full_name = args.repo or _detect_origin()
    bot = CaiBot()
    try:
        iid = bot.installation_id(full_name)
    except Exception as exc:
        print(
            f"cai-app-init: could not resolve installation for {full_name}: {exc}\n"
            f"Install the cai App on this repo first: "
            f"https://github.com/settings/installations",
            file=sys.stderr,
        )
        sys.exit(1)

    ensure_labels(
        bot,
        full_name,
        [
            LabelSpec(name="cai:raised", color="0e8a16", description="Trigger cai to solve"),
            LabelSpec(name="cai:audit", color="fbca04", description="For cai to review"),
        ],
    )

    set_local("user.name", "cai[bot]")
    set_local(
        "user.email",
        f"{bot.app_id}+cai[bot]@users.noreply.github.com",
    )
    # Reset any inherited helper; the empty-string entry then re-add is
    # git's documented way to shadow a global helper inside one repo.
    unset_all_local("credential.https://github.com.helper")
    add_local("credential.https://github.com.helper", "")
    add_local("credential.https://github.com.helper", "!cai-git-credential")
    set_local("credential.https://github.com.useHttpPath", "true")

    print(
        f"Configured {full_name} as cai[bot] (installation {iid}).\n"
        f"  user.name  = cai[bot]\n"
        f"  user.email = {bot.app_id}+cai[bot]@users.noreply.github.com"
    )


if __name__ == "__main__":
    main()
