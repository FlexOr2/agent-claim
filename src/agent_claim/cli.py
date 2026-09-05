"""Coordinate coding-agent claims through a repository-neutral GitHub ledger."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, board, checkout, discovery, forge, github, protocol

AGENT_CLAIM_AGENT_ENV = checkout.AGENT_CLAIM_AGENT_ENV
CLAUDE_SESSION_ID_ENV = checkout.CLAUDE_SESSION_ID_ENV
GROK_SESSION_ID_ENV = checkout.GROK_SESSION_ID_ENV
MAX_COMMENT_BYTES = protocol.MAX_COMMENT_BYTES
ActiveClaim = protocol.ActiveClaim
ClaimError = protocol.ClaimError
ClaimRequest = protocol.ClaimRequest
ClaimUnavailableError = protocol.ClaimUnavailableError
ClaimantRelease = protocol.ClaimantRelease
DuplicateClaimConflictError = protocol.DuplicateClaimConflictError
DuplicateClaimRepair = protocol.DuplicateClaimRepair
InvalidClaimMarkerError = protocol.InvalidClaimMarkerError
IssueComment = protocol.IssueComment
IssueIdentity = protocol.IssueIdentity
LaneIdentity = protocol.LaneIdentity
ISSUELESS_LANE_BRANCH_PREFIXES = protocol.ISSUELESS_LANE_BRANCH_PREFIXES
LEDGER_BODY_MARKER = protocol.LEDGER_BODY_MARKER
LEDGER_LABEL = protocol.LEDGER_LABEL
LedgerSupersede = protocol.LedgerSupersede
LedgerSupersededError = protocol.LedgerSupersededError
PROJECTION_MARKER_PATTERN = protocol.PROJECTION_MARKER_PATTERN
_active_projection = protocol._active_projection
_git_output = checkout._git_output
_projection_ledger = protocol._projection_ledger
_projection_marker = protocol._projection_marker
_resolved_agent = checkout._resolved_agent
_timestamp = board._timestamp
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
NEXT_PULL_DESCRIPTION = (
    "Pulling is not dispatching: an item whose expectations are still unruled is "
    "named here with refining as its first step, while dispatching a builder onto "
    "it waits for the operator's ruling."
)
WHOLE_HELP = (
    "one sentence why this wide scope does not split; required for more than "
    "three paths, any directory, or more than a quarter of versioned files"
)
WIDE_SCOPE_REFUSAL = (
    "scope is wide: more than three paths, a directory, or more than a quarter "
    "of versioned files; pass --whole REASON"
)


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


def _claim_age_fields(claim: protocol.ActiveClaim, now: datetime) -> tuple[str, bool]:
    age = board.claim_age(claim.comment.created_at, now)
    return board.format_claim_age(age), board.claim_is_old(age)


def _claim_age_suffix(claim: protocol.ActiveClaim, now: datetime) -> str:
    rendered, old = _claim_age_fields(claim, now)
    return f" {rendered} old" if old else f" {rendered}"


def _scope_cost(versioned: tuple[str, ...], scope: tuple[str, ...]) -> tuple[int, int, float]:
    n = len(checkout.paths_under_scope(versioned, scope))
    total = len(versioned)
    share = 0.0 if total == 0 else n / total
    return n, total, share


def _optional_whole_reason(arguments: argparse.Namespace) -> str | None:
    raw = getattr(arguments, "whole", None)
    if raw is None:
        return None
    return protocol._outbound_text(raw, "whole reason", maximum=512)


def _reject_wide_scope(
    scope: tuple[str, ...],
    versioned: tuple[str, ...],
    whole_reason: str | None,
) -> tuple[int, int, float]:
    n, total, share = _scope_cost(versioned, scope)
    directories = checkout._scope_directories(scope)
    if (
        protocol.scope_is_wide(
            scope,
            directories=directories,
            covered_file_count=n,
            versioned_file_count=total,
        )
        and whole_reason is None
    ):
        raise protocol.ClaimError(WIDE_SCOPE_REFUSAL)
    return n, total, share


def _touch_json(claim: protocol.ActiveClaim) -> dict[str, object]:
    return {
        **_identity_json(claim.identity),
        "claim_id": claim.claim_id,
        "agent": claim.agent,
        "scope": list(claim.scope),
    }


def _touch_summary(touches: tuple[protocol.ActiveClaim, ...]) -> str:
    if not touches:
        return "overlaps no other open claims"
    return "overlaps " + ", ".join(
        f"{_claim_subject(claim)} ({claim.claim_id})" for claim in touches
    )


def _claim_cost_line(n: int, total: int, touches: tuple[protocol.ActiveClaim, ...]) -> str:
    percent = 0 if total == 0 else round(100 * n / total)
    return f"{n} of {total} versioned files ({percent}%); {_touch_summary(touches)}"


def _request(arguments: argparse.Namespace) -> protocol.ClaimRequest:
    agent = checkout._resolved_agent(arguments.agent)
    base = checkout._git_output(["rev-parse", "HEAD"]) if arguments.base is None else arguments.base
    if arguments.branch is None:
        branch = checkout._git_output(["branch", "--show-current"])
    else:
        branch = arguments.branch
    issue = _optional_issue_number(arguments.issue)
    identity = _resolved_identity(issue, branch)
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
    whole_reason = _optional_whole_reason(arguments)
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        resource = protocol._outbound_resource_name(resource)
    request = protocol.ClaimRequest(
        identity=parsed.identity,
        agent=parsed.agent,
        role=parsed.role,
        base=parsed.base,
        branch=parsed.branch,
        scope=parsed.scope,
        claim_id=parsed.claim_id,
        out_of_order_reason=arguments.out_of_order,
        whole_reason=whole_reason,
        resource=resource,
    )
    checkout._validate_checkout(request)
    return request


LANE_ISSUE_HELP = "omit for lane mode, derived from a docs/ or fix/ checkout branch"


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

    rulings_command = commands.add_parser(
        "rulings", help="list open expectation lines without writes"
    )
    rulings_command.add_argument("--json", action="store_true")

    next_command = commands.add_parser(
        "next",
        help="name the board's top-priority item to pull",
        description=NEXT_PULL_DESCRIPTION,
    )
    next_command.add_argument("--json", action="store_true")

    claim = commands.add_parser("claim", help="claim an issue and scope before editing")
    claim.add_argument(
        "issue",
        type=int,
        nargs="?",
        help=LANE_ISSUE_HELP,
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
        help=(
            "refuses a claim without a reason when a higher-priority actionable item is "
            "free; records why"
        ),
    )
    claim.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    claim.add_argument(
        "--resource",
        metavar="NAME",
        help="allocate the next free value of this named scarce resource and hold it",
    )
    claim.add_argument("--json", action="store_true")

    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=int,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    release.add_argument("--agent")
    release.add_argument("--role")
    outcome = release.add_mutually_exclusive_group(required=True)
    outcome.add_argument(
        "--merged",
        type=int,
        metavar="PULL_REQUEST",
        help="the pull request that landed this claim's item on the default branch",
    )
    outcome.add_argument(
        "--abandoned",
        metavar="REASON",
        help="why this claim ends without a landing",
    )
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
        help=LANE_ISSUE_HELP,
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
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
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

    pull_request_check = commands.add_parser(
        "pr-check",
        help="check a pull request's typed work-item classification before it merges",
    )
    pull_request_check.add_argument("--pr", type=int, required=True, metavar="NUMBER")

    policy = commands.add_parser("policy", help="print the provider-neutral loader block")
    policy.add_argument("--print", action="store_true", required=True, dest="print_loader")
    commands.add_parser("protect", help="deny PreToolUse writes without this session's live claim")
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
) -> tuple[tuple[protocol.ActiveClaim, ...], protocol.ClaimConflictIndex]:
    selected = tuple(
        claim
        for claim in claims
        if issue is None
        or (isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue)
    )
    index = protocol._claim_conflict_index(claims)
    if not selected:
        return (), index
    related_ids = (
        {claim.claim_id for claim in claims}
        if issue is None
        else protocol._related_claim_ids(index, selected)
    )
    related = tuple(claim for claim in claims if claim.claim_id in related_ids)
    return related, index


def _resource_fields(claim: protocol.ActiveClaim) -> dict[str, object]:
    if claim.resource is None:
        return {"resource": None, "resource_value": None}
    return {"resource": claim.resource.name, "resource_value": claim.resource.value}


def _overlap_subjects(
    claims_by_id: dict[str, protocol.ActiveClaim], peer_ids: set[str]
) -> list[dict[str, object]]:
    return [
        {
            **_identity_json(peer.identity),
            "claim_id": peer.claim_id,
            "agent": peer.agent,
        }
        for claim_id in sorted(peer_ids)
        if (peer := claims_by_id.get(claim_id)) is not None
    ]


def _overlap_note(claims_by_id: dict[str, protocol.ActiveClaim], peer_ids: set[str]) -> str | None:
    peers = [claims_by_id[claim_id] for claim_id in sorted(peer_ids) if claim_id in claims_by_id]
    if not peers:
        return None
    return "overlaps " + ", ".join(f"{_claim_subject(claim)} ({claim.claim_id})" for claim in peers)


def _status(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if not related:
        subject = "repository" if issue is None else f"issue #{issue}"
        print(f"UNCLAIMED {subject}")
        return 0
    claims_by_id = {claim.claim_id: claim for claim in claims}
    for claim in related:
        state = "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED"
        print(
            f"{state} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
            f"base={claim.base} branch={claim.branch} claim={claim.claim_id}"
            f"{_claim_age_suffix(claim, observed_at)}"
        )
        for path in claim.scope:
            print(f"  {path}")
        if claim.resource is not None:
            print(f"  resource {claim.resource.name}={claim.resource.value}")
        if claim.whole_reason is not None:
            print(f"  whole: {claim.whole_reason}")
        note = _overlap_note(claims_by_id, protocol._overlap_peer_ids(index, claim))
        if note is not None:
            print(f"  {note}")
    return 2 if any(claim.claim_id in index.conflict_ids for claim in related) else 0


def _status_json(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ledger: int,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if not related:
        state = "UNCLAIMED"
    elif any(claim.claim_id in index.conflict_ids for claim in related):
        state = "CONFLICT"
    else:
        state = "CLAIMED"
    claims_by_id = {claim.claim_id: claim for claim in claims}
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
                **_resource_fields(claim),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "overlaps": _overlap_subjects(
                    claims_by_id, protocol._overlap_peer_ids(index, claim)
                ),
                "state": "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED",
                "age": _claim_age_fields(claim, observed_at)[0],
                "old": _claim_age_fields(claim, observed_at)[1],
            }
            for claim in related
        ],
    }
    print(json.dumps(payload))
    return 2 if state == "CONFLICT" else 0


def _who(claims: tuple[protocol.ActiveClaim, ...], path: str) -> None:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return
    for claim in holders:
        print(
            f"CLAIMED {path} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
        if claim.whole_reason is not None:
            print(f"  whole: {claim.whole_reason}")
    if len(holders) > 1:
        print(
            "overlap: "
            + ", ".join(f"{_claim_subject(claim)} ({claim.claim_id})" for claim in holders)
        )


def _who_json(claims: tuple[protocol.ActiveClaim, ...], path: str, ledger: int) -> None:
    holders = protocol.claims_holding_path(claims, path)
    state = "UNCLAIMED" if not holders else "CLAIMED"
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
                **_resource_fields(claim),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "state": "CLAIMED",
            }
            for claim in holders
        ],
    }
    print(json.dumps(payload))


def _rescope_json(claimed: protocol.ActiveClaim) -> None:
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


@dataclass(frozen=True)
class ScopeVersioning:
    """How much of the claimed scope's Git history the checkout already has,
    for the `--json` claim payload's `versioned_files`/`share` fields."""

    versioned_files: int
    versioned_files_total: int
    share: float


