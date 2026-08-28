"""Coordinate coding-agent claims through a repository-neutral GitHub ledger."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, board, checkout, discovery, github, protocol

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
DuplicateClaimConflict = protocol.DuplicateClaimConflict
DuplicateClaimRepair = protocol.DuplicateClaimRepair
InvalidClaimMarker = protocol.InvalidClaimMarker
IssueComment = protocol.IssueComment
IssueIdentity = protocol.IssueIdentity
LaneIdentity = protocol.LaneIdentity
ISSUELESS_LANE_BRANCH_PREFIXES = protocol.ISSUELESS_LANE_BRANCH_PREFIXES
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
claims_holding_path = protocol.claims_holding_path
configure_ledger = protocol.configure_ledger
discover_ledger = discovery.discover_ledger
is_protocol_candidate = protocol.is_protocol_candidate
parse_claim_event = protocol.parse_claim_event
reconcile_all_labels = protocol.reconcile_all_labels
reconcile_issue_label = protocol.reconcile_issue_label
repair_duplicate_claims = protocol.repair_duplicate_claims
release_claim = protocol.release_claim
rescope_claim = protocol.rescope_claim
release_comment = protocol.release_comment
rescope_comment = protocol.rescope_comment
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


def _resolved_identity(issue: int | None, branch: str) -> protocol.ClaimIdentity:
    """Resolve the CLI's discriminated identity: an explicit issue, or a lane.

    Omitting the positional issue number means lane mode, derived from `branch`
    (the same checkout branch `--base`/`--branch` auto-fill and the release
    branch-matching fallback already use). Lane mode is refused outright unless
    `branch` follows the issueless-lane convention, so a builder who simply forgot
    the issue number never gets a silent, unlabeled, non-projected lane claim.
    """
    if issue is not None:
        return protocol.IssueIdentity(issue)
    if not branch.startswith(protocol.ISSUELESS_LANE_BRANCH_PREFIXES):
        prefixes = " or ".join(repr(prefix) for prefix in protocol.ISSUELESS_LANE_BRANCH_PREFIXES)
        raise protocol.ClaimError(
            f"branch {branch!r} is not an issueless lane; pass an issue number, or "
            f"check out a branch prefixed {prefixes}"
        )
    return protocol.LaneIdentity()


def _claim_subject(claim: protocol.ActiveClaim) -> str:
    return (
        f"lane {claim.branch}"
        if isinstance(claim.identity, protocol.LaneIdentity)
        else f"issue #{claim.identity.issue}"
    )


def _reject_directory_scopes(paths: tuple[str, ...], reason: str | None) -> None:
    directories = checkout._scope_directories(paths)
    if not directories:
        return
    if reason is None:
        raise protocol.ClaimError(
            f"directory scope {directories[0]!r} locks a whole tree; "
            "pass --allow-directory REASON, or claim files instead"
        )
    protocol._outbound_text(reason, "allow-directory reason", maximum=512)


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
    identity = _resolved_identity(arguments.issue, branch)
    payload: dict[str, object] = {
        "action": "claim",
        "agent": agent,
        "base": base,
        "branch": branch,
        "claim_id": arguments.claim_id or uuid.uuid4().hex,
        protocol._identity_marker_key(identity): protocol._identity_marker_value(identity),
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
        identity=parsed.identity,
        agent=parsed.agent,
        role=parsed.role,
        base=parsed.base,
        branch=parsed.branch,
        scope=parsed.scope,
        claim_id=parsed.claim_id,
        out_of_order_reason=arguments.out_of_order,
    )
    checkout._validate_checkout(request)
    _reject_directory_scopes(request.scope, getattr(arguments, "allow_directory", None))
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

    board_command = commands.add_parser("board", help="project the open work board without writes")
    board_command.add_argument("--json", action="store_true")

    next_command = commands.add_parser("next", help="show the highest-scored actionable item")
    next_command.add_argument("--json", action="store_true")

    claim = commands.add_parser("claim", help="claim an issue and scope before editing")
    claim.add_argument(
        "issue",
        type=int,
        nargs="?",
        help="omit for lane mode, derived from a docs/ or fix/ checkout branch",
    )
    claim.add_argument("--agent")
    claim.add_argument("--role", default=DEFAULT_CLAIM_ROLE)
    claim.add_argument("--base")
    claim.add_argument("--branch")
    claim.add_argument(
        "--scope",
        action="append",
        required=True,
        help="repository-relative path; comma-joined values equal repeated --scope",
    )
    claim.add_argument("--claim-id")
    claim.add_argument(
        "--out-of-order",
        metavar="REASON",
        help="record why this claim proceeds ahead of a higher-scored actionable item",
    )
    claim.add_argument(
        "--allow-directory",
        metavar="REASON",
        help="permit a directory scope that locks a whole tree",
    )
    claim.add_argument("--json", action="store_true")

    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=int,
        nargs="?",
        help="omit for lane mode, derived from a docs/ or fix/ checkout branch",
    )
    release.add_argument("--agent")
    release.add_argument("--role")
    release.add_argument("--reason")
    release.add_argument("--claim-id")
    release.add_argument("--coordinator-override", action="store_true")
    release.add_argument("--json", action="store_true")

    rescope = commands.add_parser(
        "rescope", help="add or drop paths on a live claim without releasing"
    )
    rescope.add_argument(
        "issue",
        type=int,
        nargs="?",
        help="omit for lane mode, derived from a docs/ or fix/ checkout branch",
    )
    rescope.add_argument("--agent")
    rescope.add_argument(
        "--add",
        action="append",
        help="repository-relative path to add; comma-joined values equal repeated --add",
    )
    rescope.add_argument(
        "--drop",
        action="append",
        help="repository-relative path to drop; comma-joined values equal repeated --drop",
    )
    rescope.add_argument("--claim-id")
    rescope.add_argument(
        "--allow-directory",
        metavar="REASON",
        help="permit adding a directory scope that locks a whole tree",
    )
    rescope.add_argument("--json", action="store_true")

    who = commands.add_parser("who", help="show which live claim holds a path")
    who.add_argument("path")
    who.add_argument("--json", action="store_true")

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


def _identity_json(identity: protocol.ClaimIdentity) -> dict[str, object]:
    """`issue`/`lane` pair for one claim's discriminated identity, for JSON output.

    A lane claim's name lives in the sibling `branch` field of the same JSON
    object, so `lane` stays a bare marker instead of duplicating it.
    """
    if isinstance(identity, protocol.LaneIdentity):
        return {"issue": None, "lane": True}
    return {"issue": identity.issue, "lane": None}


def _status_claims(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None
) -> tuple[tuple[protocol.ActiveClaim, ...], set[str]]:
    selected = tuple(
        claim
        for claim in claims
        if issue is None
        or (isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue)
    )
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
            f"{state} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
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
                **_identity_json(claim.identity),
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


def _who(claims: tuple[protocol.ActiveClaim, ...], path: str) -> int:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return 0
    state = "CONFLICT" if len(holders) > 1 else "CLAIMED"
    for claim in holders:
        print(
            f"{state} {path} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
    return 2 if state == "CONFLICT" else 0


def _who_json(
    claims: tuple[protocol.ActiveClaim, ...], path: str, ledger: int
) -> int:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        state = "UNCLAIMED"
    elif len(holders) > 1:
        state = "CONFLICT"
    else:
        state = "CLAIMED"
    payload = {
        "ledger": ledger,
        "path": path,
        "state": state,
        "claims": [
            {
                **_identity_json(claim.identity),
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                "state": "CONFLICT" if len(holders) > 1 else "CLAIMED",
            }
            for claim in holders
        ],
    }
    print(json.dumps(payload))
    return 2 if state == "CONFLICT" else 0


def _rescope_json(claimed: protocol.ActiveClaim) -> int:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
            }
        )
    )
    return 0


def _claim_json(claimed: protocol.ActiveClaim) -> int:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "url": claimed.comment.url,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
            }
        )
    )
    return 0


def _release_json(
    released: protocol.ActiveClaim, agent: str, role: str | None, reason: str | None
) -> int:
    print(
        json.dumps(
            {
                **_identity_json(released.identity),
                "branch": released.branch,
                "claim_id": released.claim_id,
                "agent": agent,
                "role": role if role is not None else released.role,
                "reason": reason if reason is not None else protocol.DEFAULT_RELEASE_REASON,
            }
        )
    )
    return 0


def _board(
    client: github.GitHubIssueComments, claims: tuple[protocol.ActiveClaim, ...]
) -> board.Board:
    now = datetime.now(timezone.utc)
    toplevel = Path(checkout._git_output(["rev-parse", "--show-toplevel"]))
    return board.build_board(
        client.list_open_board_issues(),
        client.list_open_board_pull_requests(),
        client.list_recent_merged_board_pull_requests(now - timedelta(days=14)),
        claims,
        board.load_config(toplevel / ".agent-claim" / "board.toml"),
        now=now,
    )


def _next_json(item: board.BoardItem | None, skipped: tuple[board.BoardItem, ...]) -> int:
    payload: dict[str, object] = {
        "skipped": [
            {"number": skipped_item.number, "reason": skipped_item.actionable_reason}
            for skipped_item in skipped
        ]
    }
    if item is not None:
        payload.update(
            {
                "number": item.number,
                "score": item.score,
                "title": item.title,
                "next": item.contract.next,
            }
        )
    print(json.dumps(payload))
    return 0


def _next(item: board.BoardItem | None, skipped: tuple[board.BoardItem, ...]) -> int:
    lines = (
        [f"#{item.number} score {item.score}: {item.title}", f"Next: {item.contract.next}"]
        if item is not None
        else ["No actionable item."]
    )
    if skipped:
        skipped_lines = (
            f"#{skipped_item.number}: {skipped_item.actionable_reason}"
            for skipped_item in skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))
    return 0


def _proposed_expectations(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(
        item
        for item in projected.items
        if item.expectation_state is board.ExpectationState.PROPOSED
    )


def _out_of_order_warning(
    projected: board.Board, issue: int | None
) -> str | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or highest.score <= claimed_item.score:
        return None
    return (
        f"WARNING: higher-scored actionable item #{highest.number} "
        f"(score {highest.score}) is free: {highest.title}"
    )


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
        if parsed.command in {"claim", "release", "rescope"}:
            parsed.agent = checkout._resolved_agent(parsed.agent)
        release_branch: str | None = None
        if parsed.command == "release":
            if parsed.coordinator_override:
                protocol._require_coordinator_override(parsed.role, parsed.reason)
            if parsed.issue is None or parsed.claim_id is None:
                release_branch = checkout._git_output(["branch", "--show-current"])
                if not release_branch:
                    if parsed.issue is None:
                        raise protocol.ClaimUnavailable(
                            "lane release requires a non-empty current branch; "
                            "check out the docs/ or fix/ lane branch, or pass "
                            "an issue number"
                        )
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
        if parsed.command == "board":
            projected = _board(client, protocol._ledger_claims(client))
            print(board.board_json(projected) if parsed.json else board.render(projected))
            return 0
        if parsed.command == "next":
            projected = _board(client, protocol._ledger_claims(client))
            item = board.highest_scored_actionable(projected)
            skipped = _proposed_expectations(projected)
            if item is None:
                if skipped:
                    if parsed.json:
                        _next_json(None, skipped)
                    else:
                        _next(None, skipped)
                return 3
            return _next_json(item, skipped) if parsed.json else _next(item, skipped)
        if parsed.command == "who":
            claims = protocol._ledger_claims(client)
            if parsed.json:
                return _who_json(claims, parsed.path, ledger)
            print(f"LEDGER #{ledger}")
            return _who(claims, parsed.path)
        if parsed.command == "rescope":
            rescope_branch = checkout._git_output(["branch", "--show-current"])
            if not rescope_branch:
                raise protocol.ClaimUnavailable(
                    "rescope requires a non-empty current branch; "
                    "check out the claim branch, or pass an issue number"
                )
            checkout._validate_worktree_branch(rescope_branch)
            identity = _resolved_identity(parsed.issue, rescope_branch)
            add = protocol._valid_scope(parsed.add) if parsed.add else ()
            drop = protocol._valid_scope(parsed.drop) if parsed.drop else ()
            _reject_directory_scopes(add, parsed.allow_directory)
            rescoped = protocol.rescope_claim(
                client,
                identity,
                parsed.agent,
                add,
                drop,
                parsed.claim_id,
                branch=rescope_branch,
            )
            if parsed.json:
                return _rescope_json(rescoped)
            print(f"RESCOPED {_claim_subject(rescoped)}: {rescoped.claim_id}")
            return 0
        if parsed.command == "claim":
            requested = _request(parsed)
            warning = None
            if isinstance(requested.identity, protocol.IssueIdentity):
                warning = _out_of_order_warning(
                    _board(client, protocol._ledger_claims(client)), requested.identity.issue
                )
            if warning is not None:
                print(warning, file=sys.stderr if parsed.json else sys.stdout)
            claimed = protocol.acquire_claim(client, requested)
            if parsed.json:
                return _claim_json(claimed)
            print(f"CLAIMED {_claim_subject(claimed)}: {claimed.claim_id} {claimed.comment.url}")
            return 0
        if parsed.command == "release":
            identity = _resolved_identity(parsed.issue, release_branch or "")
            released = protocol.release_claim(
                client,
                identity,
                parsed.agent,
                parsed.role,
                parsed.reason,
                parsed.claim_id,
                branch=release_branch,
                coordinator_override=parsed.coordinator_override,
            )
            if parsed.json:
                return _release_json(released, parsed.agent, parsed.role, parsed.reason)
            print(f"RELEASED {_claim_subject(released)}: {released.claim_id}")
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
        try:
            for repair in protocol.repair_duplicate_claims(client):
                superseded = ", ".join(f"#{cid}" for cid in repair.superseded_comment_ids)
                print(
                    f"REPAIRED claim {repair.claim_id!r}: superseded {superseded} "
                    f"-> survivor #{repair.survivor_comment_id}"
                )
        except protocol.LedgerSuperseded:
            # A frozen ledger has nothing left for duplicate repair to fix; let the
            # label reconciliation below observe the freeze and run its own cleanup.
            pass
        if parsed.issue is None:
            reconciled = protocol.reconcile_all_labels(client)
        else:
            protocol.reconcile_issue_label(client, parsed.issue)
            reconciled = tuple(
                claim.identity.issue
                for claim in protocol._ledger_claims(client)
                if isinstance(claim.identity, protocol.IssueIdentity)
                and claim.identity.issue == parsed.issue
            )
        print("RECONCILED " + (", ".join(f"#{issue}" for issue in reconciled) or "no claims"))
        return 0
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
