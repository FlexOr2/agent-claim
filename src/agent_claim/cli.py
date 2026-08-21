"""Coordinate coding-agent claims through a repository-neutral GitHub ledger."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import __version__, checkout, discovery, github, protocol

AGENT_CLAIM_AGENT_ENV = checkout.AGENT_CLAIM_AGENT_ENV
CLAUDE_SESSION_ID_ENV = checkout.CLAUDE_SESSION_ID_ENV
GROK_SESSION_ID_ENV = checkout.GROK_SESSION_ID_ENV
GitHubIssueComments = github.GitHubIssueComments
MAX_COMMENT_BYTES = protocol.MAX_COMMENT_BYTES
ActiveClaim = protocol.ActiveClaim
ClaimError = protocol.ClaimError
ClaimRequest = protocol.ClaimRequest
ClaimUnavailable = protocol.ClaimUnavailable
ClaimantRelease = protocol.ClaimantRelease
InvalidClaimMarker = protocol.InvalidClaimMarker
IssueComment = protocol.IssueComment
LEDGER_BODY_MARKER = protocol.LEDGER_BODY_MARKER
LEDGER_LABEL = protocol.LEDGER_LABEL
LedgerSupersede = protocol.LedgerSupersede
LedgerSuperseded = protocol.LedgerSuperseded
PROJECTION_MARKER_PATTERN = protocol.PROJECTION_MARKER_PATTERN
_active_projection = protocol._active_projection
_bounded_command = github._bounded_command
_git_output = checkout._git_output
_projection_ledger = protocol._projection_ledger
_projection_marker = protocol._projection_marker
_repository = checkout._repository
_resolved_agent = checkout._resolved_agent
_unclaimed_projection = protocol._unclaimed_projection
_validate_checkout = checkout._validate_checkout
acquire_claim = protocol.acquire_claim
active_claims = protocol.active_claims
bootstrap_ledger = discovery.bootstrap_ledger
claim_comment = protocol.claim_comment
claim_label = protocol.claim_label
claims_conflict = protocol.claims_conflict
configure_ledger = protocol.configure_ledger
discover_ledger = discovery.discover_ledger
is_protocol_candidate = protocol.is_protocol_candidate
parse_claim_event = protocol.parse_claim_event
reconcile_all_labels = protocol.reconcile_all_labels
reconcile_issue_label = protocol.reconcile_issue_label
release_claim = protocol.release_claim
release_comment = protocol.release_comment
supersede_comment = protocol.supersede_comment
supersede_ledger = protocol.supersede_ledger

POLICY_LOADER = (
    "<!-- agent-claim-policy:v1 -->\n"
    "Before the first edit in a Git repository, use live `agent-claim`: "
    "`status`, then `claim` the issue and write scope. `bootstrap` only when "
    "neither a coordination/claim contract nor a ledger exists. `release` after "
    "landing or abandoning the lane. Missing `gh` or network is a failure, "
    "never coordinated success. Read-only review stays free. Do not invent a "
    "second board."
)
DEFAULT_CLAIM_ROLE = "builder"


def _request(arguments: argparse.Namespace) -> protocol.ClaimRequest:
    agent = checkout._resolved_agent(arguments.agent)
    if arguments.base is None:
        base = checkout._git_output(["rev-parse", "HEAD"])
    else:
        base = arguments.base
    if arguments.branch is None:
        branch = checkout._git_output(["branch", "--show-current"])
    else:
        branch = arguments.branch
    payload: dict[str, object] = {
        "action": "claim",
        "agent": agent,
        "base": base,
        "branch": branch,
        "claim_id": arguments.claim_id or uuid.uuid4().hex,
        "issue": arguments.issue,
        "role": arguments.role,
        "scope": arguments.scope,
    }
    synthetic = protocol.IssueComment(
        1,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        f"{protocol._marker(payload)}\n\nAgent: {agent} ({arguments.role})",
        "OWNER",
        "https://github.com/local/request",
    )
    parsed = protocol.parse_claim_event(synthetic)
    if not isinstance(parsed, protocol.ActiveClaim):
        raise protocol.ClaimError("claim request did not produce a marker")
    request = protocol.ClaimRequest(
        issue=parsed.issue,
        agent=parsed.agent,
        role=parsed.role,
        base=parsed.base,
        branch=parsed.branch,
        scope=parsed.scope,
        claim_id=parsed.claim_id,
    )
    checkout._validate_checkout(request)
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-claim", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("bootstrap", help="create or adopt this repository's locked ledger")

    status = commands.add_parser("status", help="show repository-wide build claims")
    status.add_argument("issue", type=int, nargs="?")
    status.add_argument("--json", action="store_true")

    claim = commands.add_parser("claim", help="claim an issue and scope before editing")
    claim.add_argument("issue", type=int)
    claim.add_argument("--agent")
    claim.add_argument("--role", default=DEFAULT_CLAIM_ROLE)
    claim.add_argument("--base")
    claim.add_argument("--branch")
    claim.add_argument("--scope", action="append", required=True)
    claim.add_argument("--claim-id")

    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument("issue", type=int)
    release.add_argument("--agent")
    release.add_argument("--role")
    release.add_argument("--reason")
    release.add_argument("--claim-id")
    release.add_argument("--coordinator-override", action="store_true")

    reconcile = commands.add_parser("reconcile", help="repair claimed-label projections")
    reconcile.add_argument("issue", type=int, nargs="?")

    supersede = commands.add_parser(
        "supersede", help="atomically freeze a drained ledger for its successor"
    )
    supersede.add_argument("successor_issue", type=int)
    supersede.add_argument("--agent", required=True)
    supersede.add_argument("--role", required=True)
    supersede.add_argument("--reason", required=True)
    supersede.add_argument("--claim-id", required=True)

    policy = commands.add_parser("policy", help="print the provider-neutral loader block")
    policy.add_argument("--print", action="store_true", required=True, dest="print_loader")
    commands.add_parser(
        "protect", help="deny PreToolUse writes without this session's live claim"
    )
    return parser


def _status_claims(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None
) -> tuple[tuple[protocol.ActiveClaim, ...], set[str]]:
    selected = tuple(claim for claim in claims if issue is None or claim.issue == issue)
    if not selected:
        return (), set()
    index = protocol._claim_conflict_index(claims)
    related_ids = (
        {claim.claim_id for claim in claims}
        if issue is None
        else protocol._related_claim_ids(index, selected)
    )
    related = tuple(claim for claim in claims if claim.claim_id in related_ids)
    return related, index.conflict_ids


def _status(claims: tuple[protocol.ActiveClaim, ...], issue: int | None) -> int:
    related, conflict_ids = _status_claims(claims, issue)
    if not related:
        subject = "repository" if issue is None else f"issue #{issue}"
        print(f"UNCLAIMED {subject}")
        return 0
    for claim in related:
        state = "CONFLICT" if claim.claim_id in conflict_ids else "CLAIMED"
        print(
            f"{state} issue #{claim.issue}: {claim.agent} ({claim.role}) "
            f"base={claim.base} branch={claim.branch} claim={claim.claim_id}"
        )
        for path in claim.scope:
            print(f"  {path}")
    return 2 if any(claim.claim_id in conflict_ids for claim in related) else 0


def _status_json(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None, ledger: int
) -> int:
    related, conflict_ids = _status_claims(claims, issue)
    if not related:
        state = "UNCLAIMED"
    elif any(claim.claim_id in conflict_ids for claim in related):
        state = "CONFLICT"
    else:
        state = "CLAIMED"
    payload = {
        "ledger": ledger,
        "issue": issue,
        "state": state,
        "claims": [
            {
                "issue": claim.issue,
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                "state": "CONFLICT" if claim.claim_id in conflict_ids else "CLAIMED",
            }
            for claim in related
        ],
    }
    print(json.dumps(payload))
    return 2 if state == "CONFLICT" else 0


MUTATING_HOOK_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "search_replace", "write"})


def _hook_allow() -> int:
    print(json.dumps({"decision": "allow"}))
    return 0


def _hook_deny(reason: str) -> int:
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 2


def _hook_payload() -> dict[str, object] | None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _hook_field(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _hook_path(tool_input: dict[str, object]) -> str | None:
    for key in ("path", "file_path", "filePath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _protect_relative_path(raw_path: str) -> str | None:
    toplevel = Path(checkout._git_output(["rev-parse", "--show-toplevel"])).resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        relative = candidate.resolve().relative_to(toplevel).as_posix()
        return protocol._valid_scope([relative])[0]
    except (protocol.InvalidClaimMarker, OSError, ValueError):
        return None


def _protect_write(repository: str | None, payload: dict[str, object]) -> int:
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return _hook_deny("path required")
    raw_path = _hook_path(tool_input)
    if raw_path is None:
        return _hook_deny("path required")
    agent = checkout._resolved_agent(None)
    branch = checkout._git_output(["branch", "--show-current"])
    if branch in {"main", "master"}:
        return _hook_deny("not main")
    git_directory = Path(checkout._git_output(["rev-parse", "--git-dir"])).resolve()
    common_directory = Path(checkout._git_output(["rev-parse", "--git-common-dir"])).resolve()
    if git_directory == common_directory:
        return _hook_deny("worktree")
    relative = _protect_relative_path(raw_path)
    if relative is None:
        return _hook_deny("path required")
    client = github.GitHubIssueComments(checkout._repository(repository))
    ledger = discovery.discover_ledger(client)
    if ledger is None:
        return _hook_deny("claim first")
    protocol.configure_ledger(ledger)
    for claim in protocol._ledger_claims(client):
        if (
            claim.agent == agent
            and claim.branch == branch
            and protocol._scopes_overlap(claim.scope, (relative,))
        ):
            return _hook_allow()
    return _hook_deny("claim first")


def _protect(repository: str | None) -> int:
    # Grok fail-opens on crash or non-JSON hook output; deny instead of raising.
    try:
        payload = _hook_payload()
        if payload is None:
            return _hook_deny("invalid hook payload")
        tool_name = _hook_field(payload, "toolName", "tool_name")
        if not isinstance(tool_name, str):
            return _hook_deny("invalid hook payload")
        if tool_name not in MUTATING_HOOK_TOOLS:
            return _hook_allow()
        return _protect_write(repository, payload)
    except Exception as error:
        return _hook_deny(str(error))


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    if parsed.command == "policy":
        print(POLICY_LOADER)
        return 0
    if parsed.command == "protect":
        return _protect(parsed.repo)
    try:
        if parsed.command in {"claim", "release"}:
            parsed.agent = checkout._resolved_agent(parsed.agent)
        release_branch: str | None = None
        if parsed.command == "release":
            if parsed.coordinator_override:
                protocol._require_coordinator_override(parsed.role, parsed.reason)
            if parsed.claim_id is None:
                release_branch = checkout._git_output(["branch", "--show-current"])
                if not release_branch:
                    raise protocol.ClaimUnavailable(
                        "release without --claim-id requires a non-empty current branch; "
                        "pass --claim-id"
                    )
        client = github.GitHubIssueComments(checkout._repository(parsed.repo))
        if parsed.command == "bootstrap":
            ledger = discovery.bootstrap_ledger(client)
            protocol.configure_ledger(ledger)
            print(f"LEDGER #{ledger}")
            return 0
        ledger = discovery.discover_ledger(client)
        if ledger is None:
            raise protocol.ClaimUnavailable(
                "no agent-claim ledger exists; run agent-claim bootstrap"
            )
        protocol.configure_ledger(ledger)
        if parsed.command == "status":
            claims = protocol._ledger_claims(client)
            if parsed.json:
                return _status_json(claims, parsed.issue, ledger)
            print(f"LEDGER #{ledger}")
            return _status(claims, parsed.issue)
        if parsed.command == "claim":
            claimed = protocol.acquire_claim(client, _request(parsed))
            print(f"CLAIMED issue #{parsed.issue}: {claimed.claim_id} {claimed.comment.url}")
            return 0
        if parsed.command == "release":
            released = protocol.release_claim(
                client,
                parsed.issue,
                parsed.agent,
                parsed.role,
                parsed.reason,
                parsed.claim_id,
                branch=release_branch,
                coordinator_override=parsed.coordinator_override,
            )
            print(f"RELEASED issue #{parsed.issue}: {released.claim_id}")
            return 0
        if parsed.command == "supersede":
            frozen = protocol.supersede_ledger(
                client,
                parsed.successor_issue,
                parsed.agent,
                parsed.role,
                parsed.reason,
                parsed.claim_id,
            )
            print(
                f"SUPERSEDED ledger #{protocol.LEDGER_ISSUE} successor "
                f"#{parsed.successor_issue}: {frozen.claim_id}"
            )
            return 0
        if parsed.issue is None:
            reconciled = protocol.reconcile_all_labels(client)
        else:
            protocol.reconcile_issue_label(client, parsed.issue)
            reconciled = tuple(
                claim.issue
                for claim in protocol._ledger_claims(client)
                if claim.issue == parsed.issue
            )
        print("RECONCILED " + (", ".join(f"#{issue}" for issue in reconciled) or "no claims"))
        return 0
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