def _claim_json(
    claimed: protocol.ActiveClaim,
    *,
    versioning: ScopeVersioning,
    touches: tuple[protocol.ActiveClaim, ...],
    checks: tuple[SliceCheck, ...],
) -> int:
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
                **_resource_fields(claimed),
                "versioned_files": versioning.versioned_files,
                "versioned_files_total": versioning.versioned_files_total,
                "share": versioning.share,
                "touches": [_touch_json(claim) for claim in touches],
                "checks": [check.as_json() for check in checks],
            }
        )
    )
    return 0


def _release_json(
    released: protocol.ActiveClaim,
    agent: str,
    role: str | None,
    outcome: protocol.ReleaseOutcome,
) -> None:
    print(
        json.dumps(
            {
                **_identity_json(released.identity),
                "branch": released.branch,
                "claim_id": released.claim_id,
                "agent": agent,
                "role": role if role is not None else released.role,
                "reason": outcome.reason,
            }
        )
    )


def _merged_pull_request_floor(issues: tuple[board.Issue, ...], now: datetime) -> datetime:
    """The earliest merge that could still matter to a currently open issue.

    A pull request can only touch or close an issue that already exists, so
    nothing merged before the oldest still-open issue was filed can ever
    change any open item's stage. Anchoring the query here — instead of an
    arbitrary fixed window — is what lets a slice's "Refs #N"/"Part of #N"
    landing keep crediting its still-open epic for as long as the epic stays
    open, rather than for a fixed number of days after which the credit
    silently reverts. Residual: the underlying query is still capped (see
    `GitHubForge.list_recent_merged_board_pull_requests`), so an epic
    old enough to have more merges than that cap between its filing and now
    can still lose credit for an early slice; this floor removes the
    fortnight-sized version of that gap, not every version of it.
    """
    if not issues:
        return now
    return min(_timestamp(issue.created_at) for issue in issues)


