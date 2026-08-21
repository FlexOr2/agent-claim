"""Local git checkout validation and agent identity."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .protocol import REPOSITORY_PATTERN, ClaimError, ClaimRequest, _outbound_text

GH_TIMEOUT_SECONDS = 60
AGENT_CLAIM_AGENT_ENV = "AGENT_CLAIM_AGENT"
GROK_SESSION_ID_ENV = "GROK_SESSION_ID"
CLAUDE_SESSION_ID_ENV = "CLAUDE_SESSION_ID"


def _repository(explicit: str | None) -> str:
    if explicit:
        repository = explicit
    else:
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                check=False,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as error:
            raise ClaimError("gh is required for issue claims") from error
        except subprocess.TimeoutExpired as error:
            raise ClaimError("gh timed out while resolving the repository") from error
        if result.returncode == 0 and result.stdout.strip():
            repository = result.stdout.strip()
        else:
            remote = _git_output(["config", "--get", "remote.origin.url"])
            match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote)
            if match is None:
                raise ClaimError("cannot resolve GitHub repository; pass --repo OWNER/REPO")
            repository = f"{match.group(1)}/{match.group(2)}"
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ClaimError("repository must be OWNER/REPO")
    return repository


def _git_output(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise ClaimError("git is required for issue claims") from error
    except subprocess.TimeoutExpired as error:
        raise ClaimError("git timed out while validating the build checkout") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git failure"
        raise ClaimError(detail)
    return result.stdout.strip()


def _validate_checkout(request: ClaimRequest) -> None:
    if request.branch in {"main", "master"}:
        raise ClaimError("build claims require an isolated non-main worktree branch")
    head = _git_output(["rev-parse", "HEAD"])
    branch = _git_output(["branch", "--show-current"])
    git_directory = Path(_git_output(["rev-parse", "--git-dir"])).resolve()
    common_directory = Path(_git_output(["rev-parse", "--git-common-dir"])).resolve()
    dirty = _git_output(["status", "--porcelain"])
    if head != request.base:
        raise ClaimError(
            f"claim base {request.base} does not match checkout HEAD {head}"
        )
    if branch != request.branch:
        raise ClaimError(
            f"claim branch {request.branch!r} does not match checkout branch {branch!r}"
        )
    if git_directory == common_directory:
        raise ClaimError("build claims require a linked isolated worktree checkout")
    if dirty:
        raise ClaimError("claim must be acquired before the first worktree edit")


def _resolved_agent(explicit: str | None) -> str:
    if explicit is not None:
        return _outbound_text(explicit, "agent", maximum=128)
    configured = os.environ.get(AGENT_CLAIM_AGENT_ENV)
    if configured:
        return _outbound_text(configured, "agent", maximum=128)
    grok_session = os.environ.get(GROK_SESSION_ID_ENV)
    if grok_session:
        return _outbound_text(f"Grok {grok_session}", "agent", maximum=128)
    claude_session = os.environ.get(CLAUDE_SESSION_ID_ENV)
    if claude_session:
        return _outbound_text(f"Claude {claude_session}", "agent", maximum=128)
    raise ClaimError(
        "agent identity is required: pass --agent or set "
        f"{AGENT_CLAIM_AGENT_ENV}, {GROK_SESSION_ID_ENV}, or {CLAUDE_SESSION_ID_ENV}"
    )
