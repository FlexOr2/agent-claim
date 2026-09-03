"""Coordinate coding-agent claims through a repository-neutral GitHub ledger."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
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
SCOPE_SHARE_LIMIT = 0.25
NEXT_PULL_DESCRIPTION = (
    "Pulling is not dispatching: an item whose expectations are still unruled is "
    "named here with refining as its first step, while dispatching a builder onto "
    "it waits for the operator's ruling."
)
ALLOW_DIRECTORY_HELP = (
    "permit a directory without a cut, or a scope covering more than a quarter "
    "of versioned files"
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


def _issue_body_for_cut(
    client: github.GitHubIssueComments, identity: protocol.ClaimIdentity
) -> str | None:
    if not isinstance(identity, protocol.IssueIdentity):
        return None
    for issue in client.list_open_board_issues():
        if issue.number == identity.issue:
            return issue.body
    return None


def _reject_uncut_directory_scope(
    client: github.GitHubIssueComments,
    identity: protocol.ClaimIdentity,
    paths: tuple[str, ...],
    allow_directory_reason: str | None,
) -> None:
    directories = checkout._scope_directories(paths)
    if not directories or allow_directory_reason is not None:
        return
    body = _issue_body_for_cut(client, identity)
    if body is not None and board.has_cut(body):
        return
    raise protocol.ClaimError(f"directory scope {directories[0]!r} erst schneiden")


def _scope_cost(versioned: tuple[str, ...], scope: tuple[str, ...]) -> tuple[int, int, float]:
    n = len(checkout.paths_under_scope(versioned, scope))
    total = len(versioned)
    share = 0.0 if total == 0 else n / total
    return n, total, share


def _reject_oversized_scope(
    scope: tuple[str, ...],
    allow_directory_reason: str | None,
    versioned: tuple[str, ...],
) -> tuple[int, int, float]:
    n, total, share = _scope_cost(versioned, scope)
    if share > SCOPE_SHARE_LIMIT and allow_directory_reason is None:
        raise protocol.ClaimError(
            f"scope covers more than a quarter of versioned files ({n} of {total}); "
            "pass --allow-directory REASON"
        )
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


def _claim_cost_line(
    n: int, total: int, touches: tuple[protocol.ActiveClaim, ...]
) -> str:
    percent = 0 if total == 0 else round(100 * n / total)
    return f"{n} of {total} versioned files ({percent}%); {_touch_summary(touches)}"


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
    allow_directory_reason = getattr(arguments, "allow_directory", None)
    if allow_directory_reason is not None:
        allow_directory_reason = protocol._outbound_text(
            allow_directory_reason, "allow-directory reason", maximum=512
        )
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
        allow_directory_reason=allow_directory_reason,
        resource=resource,
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

    board_command = commands.add_parser("board", help="project the open work board without writes")
    board_command.add_argument("--json", action="store_true")

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
        help=(
            "refuses a claim without a reason when a higher-priority actionable item is "
            "free; records why"
        ),
    )
    claim.add_argument(
        "--allow-directory",
        metavar="REASON",
        help=ALLOW_DIRECTORY_HELP,
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
        help=ALLOW_DIRECTORY_HELP,
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


def _overlap_note(
    claims_by_id: dict[str, protocol.ActiveClaim], peer_ids: set[str]
) -> str | None:
    peers = [
        claims_by_id[claim_id]
        for claim_id in sorted(peer_ids)
        if claim_id in claims_by_id
    ]
    if not peers:
        return None
    return "overlaps " + ", ".join(
        f"{_claim_subject(claim)} ({claim.claim_id})" for claim in peers
    )


def _status(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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


def _who(claims: tuple[protocol.ActiveClaim, ...], path: str) -> int:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return 0
    for claim in holders:
        print(
            f"CLAIMED {path} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
    if len(holders) > 1:
        print(
            "overlap: "
            + ", ".join(
                f"{_claim_subject(claim)} ({claim.claim_id})" for claim in holders
            )
        )
    return 0


def _who_json(
    claims: tuple[protocol.ActiveClaim, ...], path: str, ledger: int
) -> int:
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
                "state": "CLAIMED",
            }
            for claim in holders
        ],
    }
    print(json.dumps(payload))
    return 0


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


def _claim_json(
    claimed: protocol.ActiveClaim,
    *,
    versioned_files: int,
    versioned_files_total: int,
    share: float,
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
                "versioned_files": versioned_files,
                "versioned_files_total": versioned_files_total,
                "share": share,
                "touches": [_touch_json(claim) for claim in touches],
                "checks": [check.as_json() for check in checks],
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


def _merged_pull_request_floor(issues: tuple[board.Issue, ...], now: datetime) -> datetime:
    """The earliest merge that could still matter to a currently open issue.

    A pull request can only touch or close an issue that already exists, so
    nothing merged before the oldest still-open issue was filed can ever
    change any open item's stage. Anchoring the query here — instead of an
    arbitrary fixed window — is what lets a slice's "Refs #N"/"Part of #N"
    landing keep crediting its still-open epic for as long as the epic stays
    open, rather than for a fixed number of days after which the credit
    silently reverts. Residual: the underlying query is still capped (see
    `GitHubIssueComments.list_recent_merged_board_pull_requests`), so an epic
    old enough to have more merges than that cap between its filing and now
    can still lose credit for an early slice; this floor removes the
    fortnight-sized version of that gap, not every version of it.
    """
    if not issues:
        return now
    return min(_timestamp(issue.created_at) for issue in issues)


def _board(
    client: github.GitHubIssueComments,
    claims: tuple[protocol.ActiveClaim, ...],
    *,
    issues: tuple[board.Issue, ...] | None = None,
) -> board.Board:
    now = datetime.now(timezone.utc)
    toplevel = Path(checkout._git_output(["rev-parse", "--show-toplevel"]))
    if issues is None:
        issues = client.list_open_board_issues()
    since = _merged_pull_request_floor(issues, now)
    # Open and recently-merged pull requests are independent reads once
    # `since` is known, so fetching them on separate threads instead of one
    # after another overlaps their `gh` subprocess wait time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        open_pull_requests = pool.submit(client.list_open_board_pull_requests)
        merged_pull_requests = pool.submit(
            client.list_recent_merged_board_pull_requests, since
        )
        pull_requests = (open_pull_requests.result(), merged_pull_requests.result())
    return board.build_board(
        issues,
        *pull_requests,
        claims,
        board.load_config(toplevel / ".agent-claim" / "board.toml"),
        now=now,
        trunk_landings=checkout.trunk_landing_times(),
    )


def _ruling_pull_hint(item: board.BoardItem) -> str | None:
    if item.expectation_state is board.ExpectationState.PROPOSED:
        return "Erwartungen ungeregelt, beim Ziehen zuerst refinen"
    if not item.ruling_old:
        return None
    return (
        f"vor {item.ruling_landings} Landungen geregelt, beim Ziehen neu refinen"
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
                "ruling_landings": item.ruling_landings,
                "ruling_old": item.ruling_old,
            }
        )
        hint = _ruling_pull_hint(item)
        if hint is not None:
            payload["ruling_hint"] = hint
    print(json.dumps(payload))
    return 0


def _next(item: board.BoardItem | None, skipped: tuple[board.BoardItem, ...]) -> int:
    lines = (
        [f"#{item.number} score {item.score}: {item.title}", f"Next: {item.contract.next}"]
        if item is not None
        else ["No actionable item."]
    )
    if item is not None:
        hint = _ruling_pull_hint(item)
        if hint is not None:
            lines.append(hint)
    if skipped:
        skipped_lines = (
            f"#{skipped_item.number}: {skipped_item.actionable_reason}"
            for skipped_item in skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))
    return 0


def _unworkable(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(item for item in projected.items if not item.actionable)


class ReferenceState(StrEnum):
    """A referenced issue's state, as seen from this repository."""

    OPEN = "open"
    CLOSED = "closed"
    MISSING = "missing"