def _board(
    client: forge.ForgeReader,
    claims: tuple[protocol.ActiveClaim, ...],
    *,
    issues: tuple[board.Issue, ...] | None = None,
) -> board.Board:
    now = datetime.now(UTC)
    toplevel = Path(checkout._git_output(["rev-parse", "--show-toplevel"]))
    if issues is None:
        issues = client.list_open_board_issues()
    since = _merged_pull_request_floor(issues, now)
    blockers = board.blocker_references(issues)
    # Open and recently-merged pull requests are independent reads once
    # `since` is known, so fetching them on separate threads instead of one
    # after another overlaps their `gh` subprocess wait time.
    with ThreadPoolExecutor(max_workers=3) as pool:
        open_pull_requests = pool.submit(client.list_open_board_pull_requests)
        merged_pull_requests = pool.submit(client.list_recent_merged_board_pull_requests, since)
        blocker_references = pool.submit(client.list_board_blockers, blockers)
        pull_requests = (open_pull_requests.result(), merged_pull_requests.result())
    return board.build_board(
        issues,
        *pull_requests,
        claims,
        board.load_config(toplevel / ".agent-claim" / "board.toml"),
        repository=client.repository.path,
        blocker_references=blocker_references.result(),
        now=now,
        trunk_landings=checkout.trunk_landing_times(),
    )


def _rulings(projected: board.Board, issues: tuple[board.Issue, ...], *, as_json: bool) -> None:
    progress_by_issue = {issue.number: board.expectation_progress(issue.body) for issue in issues}
    items = tuple(
        sorted(
            (
                (item, progress_by_issue[item.number])
                for item in projected.items
                if progress_by_issue[item.number].open > 0
            ),
            key=lambda entry: (
                *board.board_rank(entry[0])[:2],
                entry[1].open,
                entry[0].number,
            ),
        )
    )
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "number": item.number,
                        "title": item.title,
                        "open": progress.open,
                        "total": progress.total,
                    }
                    for item, progress in items
                ]
            )
        )
        return
    if not items:
        print("No open expectation lines.")
        return
    print(
        "\n".join(
            f"#{item.number} {progress.open}/{progress.total}: {item.title}"
            for item, progress in items
        )
    )


def _ruling_pull_hint(item: board.BoardItem) -> str | None:
    if item.expectation_state is board.ExpectationState.PROPOSED:
        return "Erwartungen ungeregelt, beim Ziehen zuerst refinen"
    if not item.ruling_old:
        return None
    return f"vor {item.ruling_landings} Landungen geregelt, beim Ziehen neu refinen"


