"""Claim-ledger protocol: markers, claims, projections, and reconciliation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol

CLAIM_LABEL_PREFIX = "agent-claim:active:"
# The protocol core is configured by discovery/bootstrap before every CLI action.
LEDGER_ISSUE = 0
LEGACY_MARKER_PREFIX = "<!-- agent-claim:v1 "
MARKER_PREFIX = "<!-- agent-claim:v2 "
MARKER_SUFFIX = " -->"
PROJECTION_MARKER_PREFIX = "<!-- agent-claim-projection:v1 ledger="
PROJECTION_MARKER_PATTERN = re.compile(
    rf"{re.escape(PROJECTION_MARKER_PREFIX)}(?P<ledger>[1-9][0-9]*){re.escape(MARKER_SUFFIX)}"
)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CLAIM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}"
)
MAX_PROTOCOL_EVENTS = 4096
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
MAX_COMMENT_BYTES = 48 * 1024
MAX_SCOPE_ENTRIES = 256
MAX_SCOPE_PATH_LENGTH = 512
DEFAULT_RELEASE_REASON = "landed"


class ClaimError(RuntimeError):
    pass


class ClaimUnavailable(ClaimError):
    pass


class InvalidClaimMarker(ClaimError):
    pass


LEDGER_BODY_MARKER = "<!-- agent-claim-ledger:v1 -->"
LEDGER_LABEL = "agent-claim-ledger"


def configure_ledger(issue: int) -> None:
    """Bind the otherwise protocol-only core to this repository's ledger generation."""
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise ClaimError("ledger issue must be a positive integer")
    global LEDGER_ISSUE
    LEDGER_ISSUE = issue


@dataclass(frozen=True)
class IssueComment:
    identifier: int
    created_at: str
    updated_at: str
    body: str
    author_association: str
    url: str


@dataclass(frozen=True)
class ActiveClaim:
    issue: int
    claim_id: str
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    comment: IssueComment


@dataclass(frozen=True)
class ClaimantRelease:
    issue: int
    claim_id: str
    agent: str
    role: str
    reason: str
    comment: IssueComment


@dataclass(frozen=True)
class OverrideRelease:
    issue: int
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    comment: IssueComment


@dataclass(frozen=True)
class LedgerSupersede:
    issue: int
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    successor_issue: int
    comment: IssueComment


ClaimEvent = ActiveClaim | ClaimantRelease | OverrideRelease | LedgerSupersede


class LedgerSuperseded(ClaimError):
    def __init__(self, successor_issue: int, claim: ActiveClaim):
        self.successor_issue = successor_issue
        self.claim = claim
        super().__init__(
            f"claim ledger #{LEDGER_ISSUE} is frozen; update and use "
            f"successor #{successor_issue}"
        )


@dataclass(frozen=True)
class ClaimRequest:
    issue: int
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    claim_id: str


class IssueComments(Protocol):
    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]: ...

    def post_comment(self, issue: int, body: str) -> str: ...

    def add_label(self, issue: int, label: str) -> None: ...

    def remove_label(self, issue: int, label: str) -> None: ...

    def list_claimed_issues(self) -> tuple[int, ...]: ...

    def validate_successor(self, issue: int) -> None: ...

    def upsert_projection(
        self,
        issue: int,
        body: str,
        *,
        create: bool = True,
        adopt_stale: bool = False,
    ) -> bool: ...


def claim_label(ledger_issue: int | None = None) -> str:
    return f"{CLAIM_LABEL_PREFIX}{ledger_issue or LEDGER_ISSUE}"


def _projection_marker(ledger_issue: int | None = None) -> str:
    return f"{PROJECTION_MARKER_PREFIX}{ledger_issue or LEDGER_ISSUE}{MARKER_SUFFIX}"


def _projection_ledger(comment: IssueComment) -> int | None:
    match = PROJECTION_MARKER_PATTERN.fullmatch(comment.body.partition("\n")[0])
    return int(match["ledger"]) if match is not None else None


