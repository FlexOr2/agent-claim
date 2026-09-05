"""Local git checkout validation and agent identity."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from . import process
from .protocol import ClaimError, ClaimRequest, _outbound_text

# One canonical source (process.DEFAULT_TIMEOUT_SECONDS); kept as a name here
# because callers -- and tests -- read it as `checkout.GH_TIMEOUT_SECONDS`.
GH_TIMEOUT_SECONDS = process.DEFAULT_TIMEOUT_SECONDS
AGENT_CLAIM_AGENT_ENV = "AGENT_CLAIM_AGENT"
GROK_SESSION_ID_ENV = "GROK_SESSION_ID"
CLAUDE_SESSION_ID_ENV = "CLAUDE_SESSION_ID"


def _git_output(arguments: list[str]) -> str:
    try:
        result = process.run_captured(["git", *arguments], timeout=GH_TIMEOUT_SECONDS)
    except process.ExecutableMissingError as error:
        raise ClaimError("git is required for issue claims") from error
    except process.ProcessTimedOutError as error:
        raise ClaimError("git timed out while validating the build checkout") from error
    if result.exit_status != 0:
        detail = (
            result.stderr.decode().strip()
            or result.stdout.decode().strip()
            or "unknown git failure"
        )
        raise ClaimError(detail)
    return result.stdout.decode().strip()


def origin_remote_url() -> str:
    """The checkout's `origin` remote, for the GitHub-remote fallback in
    `github.discover_repository` when `gh` cannot itself resolve a repository."""
    return _git_output(["config", "--get", "remote.origin.url"])


def versioned_paths() -> tuple[str, ...]:
    try:
        result = process.run_captured(
            ["git", "ls-files", "-z", "--full-name"], timeout=GH_TIMEOUT_SECONDS
        )
    except process.ExecutableMissingError as error:
        raise ClaimError("git is required for issue claims") from error
    except process.ProcessTimedOutError as error:
        raise ClaimError("git timed out while validating the build checkout") from error
    if result.exit_status != 0:
        detail = (
            result.stderr.decode().strip()
            or result.stdout.decode().strip()
            or "unknown git failure"
        )
        raise ClaimError(detail)
    return tuple(dict.fromkeys(path for path in result.stdout.decode().split("\0") if path))


def paths_under_scope(paths: tuple[str, ...], scope: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            path
            for path in paths
            if any(path == entry or path.startswith(f"{entry}/") for entry in scope)
        )
    )


def _scope_directories(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the scope entries that name a git tree or on-disk directory."""
    directories: list[str] = []
    toplevel: str | None = None
    for path in paths:
        try:
            kind = _git_output(["cat-file", "-t", f"HEAD:{path}"])
        except ClaimError:
            kind = ""
        if kind == "tree":
            directories.append(path)
            continue
        if toplevel is None:
            try:
                toplevel = _git_output(["rev-parse", "--show-toplevel"])
            except ClaimError:
                toplevel = ""
        if toplevel and (Path(toplevel) / path).is_dir():
            directories.append(path)
    return tuple(directories)


def _validate_worktree_branch(branch: str) -> None:
    """Require an isolated non-main worktree checked out on `branch`.

    Rescope uses this without also binding HEAD to the claim base or requiring
    a clean tree, so a lane can sharpen scope after it has already committed.
    """
    if branch in {"main", "master"}:
        raise ClaimError("build claims require an isolated non-main worktree branch")
    current = _git_output(["branch", "--show-current"])
    git_directory = Path(_git_output(["rev-parse", "--git-dir"])).resolve()
    common_directory = Path(_git_output(["rev-parse", "--git-common-dir"])).resolve()
    if current != branch:
        raise ClaimError(f"claim branch {branch!r} does not match checkout branch {current!r}")
    if git_directory == common_directory:
        raise ClaimError("build claims require a linked isolated worktree checkout")


def _validate_checkout(request: ClaimRequest) -> None:
    head = _git_output(["rev-parse", "HEAD"])
    if head != request.base:
        raise ClaimError(f"claim base {request.base} does not match checkout HEAD {head}")
    _validate_worktree_branch(request.branch)
    dirty = _git_output(["status", "--porcelain"])
    if dirty:
        raise ClaimError("claim must be acquired before the first worktree edit")


def _trunk_ref() -> str:
    try:
        symbolic = _git_output(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
        if symbolic:
            return symbolic
    except ClaimError:
        pass
    for candidate in (
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
        "main",
        "master",
    ):
        try:
            _git_output(["rev-parse", "--verify", candidate])
            return candidate
        except ClaimError:
            continue
    raise ClaimError("cannot determine the main branch for ruling age")


def trunk_landing_times() -> tuple[datetime, ...]:
    """Committer times of first-parent landings on the default branch, oldest first.

    A merge counts once. Using the default branch — never the work branch — is the
    contract: a ruling ages with trunk, not with local commits.
    """
    raw = _git_output(["log", "--first-parent", "--reverse", "--format=%cI", _trunk_ref()])
    if not raw:
        return ()
    times: list[datetime] = []
    for line in raw.splitlines():
        try:
            parsed = datetime.fromisoformat(line)
        except ValueError as error:
            raise ClaimError("git returned a malformed trunk landing timestamp") from error
        if parsed.tzinfo is None:
            raise ClaimError("git returned a malformed trunk landing timestamp")
        times.append(parsed.astimezone(UTC))
    return tuple(times)


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
