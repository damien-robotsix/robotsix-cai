"""``spike_run`` — bespoke tool for the spike subagent.

The spike agent verifies a runtime fact (call signature, return shape,
exception class, library behaviour) by running a short throwaway python
script. The tool deliberately does not delegate to the backend's
generic ``execute``: that path requires plumbing ``DeferredToolRequests``
into every parent agent that lists it, leaks credentials through
inherited env, and gives the model unconstrained shell. Instead we
expose a structured ``spike_run(script=..., pip_install=..., timeout=...)``
that runs python directly with a scrubbed env in a per-workspace
scratch dir.

Scratch dir convention: ``<repo_root>/../spike``. ``repo_root`` is the
backend's root, and the cai-solve workspace layout makes its parent
the per-issue directory — so the spike scratch sits next to the issue
metadata and the cloned repo, isolated from both.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pydantic_ai import RunContext, Tool

# Env vars passed through to spike scripts. Anything outside this list
# (GH_*, GITHUB_*, AWS_*, …) is dropped so a script's stdout — which
# is fed back to the model verbatim — cannot echo a credential the
# agent should never see. ``OPENROUTER_API_KEY`` is included because
# this codebase's spikes very often need to call OpenRouter to verify
# response shapes; the value itself is redacted from returned output
# (see :func:`_redact`) so an `os.environ` dump cannot leak it.
_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "OPENROUTER_API_KEY")
_REDACT_ENV = ("OPENROUTER_API_KEY",)
_OUTPUT_CAP = 100_000
_PIP_INSTALL_TIMEOUT = 300


def _scrubbed_env(scratch: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env["HOME"] = str(scratch)
    env["TMPDIR"] = str(scratch)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _redact(output: str) -> str:
    """Strip literal occurrences of redacted env var values from ``output``.

    Cheap defence-in-depth: even with a credential in env, the model
    should never see its raw value in tool output.
    """
    for name in _REDACT_ENV:
        value = os.environ.get(name)
        if value:
            output = output.replace(value, f"***{name}***")
    return output


def _scratch_dir(ctx: RunContext) -> Path:
    """Per-workspace scratch dir, derived from the backend's root."""
    repo_root = Path(ctx.deps.backend.root_dir)
    scratch = repo_root.parent / "spike"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _truncate(output: str) -> tuple[str, bool]:
    if len(output) > _OUTPUT_CAP:
        return output[:_OUTPUT_CAP], True
    return output, False


async def spike_run(
    ctx: RunContext,
    script: str,
    pip_install: list[str] | None = None,
    timeout: int = 60,
) -> str:
    """Run a short python script in the workspace scratch dir.

    The script is written verbatim to ``snippet.py`` inside the scratch
    dir and executed with a scrubbed environment. The scratch dir
    persists across calls so a venv built by an earlier call can be
    reused.

    Args:
        script: Python source to run. Print whatever you want to
            observe — the captured stdout+stderr is returned to you.
        pip_install: Packages to install into a scratch venv before
            running the script. The venv is created lazily on first
            request and reused thereafter. Skip this if the script
            only uses the standard library.
        timeout: Maximum wall-clock seconds for the script (default
            60). Pip installs are bounded separately.

    Returns:
        The script's stdout+stderr (capped at ~100KB). On failure the
        return string starts with ``Script failed (exit code N):``.
    """
    scratch = _scratch_dir(ctx)
    env = _scrubbed_env(scratch)

    venv_dir = scratch / ".venv"
    if pip_install:
        if not venv_dir.exists():
            r = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                cwd=str(scratch),
                env=env,
                capture_output=True,
                text=True,
                timeout=_PIP_INSTALL_TIMEOUT,
            )
            if r.returncode != 0:
                return _redact(f"venv creation failed: {r.stderr.strip()}")
        py = str(venv_dir / "bin" / "python")
        r = subprocess.run(
            [py, "-m", "pip", "install", "-q", *pip_install],
            cwd=str(scratch),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PIP_INSTALL_TIMEOUT,
        )
        if r.returncode != 0:
            return _redact(
                f"pip install failed (exit {r.returncode}):\n"
                f"{(r.stdout + r.stderr).strip()}"
            )
        runner = py
    else:
        runner = str(venv_dir / "bin" / "python") if venv_dir.exists() else sys.executable

    (scratch / "snippet.py").write_text(script)

    try:
        result = subprocess.run(
            [runner, "snippet.py"],
            cwd=str(scratch),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Script timed out after {timeout}s"

    output, truncated = _truncate(result.stdout + result.stderr)
    output = _redact(output)
    suffix = "\n... (output truncated)" if truncated else ""
    if result.returncode != 0:
        return f"Script failed (exit code {result.returncode}):\n{output}{suffix}"
    return f"{output}{suffix}"


SPIKE_RUN_TOOL = Tool(spike_run)