def _required_text(
    payload: dict[str, object], key: str, *, maximum: int = 512
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise InvalidClaimMarker(f"claim marker field {key!r} must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise InvalidClaimMarker(
            f"claim marker field {key!r} must be one bounded non-empty line"
        )
    return normalized


def _outbound_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ClaimError(f"{field} must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ClaimError(f"{field} must be one bounded non-empty line")
    return normalized


def _required_issue(payload: dict[str, object]) -> int:
    issue = payload.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise InvalidClaimMarker("claim marker issue must be a positive integer")
    return issue


def _valid_branch(payload: dict[str, object]) -> str:
    branch = _required_text(payload, "branch", maximum=255)
    segments = branch.split("/")
    if (
        BRANCH_PATTERN.fullmatch(branch) is None
        or branch.startswith("-")
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or any(
            not segment
            or segment.startswith(".")
            or segment.endswith((".", ".lock"))
            for segment in segments
        )
    ):
        raise InvalidClaimMarker(f"claim marker branch is not a safe Git ref: {branch!r}")
    return branch


def _valid_scope(scope: object) -> tuple[str, ...]:
    if not isinstance(scope, list) or not scope:
        raise InvalidClaimMarker("claim marker scope must be a non-empty list")
    if len(scope) > MAX_SCOPE_ENTRIES:
        raise InvalidClaimMarker(
            f"claim marker scope exceeds {MAX_SCOPE_ENTRIES} entries"
        )
    result: list[str] = []
    for raw_path in scope:
        if not isinstance(raw_path, str):
            raise InvalidClaimMarker("claim scope entries must be text")
        path = raw_path.strip()
        if (
            not path
            or path != raw_path
            or len(path) > MAX_SCOPE_PATH_LENGTH
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise InvalidClaimMarker("claim scope entries must be canonical bounded paths")
        parsed = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        if (
            path == "."
            or parsed.is_absolute()
            or windows_path.drive
            or windows_path.root
            or ".." in parsed.parts
            or path.startswith("~")
            or not parsed.parts
            or parsed.parts[0] == ".git"
            or str(parsed) != path
        ):
            raise InvalidClaimMarker(f"claim scope must be repository-relative: {path!r}")
        result.append(path)
    if len(set(result)) != len(result):
        raise InvalidClaimMarker("claim scope contains duplicate paths")
    return tuple(result)


def _strict_keys(
    payload: dict[str, object], expected: frozenset[str], comment: IssueComment
) -> None:
    observed = frozenset(payload)
    if observed != expected:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} claim fields differ: "
            f"expected {sorted(expected)}, got {sorted(observed)}"
        )


def is_protocol_candidate(comment: IssueComment) -> bool:
    first_line = comment.body.partition("\n")[0]
    return comment.author_association in TRUSTED_ASSOCIATIONS and first_line.startswith(
        (LEGACY_MARKER_PREFIX, MARKER_PREFIX)
    )


def _marker_payload(comment: IssueComment) -> tuple[dict[str, object], bool] | None:
    if not is_protocol_candidate(comment):
        return None
    first_line = comment.body.partition("\n")[0]
    legacy = first_line.startswith(LEGACY_MARKER_PREFIX)
    prefix = LEGACY_MARKER_PREFIX if legacy else MARKER_PREFIX
    if not first_line.startswith(prefix):
        return None
    if comment.created_at != comment.updated_at:
        raise InvalidClaimMarker(
            f"trusted protocol comment {comment.url} was edited after publication"
        )
    if not first_line.endswith(MARKER_SUFFIX):
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} has an unterminated claim marker"
        )
    encoded = first_line[len(prefix) : -len(MARKER_SUFFIX)]
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} has invalid claim JSON"
        ) from error
    if not isinstance(payload, dict):
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} claim payload must be an object"
        )
    return payload, legacy


def _event_identity(
    payload: dict[str, object], comment: IssueComment
) -> tuple[str, str, str]:
    claim_id = _required_text(payload, "claim_id", maximum=128)
    agent = _required_text(payload, "agent", maximum=128)
    role = _required_text(payload, "role", maximum=64)
    visible_lines = [line for line in comment.body.splitlines() if line.strip()]
    if not visible_lines or visible_lines[-1] != f"Agent: {agent} ({role})":
        raise InvalidClaimMarker(
            f"trusted protocol comment {comment.url} lacks its exact agent attribution"
        )
    if CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
        raise InvalidClaimMarker(f"trusted comment {comment.url} has an invalid claim id")
    return claim_id, agent, role