def _next_json(
    item: board.BoardItem | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
) -> int:
    payload: dict[str, object] = {
        "recovery": [
            {
                "number": recovery_item.number,
                "title": recovery_item.title,
                "step": board.RECOVERY_STEP,
            }
            for recovery_item in recovery
        ],
        "skipped": [
            {"number": skipped_item.number, "reason": skipped_item.actionable_reason}
            for skipped_item in skipped
        ],
    }
    if item is not None:
        payload.update(
            {
                "number": item.number,
                "score": item.score,
                "title": item.title,
                "next": item.next_step,
                "ruling_landings": item.ruling_landings,
                "ruling_old": item.ruling_old,
            }
        )
        hint = _ruling_pull_hint(item)
        if hint is not None:
            payload["ruling_hint"] = hint
    print(json.dumps(payload))
    return 0


def _next(
    item: board.BoardItem | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
) -> int:
    """A landed-but-open item is named before anything new is pulled."""
    lines: list[str] = []
    if recovery:
        lines.append("RECOVERY")
        lines.extend(
            f"#{recovery_item.number}: {board.RECOVERY_STEP}" for recovery_item in recovery
        )
        lines.append("")
    if item is None:
        lines.append("No actionable item.")
    else:
        lines.append(f"#{item.number} score {item.score}: {item.title}")
        lines.append(f"Next: {item.next_step}")
        hint = _ruling_pull_hint(item)
        if hint is not None:
            lines.append(hint)
    if skipped:
        skipped_lines = (
            f"#{skipped_item.number}: {skipped_item.actionable_reason}" for skipped_item in skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))
    return 0


