"""GitHub App identity for cai. Wraps PyGithub for clean repo access."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from github import Auth, Github, GithubIntegration
from github.Repository import Repository


def _xdg(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


_HOME = Path.home()
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", _HOME / ".config") / "cai"
CACHE_DIR = _xdg("XDG_CACHE_HOME", _HOME / ".cache") / "cai"


class CaiBot:
    """One App, many repos. Lazily resolves and caches per-installation auth.

    Construct once; reuse for every API call. Thread-safe for concurrent
    callers thanks to an internal lock around cache writes.
    """

    def __init__(
        self,
        config_dir: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self._cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        env = self._load_env(self._config_dir / "app.env")
        self.app_id = int(env["APP_ID"])
        key_path = Path(env.get("PRIVATE_KEY_PATH") or self._config_dir / "github-app.pem")
        self._app_auth = Auth.AppAuth(self.app_id, key_path.read_text())
        self._integration = GithubIntegration(auth=self._app_auth)
        self._install_map_path = self._cache_dir / "installations.json"
        self._install_map: dict[str, int] = self._read_json(self._install_map_path) or {}
        self._clients: dict[int, Github] = {}
        self._lock = Lock()

    def verify(self) -> dict:
        """Validate credentials by fetching App metadata. Raises on failure."""
        app = self._integration.get_app()
        return {"name": app.name, "id": app.id, "slug": app.slug}

    def installation_id(self, full_name: str) -> int:
        if full_name in self._install_map:
            return self._install_map[full_name]
        owner, name = self._split(full_name)
        install = self._integration.get_repo_installation(owner, name)
        with self._lock:
            self._install_map[full_name] = install.id
            self._write_json(self._install_map_path, self._install_map)
        return install.id

    def client(self, full_name: str) -> Github:
        iid = self.installation_id(full_name)
        client = self._clients.get(iid)
        if client is None:
            client = Github(auth=self._app_auth.get_installation_auth(iid))
            self._clients[iid] = client
        return client

    def repo(self, full_name: str) -> Repository:
        return self.client(full_name).get_repo(full_name)

    def token_for(self, full_name: str) -> str:
        # Disk cache because git spawns the credential helper as a fresh
        # process per push — in-process caching would never hit.
        iid = self.installation_id(full_name)
        cache_file = self._cache_dir / "tokens" / f"{iid}.json"
        cached = self._read_json(cache_file)
        if cached and cached.get("expires_at", 0) > time.time() + 300:
            return cached["token"]
        token_obj = self._integration.get_access_token(iid)
        expires_at = token_obj.expires_at
        if isinstance(expires_at, datetime):
            expires_at = expires_at.timestamp()
        with self._lock:
            self._write_json(
                cache_file,
                {"token": token_obj.token, "expires_at": expires_at},
                mode=0o600,
            )
        return token_obj.token

    @staticmethod
    def _load_env(path: Path) -> dict[str, str]:
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. See cai/github/setup.md for first-time setup."
            )
        out: dict[str, str] = {}
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
        if "APP_ID" not in out:
            raise ValueError(f"{path} missing APP_ID")
        return out

    @staticmethod
    def _split(full_name: str) -> tuple[str, str]:
        if "/" not in full_name:
            raise ValueError(f"expected owner/repo, got {full_name!r}")
        owner, _, name = full_name.partition("/")
        return owner, name

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(path, mode)