def _parse_active_claim(
    payload: dict[str, object], comment: IssueComment, issue: int, *, legacy: bool
) -> ActiveClaim:
    expected = {"action", "agent", "base", "branch", "claim_id", "role", "scope"}
    if not legacy:
        expected.add("issue")
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    base = _required_text(payload, "base", maximum=40)
    if COMMIT_PATTERN.fullmatch(base) is None:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} base must be a full lowercase commit SHA"
        )
    return ActiveClaim(
        issue=issue,
        claim_id=claim_id,
        agent=agent,
        role=role,
        base=base,
        branch=_valid_branch(payload),
        scope=_valid_scope(payload.get("scope")),
        comment=comment,
    )


def _parse_claimant_release(
    payload: dict[str, object], comment: IssueComment, issue: int, *, legacy: bool
) -> ClaimantRelease:
    expected = {"action", "agent", "claim_id", "reason", "role"}
    if not legacy:
        expected.add("issue")
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    return ClaimantRelease(
        issue=issue,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        comment=comment,
    )


def _required_comment_id(payload: dict[str, object], *, action: str) -> int:
    raw_comment_id = payload.get("claim_comment_id")
    if (
        isinstance(raw_comment_id, bool)
        or not isinstance(raw_comment_id, int)
        or raw_comment_id < 1
    ):
        raise InvalidClaimMarker(f"{action} requires a positive claim comment id")
    return raw_comment_id


def _parse_override_release(
    payload: dict[str, object], comment: IssueComment, issue: int
) -> OverrideRelease:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_comment_id",
                "claim_id",
                "issue",
                "reason",
                "role",
            }
        ),
        comment,
    )
    claim_id, agent, role = _event_identity(payload, comment)
    if role != "coordinator":
        raise InvalidClaimMarker("override releases require coordinator role")
    return OverrideRelease(
        issue=issue,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        claim_comment_id=_required_comment_id(payload, action="override releases"),
        comment=comment,
    )


def _parse_ledger_supersede(
    payload: dict[str, object], comment: IssueComment, issue: int
) -> LedgerSupersede:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_comment_id",
                "claim_id",
                "issue",
                "reason",
                "role",
                "successor_issue",
            }
        ),
        comment,
    )
    claim_id, agent, role = _event_identity(payload, comment)
    if role != "coordinator":
        raise InvalidClaimMarker("ledger supersede requires coordinator role")
    successor_issue = payload.get("successor_issue")
    if (
        isinstance(successor_issue, bool)
        or not isinstance(successor_issue, int)
        or successor_issue < 1
        or successor_issue <= LEDGER_ISSUE
    ):
        raise InvalidClaimMarker("ledger successor must be greater than the current ledger")
    return LedgerSupersede(
        issue=issue,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        claim_comment_id=_required_comment_id(payload, action="ledger supersede"),
        successor_issue=successor_issue,
        comment=comment,
    )


def parse_claim_event(comment: IssueComment) -> ClaimEvent | None:
    parsed_marker = _marker_payload(comment)
    if parsed_marker is None:
        return None
    payload, legacy = parsed_marker
    action = _required_text(payload, "action", maximum=32)
    if action not in {"claim", "release", "override_release", "supersede"}:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} has unknown action {action!r}"
        )

    if legacy:
        if action not in {"claim", "release"}:
            raise InvalidClaimMarker("legacy claim markers cannot use this action")
        issue = LEDGER_ISSUE
    else:
        issue = _required_issue(payload)
    if action == "claim":
        return _parse_active_claim(payload, comment, issue, legacy=legacy)
    if action == "release":
        return _parse_claimant_release(payload, comment, issue, legacy=legacy)
    if action == "override_release":
        return _parse_override_release(payload, comment, issue)
    return _parse_ledger_supersede(payload, comment, issue)