def _unworkable(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(item for item in projected.items if not item.actionable)


@dataclass(frozen=True)
class SliceCheck:
    """One slice-rule finding — the `check` table `#79` rules.

    `slice`/`issue` carry whichever numbers the message names, so a `--json`
    caller can act on the finding without re-parsing `text`; either is
    `None` when the check has nothing of that kind to name.
    """

    level: str
    check: str
    text: str
    slice: int | None = None
    issue: int | None = None

    def render(self) -> str:
        prefix = "ERROR" if self.level == "error" else "WARNING"
        return f"{prefix}: {self.text}"

    def as_json(self) -> dict[str, object]:
        return {
            "level": self.level,
            "check": self.check,
            "text": self.text,
            "slice": self.slice,
            "issue": self.issue,
        }


def _fetch_issue_reference(client: forge.ForgeReader, number: int) -> forge.ItemReference:
    """The live state, title, and body of issue `number` from `client`.

    Called only for a claim target that the already-fetched open board
    didn't resolve as OPEN — a closed or missing issue never appears in
    `list_open_board_issues`, so those two states need their own targeted
    lookup; this is that lookup, kept to one issue at a time rather than a
    repository-wide query.
    """
    return client.item_reference(number)


def _issue_reference_state(
    client: forge.ForgeReader,
    open_by_number: dict[int, board.Issue],
    number: int,
) -> tuple[forge.ItemState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return forge.ItemState.OPEN, open_issue.title, open_issue.body
    reference = _fetch_issue_reference(client, number)
    return reference.state, reference.title, reference.body


def _out_of_order_check(
    projected: board.Board, issue: int | None, out_of_order_reason: str | None
) -> SliceCheck | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or board.board_rank(highest) >= board.board_rank(claimed_item):
        return None
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "out-of-order",
        f"higher-priority actionable item #{highest.number} "
        f"(score {highest.score}) is free: {highest.title}; "
        "use --out-of-order REASON to proceed",
        issue=highest.number,
    )


def _parent_checks(
    client: forge.ForgeReader, repository: str, issue: int, title: str
) -> SliceCheck | None:
    """Warn when a slice-shaped title names a parent GitHub does not record as one."""
    match = board.slice_title_match(title)
    if match is None:
        return None
    slice_number, parent_issue = match
    parent = client.parent_issue(issue)
    if parent is not None and parent.reference == board.IssueReference(repository, parent_issue):
        return None
    return SliceCheck(
        "warning",
        "missing-parent",
        f"looks like slice {slice_number} of #{parent_issue} but is no sub-issue "
        f"of #{parent_issue}; the parent inherits nothing",
        slice=slice_number,
        issue=parent_issue,
    )


def _body_contract_checks(
    contract: board.Contract, blocker_references: tuple[board.BlockerReference, ...]
) -> tuple[SliceCheck, ...]:
    checks = [SliceCheck("error", "body-contract", defect.message) for defect in contract.defects]
    blocker_by_number = {reference.number: reference for reference in blocker_references}
    for blocker in contract.blocker_issues:
        reference = blocker_by_number[blocker]
        if reference.is_pull_request:
            continue
        if reference.state is board.BlockerState.CLOSED:
            checks.append(
                SliceCheck(
                    "error", "closed-blocker", f"blocker #{blocker} is closed", issue=blocker
                )
            )
        elif reference.state is board.BlockerState.MISSING:
            checks.append(
                SliceCheck(
                    "error",
                    "missing-blocker",
                    f"blocker #{blocker} does not exist here",
                    issue=blocker,
                )
            )
    return tuple(checks)


@dataclass(frozen=True)
class BoardReferenceLookup:
    """The board client, its repository, and the currently open issues it can
    resolve `#reference`s against — what every cross-issue slice/parent check
    below needs to look a referenced issue up."""

    client: forge.ForgeReader
    repository: str
    open_by_number: dict[int, board.Issue]


def _slice_rule_checks(
    lookup: BoardReferenceLookup,
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
) -> tuple[SliceCheck, ...]:
    checks: list[SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason)
    if out_of_order is not None:
        checks.append(out_of_order)
    state, title, _body = _issue_reference_state(lookup.client, lookup.open_by_number, issue)
    if state is forge.ItemState.CLOSED:
        checks.append(SliceCheck("error", "closed-issue", f"issue #{issue} is closed", issue=issue))
    elif state is forge.ItemState.MISSING:
        checks.append(
            SliceCheck("error", "missing-issue", f"issue #{issue} does not exist here", issue=issue)
        )
    item = next((item for item in projected.items if item.number == issue), None)
    if item is not None:
        checks.extend(_body_contract_checks(item.contract, projected.blocker_references))
    if title is not None:
        parent_check = _parent_checks(lookup.client, lookup.repository, issue, title)
        if parent_check is not None:
            checks.append(parent_check)
    return tuple(checks)


def _refuse_claim(json_mode: bool, issue: int | None, checks: tuple[SliceCheck, ...]) -> None:
    if json_mode:
        payload = {"refused": True, "issue": issue, "checks": [c.as_json() for c in checks]}
        print(json.dumps(payload))
        return
    for check in checks:
        print(check.render(), file=sys.stderr)


def _claim_defect(
    client: forge.ForgeReader,
    detail: forge.Landing,
    identity: protocol.ClaimIdentity,
) -> board.ClassificationDefect | None:
    """A landing declares only what its own head branch holds a live claim on."""
    if any(
        claim.identity == identity and claim.branch == detail.source_branch
        for claim in protocol._ledger_claims(client)
    ):
        return None
    subject = (
        f"claim for #{identity.issue}"
        if isinstance(identity, protocol.IssueIdentity)
        else "issue-less lane claim"
    )
    return board.ClassificationDefect(f"has no active {subject} on branch {detail.source_branch!r}")


def _no_item_defect(
    client: forge.ForgeReader,
    repository: str,
    detail: forge.Landing,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept an issue-less landing as declared.

    A `No-Item` lane owns no issue, so it needs its own lane claim and may
    retire nothing: a closing reference here would close an item no claim and
    no `Work-Item:` line ever named.
    """
    claim_defect = _claim_defect(client, detail, protocol.LaneIdentity())
    if claim_defect is not None:
        return claim_defect
    closing = board.closing_references(detail.body, repository)
    if closing:
        named = ", ".join(str(reference) for reference in sorted(closing, key=str))
        return board.ClassificationDefect(
            f"declares no work item but closes {named}; name it as the work item"
        )
    return None


@dataclass(frozen=True)
class _ParentRequirement:
    """What an item's parent demands of the pull request that lands the item."""

    reference: board.IssueReference
    closing_required: bool


def _parent_requirement(
    client: forge.ForgeReader,
    repository: str,
    item: board.IssueReference,
) -> _ParentRequirement | board.ClassificationDefect | None:
    """The parent's demand, read from GitHub's sub-issue relation.

    Closing a parent's last open child completes the parent, so that landing
    closes the parent too. A parent keeping other open children stays open,
    and must say what happens next.
    """
    parent = client.parent_issue(item.number)
    if parent is None:
        return None
    if parent.reference.repository != repository:
        return board.ClassificationDefect(
            f"has parent {parent.reference} in another repository, "
            "whose children this check cannot read"
        )
    remaining = tuple(
        child for child in client.open_children(parent.reference.number) if child != item
    )
    if not remaining:
        return _ParentRequirement(parent.reference, True)
    if board.parse_contract(parent.body).next is None:
        children = "child" if len(remaining) == 1 else "children"
        return board.ClassificationDefect(
            f"leaves parent {parent.reference} open with {len(remaining)} other open "
            f"{children}, whose body carries no Next line"
        )
    return _ParentRequirement(parent.reference, False)


def _closing_defect(
    detail: forge.Landing,
    repository: str,
    item: board.IssueReference,
    requirement: _ParentRequirement | None,
) -> board.ClassificationDefect | None:
    """Which issues this landing must close, and that it closes nothing else."""
    closing = board.closing_references(detail.body, repository)
    if item not in closing:
        return board.ClassificationDefect(f"carries no closing reference for its work item {item}")
    completed_parent = (
        {requirement.reference}
        if requirement is not None and requirement.closing_required
        else set()
    )
    missing_parent = completed_parent - closing
    if missing_parent:
        parent = next(iter(missing_parent))
        return board.ClassificationDefect(
            f"closes the last open child of parent {parent}; close the parent too"
        )
    besides = tuple(sorted(closing - {item} - completed_parent, key=str))
    if besides:
        named = ", ".join(str(reference) for reference in besides)
        return board.ClassificationDefect(
            f"closes {named} besides its work item {item}; a pull request lands one item"
        )
    return None


def _work_item_defect(
    client: forge.ForgeReader,
    repository: str,
    detail: forge.Landing,
    item: board.IssueReference,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept `item` as the landing pull request's work item."""
    if item.repository != repository:
        return board.ClassificationDefect(
            f"names work item {item} of another repository, which holds no claim here"
        )
    if item.number == protocol.LEDGER_ISSUE:
        return board.ClassificationDefect(
            f"names the claim ledger #{protocol.LEDGER_ISSUE} as its work item"
        )
    claim_defect = _claim_defect(client, detail, protocol.IssueIdentity(item.number))
    if claim_defect is not None:
        return claim_defect
    requirement = _parent_requirement(client, repository, item)
    if isinstance(requirement, board.ClassificationDefect):
        return requirement
    return _closing_defect(detail, repository, item, requirement)


def _checked_classification(
    client: forge.ForgeReader, repository: str, detail: forge.Landing
) -> board.Classification | board.ClassificationDefect:
    if detail.source_repository.path != repository:
        return board.ClassificationDefect(
            f"proposes a branch of {detail.source_repository}; cross-repository pull "
            "requests are not classified"
        )
    classification = board.parse_pull_request_classification(detail.body, repository)
    if isinstance(classification, board.ClassificationDefect):
        return classification
    default_branch = client.default_branch()
    if detail.target_branch != default_branch:
        return board.ClassificationDefect(
            f"targets {detail.target_branch!r}, not the default branch {default_branch!r}"
        )
    defect = (
        _no_item_defect(client, repository, detail)
        if isinstance(classification, board.NoItemClassification)
        else _work_item_defect(client, repository, detail, classification.item)
    )
    return classification if defect is None else defect


def _pull_request_check(client: forge.ForgeReader, repository: str, number: int) -> int:
    detail = client.landing(number)
    checked = _checked_classification(client, repository, detail)
    if isinstance(checked, board.ClassificationDefect):
        print(f"REFUSED: pull request #{detail.number} {checked.message}", file=sys.stderr)
        return 1
    print(f"PR #{detail.number} by {detail.author} declares {checked}")
    return 0


def _release_outcome(merged: int | None, abandoned: str | None) -> protocol.ReleaseOutcome:
    if merged is not None:
        return protocol.MergedRelease(merged)
    return protocol.AbandonedRelease(
        protocol._outbound_text(abandoned, "abandoned reason", maximum=512)
    )


def _verify_merged_release(
    client: forge.ForgeReader,
    repository: str,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
) -> None:
    """Refuse a `--merged` release the landing itself does not support."""
    detail = client.landing(merged.pull_request)
    if not detail.merged:
        raise protocol.ClaimUnavailableError(f"pull request #{detail.number} is not merged")
    default_branch = client.default_branch()
    if detail.target_branch != default_branch:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} merged into {detail.target_branch!r}, "
            f"not the default branch {default_branch!r}"
        )
    classification = board.parse_pull_request_classification(detail.body, repository)
    if isinstance(classification, board.ClassificationDefect):
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} {classification.message}"
        )
    if isinstance(identity, protocol.LaneIdentity):
        if isinstance(classification, board.WorkItemClassification):
            raise protocol.ClaimUnavailableError(
                f"pull request #{detail.number} names {classification.item}; "
                "an issue-less lane needs a No-Item line"
            )
        return
    item = board.IssueReference(repository, identity.issue)
    if not isinstance(classification, board.WorkItemClassification) or classification.item != item:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} names {classification}, not work item #{identity.issue}"
        )
    reference = _fetch_issue_reference(client, identity.issue)
    if reference.state is not forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(
            f"work item #{identity.issue} is {reference.state.value}, not closed"
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
    except (protocol.InvalidClaimMarkerError, OSError, ValueError):
        return None


PATH_REQUIRED = "path required"


def _protect_write(repository: str | None, payload: dict[str, object]) -> int:
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return _hook_deny(PATH_REQUIRED)
    raw_path = _hook_path(tool_input)
    if raw_path is None:
        return _hook_deny(PATH_REQUIRED)
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
        return _hook_deny(PATH_REQUIRED)
    forge_handle = github.GitHubForge(
        github.discover_repository(repository, remote_url=checkout.origin_remote_url)
    )
    ledger = discovery.discover_ledger(forge_handle)
    if ledger is None:
        return _hook_deny("claim first")
    protocol.configure_ledger(ledger)
    for claim in protocol._ledger_claims(forge_handle):
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


def _optional_issue_number(value: int | None) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class _ReadSession:
    """What a dispatched read-only subcommand needs beyond its parsed arguments."""

    forge: forge.ForgeReader
    ledger: int


@dataclass(frozen=True)
class _WriteSession:
    """What a dispatched write subcommand needs beyond its parsed arguments."""

    forge: forge.ForgeWriter
    ledger: int
    release_branch: str | None


@dataclass(frozen=True)
class _RescopeCommand:
    identity: protocol.ClaimIdentity
    agent: str
    add: tuple[str, ...]
    drop: tuple[str, ...]
    claim_id: str | None
    branch: str
    whole_reason: str | None


def _rescope_command(parsed: argparse.Namespace) -> _RescopeCommand:
    branch = checkout._git_output(["branch", "--show-current"])
    if not branch:
        raise protocol.ClaimUnavailableError(
            "rescope requires a non-empty current branch; "
            "check out the claim branch, or pass an issue number"
        )
    checkout._validate_worktree_branch(branch)
    identity = _resolved_identity(_optional_issue_number(parsed.issue), branch)
    return _RescopeCommand(
        identity=identity,
        agent=parsed.agent,
        add=protocol._valid_scope(parsed.add) if parsed.add else (),
        drop=protocol._valid_scope(parsed.drop) if parsed.drop else (),
        claim_id=parsed.claim_id,
        branch=branch,
        whole_reason=_optional_whole_reason(parsed),
    )


def _cmd_pull_request_check(parsed: argparse.Namespace, session: _ReadSession) -> int:
    pull_request_number = int(parsed.pr)
    return _pull_request_check(session.forge, session.forge.repository.path, pull_request_number)


def _cmd_status(parsed: argparse.Namespace, session: _ReadSession) -> int:
    issue = _optional_issue_number(parsed.issue)
    comments = session.forge.list_protocol_candidates(protocol.LEDGER_ISSUE)
    claims = protocol.active_claims(comments)
    now = datetime.now(UTC)
    if parsed.json:
        return _status_json(claims, issue, session.ledger, now=now)
    print(f"LEDGER #{session.ledger}")
    return _status(claims, issue, now=now)


def _cmd_board(parsed: argparse.Namespace, session: _ReadSession) -> None:
    comments = session.forge.list_protocol_candidates(protocol.LEDGER_ISSUE)
    projected = _board(session.forge, protocol.active_claims(comments))
    print(board.board_json(projected) if parsed.json else board.render(projected))


def _cmd_rulings(parsed: argparse.Namespace, session: _ReadSession) -> None:
    comments = session.forge.list_protocol_candidates(protocol.LEDGER_ISSUE)
    issues = session.forge.list_open_board_issues()
    projected = _board(session.forge, protocol.active_claims(comments), issues=issues)
    _rulings(projected, issues, as_json=parsed.json)


def _cmd_next(parsed: argparse.Namespace, session: _ReadSession) -> int:
    comments = session.forge.list_protocol_candidates(protocol.LEDGER_ISSUE)
    projected = _board(session.forge, protocol.active_claims(comments))
    item = board.highest_scored_actionable(projected)
    skipped = _unworkable(projected)
    recovery = projected.recovery
    if item is None:
        if skipped or recovery:
            if parsed.json:
                _next_json(None, skipped, recovery)
            else:
                _next(None, skipped, recovery)
        return 3
    return _next_json(item, skipped, recovery) if parsed.json else _next(item, skipped, recovery)


def _cmd_who(parsed: argparse.Namespace, session: _ReadSession) -> None:
    claims = protocol._ledger_claims(session.forge)
    if parsed.json:
        _who_json(claims, parsed.path, session.ledger)
        return
    print(f"LEDGER #{session.ledger}")
    _who(claims, parsed.path)


def _cmd_rescope(parsed: argparse.Namespace, session: _WriteSession) -> None:
    client = session.forge
    requested = _rescope_command(parsed)
    if requested.add or requested.drop:
        versioned = checkout.versioned_paths()
        selected = protocol._select_rescope_claim(
            protocol._ledger_claims(client),
            requested.identity,
            requested.agent,
            requested.claim_id,
            branch=requested.branch,
        )
        combined = protocol._combined_scope(selected.scope, requested.add, requested.drop)
        _reject_wide_scope(combined, versioned, requested.whole_reason)
    rescoped = protocol.rescope_claim(
        client,
        requested.identity,
        requested.agent,
        requested.add,
        requested.drop,
        requested.claim_id,
        branch=requested.branch,
        whole_reason=requested.whole_reason,
    )
    if parsed.json:
        _rescope_json(rescoped)
        return
    print(f"RESCOPED {_claim_subject(rescoped)}: {rescoped.claim_id}")


def _cmd_claim(parsed: argparse.Namespace, session: _WriteSession) -> int:
    client = session.forge
    requested = _request(parsed)
    versioned = checkout.versioned_paths()
    n, total, share = _reject_wide_scope(requested.scope, versioned, requested.whole_reason)
    checks: tuple[SliceCheck, ...] = ()
    target_issue: int | None = None
    if isinstance(requested.identity, protocol.IssueIdentity):
        target_issue = requested.identity.issue
        replayed = protocol.matching_claim_retry(protocol._ledger_claims(client), requested)
        if replayed is None:
            open_issues = client.list_open_board_issues()
            open_by_number = {issue.number: issue for issue in open_issues}
            projected = _board(client, protocol._ledger_claims(client), issues=open_issues)
            checks = _slice_rule_checks(
                BoardReferenceLookup(client, client.repository.path, open_by_number),
                target_issue,
                projected,
                requested.out_of_order_reason,
            )
    if any(check.level == "error" for check in checks):
        _refuse_claim(parsed.json, target_issue, checks)
        return 2
    for check in checks:
        print(check.render(), file=sys.stderr if parsed.json else sys.stdout)
    # `_acquire_claim_with_observed` already reads the ledger once,
    # right after posting, to detect a claim race; that same snapshot
    # is what the "touches" note below needs, so reusing it (instead
    # of a fresh `protocol._ledger_claims(client)` call) removes the
    # slowest step of `claim` — the wait was reported as a hang that
    # landed after the mutation was already visible on the ledger.
    try:
        claimed, observed = protocol._acquire_claim_with_observed(client, requested)
    except protocol.ClaimPostedReconcileFailedError as error:
        # The claim comment already exists and already won the
        # ledger; a failure in the post-claim label/projection
        # reconcile must never read as a refusal — that would leave
        # the operator believing nothing happened while a live claim
        # sits on the ledger. Print the claim plainly even under
        # --json: there is no well-formed claim payload to emit when
        # the reconcile itself is what failed.
        print(
            f"CLAIMED {_claim_subject(error.claim)}: "
            f"{error.claim.claim_id} {error.claim.comment.url}"
        )
        print(
            f"ERROR: the claim above exists, but the post-claim "
            f"reconcile failed: {error.reconcile_error}",
            file=sys.stderr,
        )
        return 2
    touches = protocol.conflicting_claims(observed, claimed)
    if parsed.json:
        return _claim_json(
            claimed,
            versioning=ScopeVersioning(n, total, share),
            touches=touches,
            checks=checks,
        )
    print(f"CLAIMED {_claim_subject(claimed)}: {claimed.claim_id} {claimed.comment.url}")
    print(_claim_cost_line(n, total, touches))
    return 0


def _cmd_release(parsed: argparse.Namespace, session: _WriteSession) -> None:
    client = session.forge
    issue = _optional_issue_number(parsed.issue)
    identity = _resolved_identity(issue, session.release_branch or "")
    merged = None if parsed.merged is None else int(parsed.merged)
    outcome = _release_outcome(merged, parsed.abandoned)
    if isinstance(outcome, protocol.MergedRelease):
        _verify_merged_release(client, client.repository.path, identity, outcome)
    released = protocol.release_claim(
        client,
        identity,
        parsed.agent,
        parsed.role,
        outcome,
        parsed.claim_id,
        branch=session.release_branch,
        coordinator_override=parsed.coordinator_override,
    )
    if parsed.json:
        _release_json(released, parsed.agent, parsed.role, outcome)
        return
    print(f"RELEASED {_claim_subject(released)}: {released.claim_id}")


def _cmd_supersede(parsed: argparse.Namespace, session: _WriteSession) -> None:
    successor_issue = int(parsed.successor_issue)
    frozen = protocol.supersede_ledger(
        session.forge,
        successor_issue,
        parsed.agent,
        parsed.role,
        parsed.reason,
        parsed.claim_id,
    )
    print(
        f"SUPERSEDED ledger #{protocol.LEDGER_ISSUE} successor "
        f"#{successor_issue}: {frozen.claim_id}"
    )


def _cmd_reconcile(parsed: argparse.Namespace, session: _WriteSession) -> None:
    client = session.forge
    try:
        for repair in protocol.repair_duplicate_claims(client):
            superseded = ", ".join(f"#{cid}" for cid in repair.superseded_comment_ids)
            print(
                f"REPAIRED claim {repair.claim_id!r}: superseded {superseded} "
                f"-> survivor #{repair.survivor_comment_id}"
            )
    except protocol.LedgerSupersededError:
        # A frozen ledger has nothing left for duplicate repair to fix; let the
        # label reconciliation below observe the freeze and run its own cleanup.
        pass
    issue = _optional_issue_number(parsed.issue)
    if issue is None:
        reconciled = protocol.reconcile_all_labels(client)
    else:
        protocol.reconcile_issue_label(client, issue)
        reconciled = tuple(
            claim.identity.issue
            for claim in protocol._ledger_claims(client)
            if isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue
        )
    print("RECONCILED " + (", ".join(f"#{issue}" for issue in reconciled) or "no claims"))


_READ_HANDLERS: dict[str, Callable[[argparse.Namespace, _ReadSession], int | None]] = {
    "pr-check": _cmd_pull_request_check,
    "status": _cmd_status,
    "board": _cmd_board,
    "rulings": _cmd_rulings,
    "next": _cmd_next,
    "who": _cmd_who,
}
_WRITE_HANDLERS: dict[str, Callable[[argparse.Namespace, _WriteSession], int | None]] = {
    "rescope": _cmd_rescope,
    "claim": _cmd_claim,
    "release": _cmd_release,
    "supersede": _cmd_supersede,
    "reconcile": _cmd_reconcile,
}


def _release_branch_for(parsed: argparse.Namespace) -> str | None:
    if parsed.coordinator_override:
        protocol._require_coordinator_override(parsed.role)
    if parsed.issue is not None and parsed.claim_id is not None:
        return None
    release_branch = checkout._git_output(["branch", "--show-current"])
    if release_branch:
        return release_branch
    if parsed.issue is None:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; "
            "check out the docs/ or fix/ lane branch, or pass "
            "an issue number"
        )
    raise protocol.ClaimUnavailableError(
        "release without --claim-id requires a non-empty current branch; pass --claim-id"
    )