@dataclass(frozen=True)
class _IssueReference:
    state: ReferenceState
    title: str | None = None
    body: str | None = None


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


def _fetch_issue_reference(repository: str, number: int) -> _IssueReference:
    """The live state, title, and body of issue `number` in `repository`.

    Called only for a claim target or a slice-table `#n` link that the
    already-fetched open board didn't resolve as OPEN — a closed or missing
    issue never appears in `list_open_board_issues`, so those two states
    need their own targeted lookup; this is that lookup, kept to one issue
    at a time rather than a repository-wide query.
    """
    try:
        raw = _bounded_command(
            ["gh", "api", f"repos/{repository}/issues/{number}", "--jq", "{state,title,body}"],
            purpose="claim reference lookup",
        )
    except protocol.ClaimError as error:
        # `gh api` reports a nonexistent issue as an HTTP 404 in its combined
        # stdout/stderr text; `_bounded_command` doesn't expose the process's
        # real exit status, so this substring is the only signal available
        # to tell a missing issue apart from every other adapter failure.
        if "HTTP 404" in str(error):
            return _IssueReference(ReferenceState.MISSING)
        raise
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise protocol.ClaimError("GitHub returned a malformed issue reference") from error
    state = value.get("state") if isinstance(value, dict) else None
    title = value.get("title") if isinstance(value, dict) else None
    body = value.get("body") if isinstance(value, dict) else None
    if (
        state not in {"open", "closed"}
        or not isinstance(title, str)
        or (body is not None and not isinstance(body, str))
    ):
        raise protocol.ClaimError("GitHub returned a malformed issue reference")
    return _IssueReference(
        ReferenceState.OPEN if state == "open" else ReferenceState.CLOSED,
        title,
        body or "",
    )