def _apply_terminal_event(
    event: ClaimantRelease | OverrideRelease | LedgerSupersede,
    active: dict[str, ActiveClaim],
    acquired: dict[str, ActiveClaim],
) -> None:
    claimed = acquired.get(event.claim_id)
    if isinstance(event, LedgerSupersede):
        if (
            claimed is None
            or claimed.issue != event.issue
            or claimed.issue != LEDGER_ISSUE
            or event.claim_comment_id != claimed.comment.identifier
            or set(active) != {claimed.claim_id}
        ):
            return
        raise LedgerSuperseded(event.successor_issue, claimed)
    if claimed is None:
        raise InvalidClaimMarker(
            f"claim id {event.claim_id!r} was released before it was acquired"
        )
    if claimed.issue != event.issue:
        raise InvalidClaimMarker(
            f"claim id {event.claim_id!r} release targets the wrong issue"
        )
    if isinstance(event, ClaimantRelease):
        if (claimed.agent, claimed.role) != (event.agent, event.role):
            raise InvalidClaimMarker(
                f"claim id {event.claim_id!r} can only be released by its claimant"
            )
    elif event.claim_comment_id != claimed.comment.identifier:
        raise InvalidClaimMarker(
            f"claim id {event.claim_id!r} terminal event targets the wrong claim comment"
        )
    active.pop(event.claim_id, None)


def active_claims(comments: tuple[IssueComment, ...]) -> tuple[ActiveClaim, ...]:
    active: dict[str, ActiveClaim] = {}
    acquired: dict[str, ActiveClaim] = {}
    seen_claim_ids: set[str] = set()
    ordered = sorted(comments, key=lambda comment: (comment.created_at, comment.identifier))
    for comment in ordered:
        event = parse_claim_event(comment)
        if event is None:
            continue
        if isinstance(event, ActiveClaim):
            if event.claim_id in seen_claim_ids:
                raise InvalidClaimMarker(f"claim id {event.claim_id!r} was reused")
            seen_claim_ids.add(event.claim_id)
            acquired[event.claim_id] = event
            active[event.claim_id] = event
            continue
        _apply_terminal_event(event, active, acquired)

    return tuple(
        sorted(
            active.values(),
            key=lambda event: (event.comment.created_at, event.comment.identifier),
        )
    )


def _scope_prefixes(paths: tuple[str, ...]) -> set[tuple[str, ...]]:
    prefixes: set[tuple[str, ...]] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        prefixes.update(parts[:length] for length in range(1, len(parts) + 1))
    return prefixes


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_paths = {PurePosixPath(path).parts for path in left}
    right_paths = {PurePosixPath(path).parts for path in right}
    return bool(
        left_paths.intersection(_scope_prefixes(right))
        or right_paths.intersection(_scope_prefixes(left))
    )


def claims_conflict(left: ActiveClaim | ClaimRequest, right: ActiveClaim | ClaimRequest) -> bool:
    if left.issue == right.issue:
        return True
    return _scopes_overlap(left.scope, right.scope)


def conflicting_claims(
    claims: tuple[ActiveClaim, ...], candidate: ActiveClaim | ClaimRequest
) -> tuple[ActiveClaim, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != candidate.claim_id and claims_conflict(claim, candidate)
    )


@dataclass(frozen=True)
class ClaimConflictIndex:
    conflict_ids: set[str]
    claims_by_issue: dict[int, set[str]]
    complete_paths: dict[tuple[str, ...], set[str]]
    descendant_paths: dict[tuple[str, ...], set[str]]


def _claim_conflict_index(claims: tuple[ActiveClaim, ...]) -> ClaimConflictIndex:
    """Index active-claim paths once for conflict status and targeted lookup."""
    conflict_ids: set[str] = set()
    claims_by_issue: dict[int, set[str]] = {}
    complete_paths: dict[tuple[str, ...], set[str]] = {}
    descendant_paths: dict[tuple[str, ...], set[str]] = {}

    for claim in claims:
        same_issue = claims_by_issue.setdefault(claim.issue, set())
        if same_issue:
            conflict_ids.add(claim.claim_id)
            conflict_ids.update(same_issue)
        same_issue.add(claim.claim_id)

        for path in claim.scope:
            parts = PurePosixPath(path).parts
            matches = set(descendant_paths.get(parts, ()))
            for length in range(1, len(parts) + 1):
                matches.update(complete_paths.get(parts[:length], ()))
            matches.discard(claim.claim_id)
            if matches:
                conflict_ids.add(claim.claim_id)
                conflict_ids.update(matches)

            complete_paths.setdefault(parts, set()).add(claim.claim_id)
            for length in range(1, len(parts) + 1):
                descendant_paths.setdefault(parts[:length], set()).add(claim.claim_id)

    return ClaimConflictIndex(
        conflict_ids,
        claims_by_issue,
        complete_paths,
        descendant_paths,
    )