def _dispatch(parsed: argparse.Namespace) -> int:
    if parsed.command in {"claim", "release", "rescope"}:
        parsed.agent = checkout._resolved_agent(parsed.agent)
    release_branch = _release_branch_for(parsed) if parsed.command == "release" else None
    repository = github.discover_repository(parsed.repo, remote_url=checkout.origin_remote_url)
    forge_handle = github.GitHubForge(repository)
    if parsed.command == "bootstrap":
        ledger = discovery.bootstrap_ledger(forge_handle)
        protocol.configure_ledger(ledger)
        print(f"LEDGER #{ledger}")
        return 0
    ledger = discovery.discover_ledger(forge_handle)
    if ledger is None:
        raise protocol.ClaimUnavailableError(
            "no agent-claim ledger exists; run agent-claim bootstrap"
        )
    protocol.configure_ledger(ledger)
    if parsed.command in _READ_HANDLERS:
        read_session = _ReadSession(forge=forge_handle, ledger=ledger)
        result = _READ_HANDLERS[parsed.command](parsed, read_session)
    else:
        write_session = _WriteSession(
            forge=forge_handle, ledger=ledger, release_branch=release_branch
        )
        result = _WRITE_HANDLERS[parsed.command](parsed, write_session)
    return 0 if result is None else result


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    if parsed.command == "policy":
        print(POLICY_LOADER)
        return 0
    if parsed.command == "protect":
        return _protect(parsed.repo)
    try:
        return _dispatch(parsed)
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