def _issue_reference_state(
    repository: str, open_by_number: dict[int, board.Issue], number: int
) -> tuple[ReferenceState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return ReferenceState.OPEN, open_issue.title, open_issue.body
    reference = _fetch_issue_reference(repository, number)
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


def _slice_table_entry_checks(
    repository: str, open_by_number: dict[int, board.Issue], entry: board.SliceTableEntry
) -> tuple[SliceCheck, ...]:
    if isinstance(entry, board.MalformedSliceTable):
        return (
            SliceCheck(
                "error",
                "malformed-slice-table",
                f'malformed slice table header: "{entry.line}"',
            ),
        )
    if isinstance(entry, board.MalformedSliceRow):
        return (
            SliceCheck(
                "error",
                "malformed-slice-cell",
                f'malformed slice table row: "{entry.line}"',
            ),
        )
    return _slice_row_checks(repository, open_by_number, entry)


def _slice_row_checks(
    repository: str, open_by_number: dict[int, board.Issue], row: board.SliceTableRow
) -> tuple[SliceCheck, ...]:
    if row.item_issue is not None:
        state, _title, _body = _issue_reference_state(repository, open_by_number, row.item_issue)
        if state is ReferenceState.CLOSED:
            return (
                SliceCheck(
                    "error",
                    "landed-slice-in-table",
                    f"slice {row.index} links closed #{row.item_issue}; "
                    "a landed slice leaves the table",
                    slice=row.index,
                    issue=row.item_issue,
                ),
            )
        if state is ReferenceState.MISSING:
            return (
                SliceCheck(
                    "error",
                    "missing-slice-item",
                    f"slice {row.index} links #{row.item_issue}, which does not exist here",
                    slice=row.index,
                    issue=row.item_issue,
                ),
            )
        return ()
    if row.item_cell == board.UNDISPATCHED_SLICE_CELL:
        return (
            SliceCheck(
                "warning",
                "undispatched-slice",
                f'slice {row.index} "{row.name}" is not dispatched; '
                "make it an item before building it",
                slice=row.index,
            ),
        )
    return (
        SliceCheck(
            "error",
            "malformed-slice-cell",
            f'slice {row.index} item cell "{row.item_cell}" is neither — nor #n',
            slice=row.index,
        ),
    )


def _parent_line_checks(title: str, body: str) -> tuple[SliceCheck, ...]:
    match = board.slice_title_match(title)
    if match is None:
        return ()
    slice_number, parent_issue = match
    if parent_issue in board.parent_line_numbers(body):
        return ()
    return (
        SliceCheck(
            "warning",
            "missing-parent-line",
            f"looks like slice {slice_number} of #{parent_issue} but carries no "
            f'"Part of #{parent_issue}" line; the parent inherits nothing',
            slice=slice_number,
            issue=parent_issue,
        ),
    )


def _slice_rule_checks(
    repository: str,
    open_by_number: dict[int, board.Issue],
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
) -> tuple[SliceCheck, ...]:
    checks: list[SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason)
    if out_of_order is not None:
        checks.append(out_of_order)
    state, title, body = _issue_reference_state(repository, open_by_number, issue)
    if state is ReferenceState.CLOSED:
        checks.append(
            SliceCheck("error", "closed-issue", f"issue #{issue} is closed", issue=issue)
        )
    elif state is ReferenceState.MISSING:
        checks.append(
            SliceCheck(
                "error", "missing-issue", f"issue #{issue} does not exist here", issue=issue
            )
        )
    if body is not None:
        for entry in board.parse_slice_table(body):
            checks.extend(_slice_table_entry_checks(repository, open_by_number, entry))
    if title is not None and body is not None:
        checks.extend(_parent_line_checks(title, body))
    return tuple(checks)


def _refuse_claim(json_mode: bool, issue: int | None, checks: tuple[SliceCheck, ...]) -> int:
    if json_mode:
        payload = {"refused": True, "issue": issue, "checks": [c.as_json() for c in checks]}
        print(json.dumps(payload))
        return 2
    for check in checks:
        print(check.render(), file=sys.stderr)
    return 2


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
        repository = checkout._repository(parsed.repo)
        client = github.GitHubIssueComments(repository)
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
            comments = client.list_protocol_candidates(protocol.LEDGER_ISSUE)
            claims = protocol.active_claims(comments)
            now = datetime.now(timezone.utc)
            if parsed.json:
                return _status_json(claims, parsed.issue, ledger, now=now)
            print(f"LEDGER #{ledger}")
            return _status(claims, parsed.issue, now=now)
        if parsed.command == "board":
            comments = client.list_protocol_candidates(protocol.LEDGER_ISSUE)
            projected = _board(client, protocol.active_claims(comments))
            print(board.board_json(projected) if parsed.json else board.render(projected))
            return 0
        if parsed.command == "next":
            comments = client.list_protocol_candidates(protocol.LEDGER_ISSUE)
            projected = _board(client, protocol.active_claims(comments))
            item = board.highest_scored_actionable(projected)
            skipped = _unworkable(projected)
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
            allow_directory_reason = parsed.allow_directory
            if allow_directory_reason is not None:
                allow_directory_reason = protocol._outbound_text(
                    allow_directory_reason, "allow-directory reason", maximum=512
                )
            if add:
                versioned = checkout.versioned_paths()
                _reject_uncut_directory_scope(
                    client, identity, add, allow_directory_reason
                )
                selected = protocol._select_rescope_claim(
                    protocol._ledger_claims(client),
                    identity,
                    parsed.agent,
                    parsed.claim_id,
                    branch=rescope_branch,
                )
                _reject_oversized_scope(
                    protocol._combined_scope(selected.scope, add, drop),
                    allow_directory_reason,
                    versioned,
                )
            rescoped = protocol.rescope_claim(
                client,
                identity,
                parsed.agent,
                add,
                drop,
                parsed.claim_id,
                branch=rescope_branch,
                allow_directory_reason=allow_directory_reason,
            )
            if parsed.json:
                return _rescope_json(rescoped)
            print(f"RESCOPED {_claim_subject(rescoped)}: {rescoped.claim_id}")
            return 0
        if parsed.command == "claim":
            requested = _request(parsed)
            versioned = checkout.versioned_paths()
            _reject_uncut_directory_scope(
                client,
                requested.identity,
                requested.scope,
                requested.allow_directory_reason,
            )
            n, total, share = _reject_oversized_scope(
                requested.scope, requested.allow_directory_reason, versioned
            )
            checks: tuple[SliceCheck, ...] = ()
            target_issue: int | None = None
            if isinstance(requested.identity, protocol.IssueIdentity):
                target_issue = requested.identity.issue
                replayed = protocol.matching_claim_retry(
                    protocol._ledger_claims(client), requested
                )
                if replayed is None:
                    open_issues = client.list_open_board_issues()
                    open_by_number = {issue.number: issue for issue in open_issues}
                    projected = _board(
                        client, protocol._ledger_claims(client), issues=open_issues
                    )
                    checks = _slice_rule_checks(
                        repository,
                        open_by_number,
                        target_issue,
                        projected,
                        requested.out_of_order_reason,
                    )
            if any(check.level == "error" for check in checks):
                return _refuse_claim(parsed.json, target_issue, checks)
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
            except protocol.ClaimPostedReconcileFailed as error:
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
                    versioned_files=n,
                    versioned_files_total=total,
                    share=share,
                    touches=touches,
                    checks=checks,
                )
            print(f"CLAIMED {_claim_subject(claimed)}: {claimed.claim_id} {claimed.comment.url}")
            print(_claim_cost_line(n, total, touches))
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