def _related_claim_ids(
    index: ClaimConflictIndex, selected: tuple[ActiveClaim, ...]
) -> set[str]:
    related = {claim.claim_id for claim in selected}
    for claim in selected:
        related.update(index.claims_by_issue[claim.issue])
        for path in claim.scope:
            parts = PurePosixPath(path).parts
            related.update(index.descendant_paths.get(parts, ()))
            for length in range(1, len(parts) + 1):
                related.update(index.complete_paths.get(parts[:length], ()))
    return related


def _marker(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"{MARKER_PREFIX}{encoded}{MARKER_SUFFIX}"


def _validated_comment(body: str) -> str:
    if "\x00" in body:
        raise ClaimError("GitHub comment body contains a NUL byte")
    size = len(body.encode("utf-8"))
    if size > MAX_COMMENT_BYTES:
        raise ClaimError(
            f"GitHub comment body exceeds the {MAX_COMMENT_BYTES}-byte safety limit"
        )
    return body


def claim_comment(request: ClaimRequest) -> str:
    agent = _outbound_text(request.agent, "agent", maximum=128)
    role = _outbound_text(request.role, "role", maximum=64)
    payload: dict[str, object] = {
        "action": "claim",
        "agent": agent,
        "base": request.base,
        "branch": request.branch,
        "claim_id": request.claim_id,
        "issue": request.issue,
        "role": role,
        "scope": list(request.scope),
    }
    scope = "\n".join(f"- `{path}`" for path in request.scope)
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## CLAIM — exclusive build lane\n\n"
        f"- Issue: #{request.issue}\n"
        f"- Owner: {agent} ({role})\n"
        f"- Base: `{request.base}`\n"
        f"- Branch: `{request.branch}`\n"
        f"- Claim ID: `{request.claim_id}`\n"
        "- Write scope:\n"
        f"{scope}\n\n"
        "Repository-wide ledger event. No edit starts before this claim is re-read live. "
        "Read-only review remains parallel. No Auto-Runner.\n\n"
        f"Agent: {agent} ({role})"
    )


def release_comment(
    claim: ActiveClaim,
    agent: str,
    role: str,
    reason: str,
    *,
    coordinator_override: bool = False,
) -> str:
    validated_agent = _outbound_text(agent, "agent", maximum=128)
    validated_role = _outbound_text(role, "role", maximum=64)
    validated_reason = _outbound_text(reason, "reason", maximum=512)
    action = "override_release" if coordinator_override else "release"
    payload: dict[str, object] = {
        "action": action,
        "agent": validated_agent,
        "claim_id": claim.claim_id,
        "issue": claim.issue,
        "reason": validated_reason,
        "role": validated_role,
    }
    if coordinator_override:
        payload["claim_comment_id"] = claim.comment.identifier
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## RELEASE — build lane\n\n"
        f"- Issue: #{claim.issue}\n"
        f"- Claim ID: `{claim.claim_id}`\n"
        f"- Previous owner: {claim.agent} ({claim.role})\n"
        f"- Released by: {validated_agent} ({validated_role})\n"
        f"- Reason: {validated_reason}\n\n"
        f"Agent: {validated_agent} ({validated_role})"
    )


def supersede_comment(
    claim: ActiveClaim,
    successor_issue: int,
    agent: str,
    role: str,
    reason: str,
) -> str:
    if successor_issue <= LEDGER_ISSUE:
        raise ClaimError("ledger successor must be greater than the current ledger")
    validated_agent = _outbound_text(agent, "agent", maximum=128)
    validated_role = _outbound_text(role, "role", maximum=64)
    validated_reason = _outbound_text(reason, "reason", maximum=512)
    payload: dict[str, object] = {
        "action": "supersede",
        "agent": validated_agent,
        "claim_comment_id": claim.comment.identifier,
        "claim_id": claim.claim_id,
        "issue": claim.issue,
        "reason": validated_reason,
        "role": validated_role,
        "successor_issue": successor_issue,
    }
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## SUPERSEDE — claim ledger frozen\n\n"
        f"- Ledger: #{LEDGER_ISSUE}\n"
        f"- Successor: #{successor_issue}\n"
        f"- Rollover claim: `{claim.claim_id}`\n"
        f"- Frozen by: {validated_agent} ({validated_role})\n"
        f"- Reason: {validated_reason}\n\n"
        "This terminal event rejects every later operation through helpers that still "
        "target this ledger. Update before coordinating more work.\n\n"
        f"Agent: {validated_agent} ({validated_role})"
    )


def _active_projection(claim: ActiveClaim) -> str:
    return _validated_comment(
        f"{_projection_marker()}\n"
        f"🔒 **Claimed** · {claim.agent} ({claim.role}) · `{claim.branch}`\n\n"
        f"[Ledger details]({claim.comment.url})"
    )


def _unclaimed_projection(
    ledger_url: str | None = None, reason: str | None = None
) -> str:
    detail = f" · {reason}" if reason else ""
    ledger = f"[Ledger]({ledger_url})" if ledger_url else f"Ledger: #{LEDGER_ISSUE}"
    return _validated_comment(
        f"{_projection_marker()}\n"
        f"🔓 **Unclaimed**{detail}\n\n"
        f"{ledger}"
    )


def _ledger_claims(client: IssueComments) -> tuple[ActiveClaim, ...]:
    return active_claims(client.list_protocol_candidates(LEDGER_ISSUE))


def _issue_claim(claims: tuple[ActiveClaim, ...], issue: int) -> ActiveClaim | None:
    matching = tuple(claim for claim in claims if claim.issue == issue)
    if not matching:
        return None
    return min(
        matching,
        key=lambda claim: (claim.comment.created_at, claim.comment.identifier),
    )


def _apply_issue_projection(
    client: IssueComments,
    issue: int,
    claim: ActiveClaim | None,
    *,
    unclaimed_body: str | None = None,
) -> None:
    if issue == LEDGER_ISSUE:
        return
    if claim is None:
        client.upsert_projection(
            issue,
            unclaimed_body or _unclaimed_projection(),
            create=False,
        )
        return
    client.upsert_projection(
        issue,
        _active_projection(claim),
        adopt_stale=True,
    )


def reconcile_issue_label(
    client: IssueComments,
    issue: int,
    *,
    unclaimed_body: str | None = None,
) -> None:
    for _ in range(3):
        try:
            expected = _issue_claim(_ledger_claims(client), issue)
        except LedgerSuperseded:
            client.remove_label(issue, claim_label())
            raise
        _apply_issue_projection(
            client,
            issue,
            expected,
            unclaimed_body=unclaimed_body,
        )
        if expected is not None:
            client.add_label(issue, claim_label())
        else:
            client.remove_label(issue, claim_label())
        try:
            observed = _issue_claim(_ledger_claims(client), issue)
        except LedgerSuperseded:
            client.remove_label(issue, claim_label())
            raise
        if (observed.claim_id if observed else None) == (
            expected.claim_id if expected else None
        ):
            return
    raise ClaimError(f"issue #{issue} claim label changed repeatedly during reconciliation")


def reconcile_all_labels(client: IssueComments) -> tuple[int, ...]:
    try:
        active_issues = {claim.issue for claim in _ledger_claims(client)}
    except LedgerSuperseded:
        for issue in client.list_claimed_issues():
            client.remove_label(issue, claim_label())
        raise
    known_issues = active_issues | set(client.list_claimed_issues())
    for issue in sorted(known_issues):
        reconcile_issue_label(client, issue)
    return tuple(sorted(active_issues))


def acquire_claim(client: IssueComments, request: ClaimRequest) -> ActiveClaim:
    standing = _ledger_claims(client)
    blocked_by = conflicting_claims(standing, request)
    if blocked_by:
        owner = blocked_by[0]
        raise ClaimUnavailable(
            f"issue #{request.issue} or its scope is claimed by {owner.agent} "
            f"({owner.role}) on issue #{owner.issue} branch {owner.branch}"
        )

    client.post_comment(LEDGER_ISSUE, claim_comment(request))
    observed = _ledger_claims(client)
    own = next((claim for claim in observed if claim.claim_id == request.claim_id), None)
    if own is None:
        raise ClaimError(f"issue #{request.issue} did not expose the posted claim id")
    competitors = conflicting_claims(observed, own)
    winner = min(
        (own, *competitors),
        key=lambda claim: (claim.comment.created_at, claim.comment.identifier),
    )
    if winner.claim_id != request.claim_id:
        client.post_comment(
            LEDGER_ISSUE,
            release_comment(own, request.agent, request.role, "claim race lost"),
        )
        reconcile_issue_label(client, request.issue)
        reconcile_issue_label(client, winner.issue)
        raise ClaimUnavailable(
            f"issue #{request.issue} claim race lost to {winner.agent} "
            f"({winner.role}) on issue #{winner.issue} branch {winner.branch}"
        )

    reconcile_issue_label(client, request.issue)
    return own


def _require_coordinator_override(role: str | None, reason: str | None) -> None:
    if role != "coordinator":
        raise ClaimUnavailable("a coordinator override requires --role coordinator")
    if reason is None:
        raise ClaimUnavailable("a coordinator override requires --reason")


def release_claim(
    client: IssueComments,
    issue: int,
    agent: str,
    role: str | None,
    reason: str | None,
    claim_id: str | None,
    *,
    branch: str | None = None,
    coordinator_override: bool = False,
) -> ActiveClaim:
    if coordinator_override:
        _require_coordinator_override(role, reason)
    standing = tuple(claim for claim in _ledger_claims(client) if claim.issue == issue)
    if not standing:
        raise ClaimUnavailable(f"issue #{issue} has no active build claim")
    if claim_id is None:
        if not branch:
            raise ClaimUnavailable(
                "release without --claim-id requires a non-empty current branch; "
                "pass --claim-id"
            )
        matches = tuple(
            claim
            for claim in standing
            if claim.agent == agent and claim.branch == branch
        )
        if len(matches) != 1:
            raise ClaimUnavailable(
                f"issue #{issue} has no unique claim for this session on branch "
                f"{branch!r}; pass --claim-id"
            )
        selected = matches[0]
    else:
        selected = next(
            (claim for claim in standing if claim.claim_id == claim_id),
            None,
        )
        if selected is None:
            raise ClaimUnavailable(f"issue #{issue} has no active claim {claim_id!r}")
    if role is None:
        role = selected.role
    if not coordinator_override and (agent, role) != (selected.agent, selected.role):
        raise ClaimUnavailable(
            "only the original claimant may release; use an explicit coordinator override"
        )
    if reason is None:
        reason = DEFAULT_RELEASE_REASON

    ledger_url = client.post_comment(
        LEDGER_ISSUE,
        release_comment(
            selected,
            agent,
            role,
            reason,
            coordinator_override=coordinator_override,
        ),
    )
    reconcile_issue_label(
        client,
        issue,
        unclaimed_body=_unclaimed_projection(ledger_url, reason),
    )
    return selected


def supersede_ledger(
    client: IssueComments,
    successor_issue: int,
    agent: str,
    role: str,
    reason: str,
    claim_id: str,
) -> ActiveClaim:
    if role != "coordinator":
        raise ClaimUnavailable("ledger supersede requires --role coordinator")
    if successor_issue <= LEDGER_ISSUE:
        raise ClaimUnavailable("successor issue must be greater than the current ledger")
    try:
        standing = _ledger_claims(client)
    except LedgerSuperseded as error:
        if error.successor_issue != successor_issue or error.claim.claim_id != claim_id:
            raise
        client.remove_label(LEDGER_ISSUE, claim_label())
        return error.claim
    selected = next((claim for claim in standing if claim.claim_id == claim_id), None)
    if (
        selected is None
        or selected.issue != LEDGER_ISSUE
        or len(standing) != 1
    ):
        raise ClaimUnavailable(
            "ledger supersede requires the named claim to be the only active claim "
            "and to own the ledger issue"
        )
    client.validate_successor(successor_issue)
    client.post_comment(
        LEDGER_ISSUE,
        supersede_comment(selected, successor_issue, agent, role, reason),
    )
    try:
        _ledger_claims(client)
    except LedgerSuperseded as error:
        if error.successor_issue == successor_issue and error.claim == selected:
            client.remove_label(LEDGER_ISSUE, claim_label())
            return selected
        raise
    raise ClaimError("ledger supersede event was not observed after publication")

