"""Claim-ledger protocol: markers, claims, projections, and reconciliation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Protocol

CLAIM_LABEL_PREFIX = "agent-claim:active:"
# The protocol core is configured by discovery/bootstrap before every CLI action.
LEDGER_ISSUE = 0
LEGACY_MARKER_PREFIX = "<!-- agent-claim:v1 "
MARKER_PREFIX = "<!-- agent-claim:v2 "
MARKER_SUFFIX = " -->"
# Coordination-contract convention: the only branch prefixes an issueless lane claim
# may use, so a builder that forgot its issue number never gets a silent, unlabeled,
# non-projected claim instead of a loud refusal.
ISSUELESS_LANE_BRANCH_PREFIXES = ("docs/", "fix/")
PROJECTION_MARKER_PREFIX = "<!-- agent-claim-projection:v1 ledger="
PROJECTION_MARKER_PATTERN = re.compile(
    rf"{re.escape(PROJECTION_MARKER_PREFIX)}(?P<ledger>[1-9][0-9]*){re.escape(MARKER_SUFFIX)}"
)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CLAIM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RESOURCE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
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
class IssueIdentity:
    """A claim scoped to one numbered GitHub issue."""

    issue: int

    def __post_init__(self) -> None:
        if isinstance(self.issue, bool) or not isinstance(self.issue, int) or self.issue < 1:
            raise ClaimError("issue identity must be a positive integer")


@dataclass(frozen=True)
class LaneIdentity:
    """A claim scoped to one issueless `docs/`/`fix/` lane.

    Carries no branch of its own: the lane name is owned entirely by the enclosing
    `ActiveClaim`/`ClaimRequest.branch`, which every lane claim already has. A second
    branch-shaped field here would give the branch two owners that could drift apart.
    """


ClaimIdentity = IssueIdentity | LaneIdentity


@dataclass(frozen=True)
class ResourceHold:
    """One named scarce value held by a live claim until land or release."""

    name: str
    value: int


@dataclass(frozen=True)
class ActiveClaim:
    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    comment: IssueComment
    resource: ResourceHold | None = None
    requested_resource: str | None = None


@dataclass(frozen=True)
class ClaimantRelease:
    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    reason: str
    comment: IssueComment


@dataclass(frozen=True)
class OverrideRelease:
    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    comment: IssueComment


@dataclass(frozen=True)
class ClaimRescope:
    """Append-only scope replacement for a still-active claim.

    Keeps claim id, identity, agent, role, base, and branch. Old helpers that
    do not know this action fail loud on the whole ledger.
    """

    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    scope: tuple[str, ...]
    comment: IssueComment


@dataclass(frozen=True)
class LedgerSupersede:
    """Terminal event that freezes the whole ledger; always issue(ledger)-scoped.

    No lane variant exists on purpose (Entschieden #6): the ledger itself is the
    numbered issue configured by `configure_ledger`, and a lane claim can never
    own it, so `issue` stays a plain int rather than a `ClaimIdentity`.
    """

    issue: int
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    successor_issue: int
    comment: IssueComment


ClaimEvent = ActiveClaim | ClaimantRelease | OverrideRelease | ClaimRescope | LedgerSupersede


class DuplicateClaimConflict(ClaimError):
    """A duplicate claim id where reconcile refuses to pick a winner silently."""

    def __init__(self, claim_id: str, superseded: ActiveClaim, survivor: ActiveClaim):
        self.claim_id = claim_id
        self.superseded = superseded
        self.survivor = survivor
        super().__init__(
            f"claim id {claim_id!r} has two still-active claims from different agents "
            f"({superseded.agent} {superseded.comment.url} vs {survivor.agent} "
            f"{survivor.comment.url}); release one manually, then run reconcile again"
        )


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
    identity: ClaimIdentity
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    claim_id: str
    out_of_order_reason: str | None = None
    allow_directory_reason: str | None = None
    resource: str | None = None
    resource_value: int | None = None


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

    def neutralize_claim_comment(self, comment_id: int, body: str) -> None: ...


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


def _outbound_resource_name(value: object) -> str:
    name = _outbound_text(value, "resource", maximum=64)
    if RESOURCE_NAME_PATTERN.fullmatch(name) is None:
        raise ClaimError("resource is not a resource name")
    return name


def _required_issue(payload: dict[str, object]) -> int:
    issue = payload.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise InvalidClaimMarker("claim marker issue must be a positive integer")
    return issue


LANE_MARKER_KEY = "lane"


def _required_identity(payload: dict[str, object]) -> ClaimIdentity:
    """Dispatch a claim/release/override_release marker to its identity kind.

    Checks for the lane marker key before ever requiring `issue`, so a lane event
    never triggers `_required_issue`: the natural hard stop for an old reader (which
    always calls `_required_issue` unconditionally) happens exactly there instead of
    in a try/except that would silently skip the comment.
    """
    has_issue = "issue" in payload
    has_lane = LANE_MARKER_KEY in payload
    if has_issue and has_lane:
        raise InvalidClaimMarker("claim marker must not carry both issue and lane")
    if has_lane:
        if payload[LANE_MARKER_KEY] is not True:
            raise InvalidClaimMarker("claim marker lane field must be true")
        return LaneIdentity()
    return IssueIdentity(_required_issue(payload))


def _identity_marker_key(identity: ClaimIdentity) -> str:
    return LANE_MARKER_KEY if isinstance(identity, LaneIdentity) else "issue"


def _identity_marker_value(identity: ClaimIdentity) -> int | bool:
    return True if isinstance(identity, LaneIdentity) else identity.issue


def _identity_label(identity: ClaimIdentity, branch: str) -> str:
    """Human-readable subject line for a claim/release comment body."""
    if isinstance(identity, LaneIdentity):
        return f"Lane: `{branch}`"
    return f"Issue: #{identity.issue}"


def _identity_summary(identity: ClaimIdentity, branch: str) -> str:
    """Human-readable subject for error messages, distinct from `_identity_label`."""
    return f"lane {branch!r}" if isinstance(identity, LaneIdentity) else f"issue #{identity.issue}"


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


def _scope_list_entries(scope: object) -> list[str]:
    """Expand a stored or CLI scope list into individual path strings.

    Each list entry may itself be comma-joined: `--scope a,b` equals
    `--scope a --scope b`. Existing ledger markers that stored one opaque
    comma-joined string are read the same way, so overlap detection covers
    them without rewriting the append-only comment.
    """
    if not isinstance(scope, list) or not scope:
        raise InvalidClaimMarker("claim marker scope must be a non-empty list")
    expanded: list[str] = []
    for raw_path in scope:
        if not isinstance(raw_path, str):
            raise InvalidClaimMarker("claim scope entries must be text")
        if raw_path.strip() != raw_path or not raw_path:
            raise InvalidClaimMarker("claim scope entries must be canonical bounded paths")
        pieces = [piece.strip() for piece in raw_path.split(",")]
        if any(not piece for piece in pieces):
            raise InvalidClaimMarker("claim scope entries must be canonical bounded paths")
        expanded.extend(pieces)
    if len(expanded) > MAX_SCOPE_ENTRIES:
        raise InvalidClaimMarker(
            f"claim marker scope exceeds {MAX_SCOPE_ENTRIES} entries"
        )
    return expanded


def _valid_scope(scope: object) -> tuple[str, ...]:
    result: list[str] = []
    for path in _scope_list_entries(scope):
        if (
            len(path) > MAX_SCOPE_PATH_LENGTH
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
    if observed == expected:
        return
    message = (
        f"trusted comment {comment.url} claim fields differ: "
        f"expected {sorted(expected)}, got {sorted(observed)}"
    )
    if observed > expected:
        message += (
            "; unknown fields in a trusted comment usually mean a newer "
            "agent-claim wrote this ledger - upgrade the installed tool"
        )
    raise InvalidClaimMarker(message)


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


def _valid_resource_name(name: str, *, field: str) -> str:
    if RESOURCE_NAME_PATTERN.fullmatch(name) is None:
        raise InvalidClaimMarker(f"{field} is not a resource name")
    return name


def _required_resource_hold(payload: dict[str, object], comment: IssueComment) -> ResourceHold:
    name = _required_text(payload, "resource", maximum=64)
    _valid_resource_name(name, field=f"trusted comment {comment.url} resource")
    value = payload.get("resource_value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} resource_value must be a positive integer"
        )
    return ResourceHold(name, value)


def _parse_active_claim(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity, *, legacy: bool
) -> ActiveClaim:
    expected = {"action", "agent", "base", "branch", "claim_id", "role", "scope"}
    if not legacy:
        expected.add(_identity_marker_key(identity))
    if "resource_value" in payload and "resource" not in payload:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} resource_value requires resource"
        )
    if "resource" in payload:
        expected.add("resource")
        if "resource_value" in payload:
            expected.add("resource_value")
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    base = _required_text(payload, "base", maximum=40)
    if COMMIT_PATTERN.fullmatch(base) is None:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} base must be a full lowercase commit SHA"
        )
    requested_resource = None
    resource = None
    if "resource" in payload:
        requested_resource = _required_text(payload, "resource", maximum=64)
        _valid_resource_name(
            requested_resource, field=f"trusted comment {comment.url} resource"
        )
        if "resource_value" in payload:
            resource = _required_resource_hold(payload, comment)
    return ActiveClaim(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        base=base,
        branch=_valid_branch(payload),
        scope=_valid_scope(payload.get("scope")),
        comment=comment,
        resource=resource,
        requested_resource=requested_resource,
    )


def _parse_claimant_release(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity, *, legacy: bool
) -> ClaimantRelease:
    expected = {"action", "agent", "claim_id", "reason", "role"}
    if not legacy:
        expected.add(_identity_marker_key(identity))
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    return ClaimantRelease(
        identity=identity,
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
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity
) -> OverrideRelease:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_comment_id",
                "claim_id",
                _identity_marker_key(identity),
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
        identity=identity,
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


def _parse_claim_rescope(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity
) -> ClaimRescope:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_id",
                _identity_marker_key(identity),
                "role",
                "scope",
            }
        ),
        comment,
    )
    claim_id, agent, role = _event_identity(payload, comment)
    return ClaimRescope(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        scope=_valid_scope(payload.get("scope")),
        comment=comment,
    )


def parse_claim_event(comment: IssueComment) -> ClaimEvent | None:
    parsed_marker = _marker_payload(comment)
    if parsed_marker is None:
        return None
    payload, legacy = parsed_marker
    action = _required_text(payload, "action", maximum=32)
    if action not in {"claim", "release", "override_release", "rescope", "supersede"}:
        raise InvalidClaimMarker(
            f"trusted comment {comment.url} has unknown action {action!r}"
        )

    if legacy:
        if action not in {"claim", "release"}:
            raise InvalidClaimMarker("legacy claim markers cannot use this action")
        if LEDGER_ISSUE < 1:
            # IssueIdentity itself would reject 0 as "not a positive integer", which
            # would misreport this as a marker defect; it is a caller/setup defect.
            raise ClaimError(
                "legacy claim marker cannot be parsed before configure_ledger "
                "binds the ledger issue"
            )
        identity: ClaimIdentity = IssueIdentity(LEDGER_ISSUE)
    elif action == "supersede":
        # Supersede stays ledger-issue-only (Entschieden #6): no lane branching here.
        identity = IssueIdentity(_required_issue(payload))
    else:
        identity = _required_identity(payload)
    if action == "claim":
        return _parse_active_claim(payload, comment, identity, legacy=legacy)
    if action == "release":
        return _parse_claimant_release(payload, comment, identity, legacy=legacy)
    if action == "override_release":
        return _parse_override_release(payload, comment, identity)
    if action == "rescope":
        return _parse_claim_rescope(payload, comment, identity)
    return _parse_ledger_supersede(payload, comment, identity.issue)


def _apply_terminal_event(
    event: ClaimantRelease | OverrideRelease | LedgerSupersede,
    active: dict[str, ActiveClaim],
    acquired: dict[str, ActiveClaim],
) -> bool:
    """Validate and apply a terminal event against the claim it targets.

    Returns whether the engine honored the event: accepted it as a real terminal
    event for the claim `acquired` currently holds for this id, whether or not
    popping `active` actually changed anything (an idempotent release retry, or a
    coordinator override after the claimant already released, still counts as
    honored). A `LedgerSupersede` that does not satisfy its narrow freeze window is
    never honored — it silently no-ops without being validated against any claim.
    """
    claimed = acquired.get(event.claim_id)
    if isinstance(event, LedgerSupersede):
        if (
            claimed is None
            or not isinstance(claimed.identity, IssueIdentity)
            or claimed.identity.issue != event.issue
            or claimed.identity.issue != LEDGER_ISSUE
            or event.claim_comment_id != claimed.comment.identifier
            or set(active) != {claimed.claim_id}
        ):
            return False
        raise LedgerSuperseded(event.successor_issue, claimed)
    if claimed is None:
        raise InvalidClaimMarker(
            f"claim id {event.claim_id!r} was released before it was acquired"
        )
    if claimed.identity != event.identity:
        raise InvalidClaimMarker(
            f"claim id {event.claim_id!r} release targets the wrong claim"
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
    return True


@dataclass(frozen=True)
class ClaimLedgerAggregate:
    """Non-raising result of one chronological walk over the ledger's claim events.

    `occurrences` holds every ActiveClaim event seen for a claim id, in ledger order;
    a duplicated id has more than one. `terminated_by`, when present for a claim id,
    holds every terminal-event comment `_apply_terminal_event` honored for it — a
    release retry or a coordinator override that lands after the claimant already
    released both still count, since the engine validated and accepted each one.
    An inert or foreign terminal event (one `_apply_terminal_event` never honored,
    such as a `LedgerSupersede` posted outside its narrow freeze window) never shows
    up here. Because `acquired` (below) only ever binds a claim id to its first-ever
    occurrence, every honored termination belongs to `occurrences[claim_id][0]`;
    later occurrences of a duplicated id are never tracked as "acquired" and so can
    never absorb a terminal event themselves.
    """

    active: tuple[ActiveClaim, ...]
    seen_claim_ids: frozenset[str]
    duplicate_claim_ids: tuple[str, ...]
    occurrences: Mapping[str, tuple[ActiveClaim, ...]]
    terminated_by: Mapping[str, tuple[IssueComment, ...]]


def _aggregate_claim_events(comments: tuple[IssueComment, ...]) -> ClaimLedgerAggregate:
    """Walk the ledger once, tolerating a reused claim id instead of raising on sight.

    The strict reader, the pre/post acquire guards, and the reconcile repair pass all
    consume this single walk, so duplicate-claim-id detection and release status can
    never drift between two independently maintained parsers.
    """
    active: dict[str, ActiveClaim] = {}
    acquired: dict[str, ActiveClaim] = {}
    occurrences: dict[str, list[ActiveClaim]] = {}
    terminated_by: dict[str, list[IssueComment]] = {}
    duplicate_claim_ids: list[str] = []
    ordered = sorted(comments, key=lambda comment: (comment.created_at, comment.identifier))
    for comment in ordered:
        event = parse_claim_event(comment)
        if event is None:
            continue
        if isinstance(event, ActiveClaim):
            occurrences.setdefault(event.claim_id, []).append(event)
            if event.claim_id in acquired:
                if event.claim_id not in duplicate_claim_ids:
                    duplicate_claim_ids.append(event.claim_id)
                continue
            acquired[event.claim_id] = event
            active[event.claim_id] = event
            continue
        if isinstance(event, ClaimRescope):
            current = active.get(event.claim_id)
            if current is None:
                if event.claim_id in acquired:
                    raise InvalidClaimMarker(
                        f"claim id {event.claim_id!r} was rescoped after it was released"
                    )
                raise InvalidClaimMarker(
                    f"claim id {event.claim_id!r} was rescoped before it was acquired"
                )
            if current.identity != event.identity:
                raise InvalidClaimMarker(
                    f"claim id {event.claim_id!r} rescope targets the wrong claim"
                )
            if (current.agent, current.role) != (event.agent, event.role):
                raise InvalidClaimMarker(
                    f"claim id {event.claim_id!r} can only be rescoped by its claimant"
                )
            active[event.claim_id] = replace(current, scope=event.scope)
            continue
        if _apply_terminal_event(event, active, acquired):
            terminated_by.setdefault(event.claim_id, []).append(event.comment)

    occurrence_map = {claim_id: tuple(events) for claim_id, events in occurrences.items()}
    derived_active = _apply_derived_resource_holds(active, occurrence_map)
    return ClaimLedgerAggregate(
        active=tuple(
            sorted(
                derived_active.values(),
                key=lambda event: (event.comment.created_at, event.comment.identifier),
            )
        ),
        seen_claim_ids=frozenset(acquired),
        duplicate_claim_ids=tuple(duplicate_claim_ids),
        occurrences=MappingProxyType(occurrence_map),
        terminated_by=MappingProxyType(
            {
                claim_id: tuple(terminal_comments)
                for claim_id, terminal_comments in terminated_by.items()
            }
        ),
    )


def _apply_derived_resource_holds(
    active: dict[str, ActiveClaim],
    occurrences: Mapping[str, tuple[ActiveClaim, ...]],
) -> dict[str, ActiveClaim]:
    """Assign auto resource values from occupied first-occurrence intents.

    Walk first-occurrence intents for a name in ledger order. Explicit intents occupy
    their posted value, including after release. Auto intents take the next positive
    integer not already occupied; a released auto still occupies the integer it would
    have been assigned. Among still-active claims, only the earliest live (name, value)
    pair stays the holder — later live explicit posts of that pair are stripped.
    """
    derived = dict(active)
    first_occurrences = [events[0] for events in occurrences.values()]
    names = sorted(
        {
            event.requested_resource
            for event in first_occurrences
            if event.requested_resource is not None
        }
    )
    for name in names:
        intents = sorted(
            (
                event
                for event in first_occurrences
                if event.requested_resource == name
            ),
            key=lambda event: (event.comment.created_at, event.comment.identifier),
        )
        occupied: set[int] = set()
        for intent in intents:
            if intent.resource is not None:
                occupied.add(intent.resource.value)
                continue
            value = 1
            while value in occupied:
                value += 1
            occupied.add(value)
            current = derived.get(intent.claim_id)
            if current is None:
                continue
            derived[intent.claim_id] = replace(current, resource=ResourceHold(name, value))
    holders: dict[tuple[str, int], list[ActiveClaim]] = {}
    first_by_id = {event.claim_id: event for event in first_occurrences}
    for claim in derived.values():
        if claim.resource is not None:
            holders.setdefault((claim.resource.name, claim.resource.value), []).append(claim)
    for held in holders.values():
        ordered = sorted(
            held, key=lambda claim: (claim.comment.created_at, claim.comment.identifier)
        )
        for loser in ordered[1:]:
            first = first_by_id.get(loser.claim_id)
            if first is None or first.resource is None:
                continue
            derived[loser.claim_id] = replace(loser, resource=None)
    return derived


def _reject_duplicate_claim_ids(aggregate: ClaimLedgerAggregate) -> None:
    if aggregate.duplicate_claim_ids:
        raise InvalidClaimMarker(f"claim id {aggregate.duplicate_claim_ids[0]!r} was reused")


def active_claims(comments: tuple[IssueComment, ...]) -> tuple[ActiveClaim, ...]:
    aggregate = _aggregate_claim_events(comments)
    _reject_duplicate_claim_ids(aggregate)
    return aggregate.active


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


def _identity_conflicts(
    left: ActiveClaim | ClaimRequest, right: ActiveClaim | ClaimRequest
) -> bool:
    """Two claims share an identity: same issue number, or same lane branch.

    A lane claim and an issue claim never share an identity by themselves — only
    scope overlap can put them in conflict.
    """
    match left.identity, right.identity:
        case IssueIdentity(issue=left_issue), IssueIdentity(issue=right_issue):
            return left_issue == right_issue
        case LaneIdentity(), LaneIdentity():
            return left.branch == right.branch
        case _:
            return False


def claims_conflict(left: ActiveClaim | ClaimRequest, right: ActiveClaim | ClaimRequest) -> bool:
    """True when two claims share an issue or lane branch.

    Path overlap is advisory: it is a visible note, not a conflict.
    """
    return _identity_conflicts(left, right)


def claims_overlap(left: ActiveClaim | ClaimRequest, right: ActiveClaim | ClaimRequest) -> bool:
    return _scopes_overlap(left.scope, right.scope)


def claims_holding_path(
    claims: tuple[ActiveClaim, ...], path: str
) -> tuple[ActiveClaim, ...]:
    target = _valid_scope([path])
    if len(target) != 1:
        raise ClaimError("who requires a single repository-relative path")
    return tuple(claim for claim in claims if _scopes_overlap(claim.scope, target))


def blocking_claims(
    claims: tuple[ActiveClaim, ...], candidate: ActiveClaim | ClaimRequest
) -> tuple[ActiveClaim, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != candidate.claim_id and claims_conflict(claim, candidate)
    )


def overlapping_claims(
    claims: tuple[ActiveClaim, ...], candidate: ActiveClaim | ClaimRequest
) -> tuple[ActiveClaim, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != candidate.claim_id and claims_overlap(claim, candidate)
    )


def conflicting_claims(
    claims: tuple[ActiveClaim, ...], candidate: ActiveClaim | ClaimRequest
) -> tuple[ActiveClaim, ...]:
    """Path-overlapping live claims, excluding identity. Used as the advisory note."""
    return overlapping_claims(claims, candidate)


IdentityKey = tuple[str, int | str]


def _identity_key(claim: ActiveClaim) -> IdentityKey:
    """Hashable index key for one claim's identity: an issue number or a lane branch.

    `LaneIdentity` instances all compare equal to each other, so indexing by
    identity alone would merge every lane into one bucket; the branch already owned
    by `ActiveClaim.branch` supplies the missing distinction without giving the
    lane name a second owner.
    """
    if isinstance(claim.identity, LaneIdentity):
        return (LANE_MARKER_KEY, claim.branch)
    return ("issue", claim.identity.issue)


@dataclass(frozen=True)
class ClaimConflictIndex:
    conflict_ids: set[str]
    overlap_ids: set[str]
    claims_by_identity: dict[IdentityKey, set[str]]
    complete_paths: dict[tuple[str, ...], set[str]]
    descendant_paths: dict[tuple[str, ...], set[str]]


def _claim_conflict_index(claims: tuple[ActiveClaim, ...]) -> ClaimConflictIndex:
    """Index identities and paths once for status conflict and overlap notes."""
    conflict_ids: set[str] = set()
    overlap_ids: set[str] = set()
    claims_by_identity: dict[IdentityKey, set[str]] = {}
    complete_paths: dict[tuple[str, ...], set[str]] = {}
    descendant_paths: dict[tuple[str, ...], set[str]] = {}

    for claim in claims:
        same_identity = claims_by_identity.setdefault(_identity_key(claim), set())
        if same_identity:
            conflict_ids.add(claim.claim_id)
            conflict_ids.update(same_identity)
        same_identity.add(claim.claim_id)

        for path in claim.scope:
            parts = PurePosixPath(path).parts
            matches = set(descendant_paths.get(parts, ()))
            for length in range(1, len(parts) + 1):
                matches.update(complete_paths.get(parts[:length], ()))
            matches.discard(claim.claim_id)
            if matches:
                overlap_ids.add(claim.claim_id)
                overlap_ids.update(matches)

            complete_paths.setdefault(parts, set()).add(claim.claim_id)
            for length in range(1, len(parts) + 1):
                descendant_paths.setdefault(parts[:length], set()).add(claim.claim_id)

    return ClaimConflictIndex(
        conflict_ids,
        overlap_ids,
        claims_by_identity,
        complete_paths,
        descendant_paths,
    )


def _related_claim_ids(
    index: ClaimConflictIndex, selected: tuple[ActiveClaim, ...]
) -> set[str]:
    related = {claim.claim_id for claim in selected}
    for claim in selected:
        related.update(index.claims_by_identity[_identity_key(claim)])
        related.update(_overlap_peer_ids(index, claim))
    return related


def _overlap_peer_ids(index: ClaimConflictIndex, claim: ActiveClaim) -> set[str]:
    related: set[str] = set()
    for path in claim.scope:
        parts = PurePosixPath(path).parts
        related.update(index.descendant_paths.get(parts, ()))
        for length in range(1, len(parts) + 1):
            related.update(index.complete_paths.get(parts[:length], ()))
    related.discard(claim.claim_id)
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
        _identity_marker_key(request.identity): _identity_marker_value(request.identity),
        "role": role,
        "scope": list(request.scope),
    }
    resource_line = ""
    if request.resource is not None:
        name = _outbound_resource_name(request.resource)
        payload["resource"] = name
        if request.resource_value is None:
            resource_line = f"- Resource: `{name}`\n"
        else:
            if (
                isinstance(request.resource_value, bool)
                or not isinstance(request.resource_value, int)
                or request.resource_value < 1
            ):
                raise ClaimError("resource value must be a positive integer")
            payload["resource_value"] = request.resource_value
            resource_line = f"- Resource: `{name}` = {request.resource_value}\n"
    scope = "\n".join(f"- `{path}`" for path in request.scope)
    out_of_order = ""
    if request.out_of_order_reason is not None:
        reason = _outbound_text(request.out_of_order_reason, "out-of-order reason", maximum=512)
        out_of_order = f"- Out-of-order reason: {reason}\n"
    allow_directory = ""
    if request.allow_directory_reason is not None:
        reason = _outbound_text(
            request.allow_directory_reason, "allow-directory reason", maximum=512
        )
        allow_directory = f"- Allow-directory reason: {reason}\n"
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## CLAIM — build lane\n\n"
        f"- {_identity_label(request.identity, request.branch)}\n"
        f"- Owner: {agent} ({role})\n"
        f"- Base: `{request.base}`\n"
        f"- Branch: `{request.branch}`\n"
        f"- Claim ID: `{request.claim_id}`\n"
        f"{resource_line}"
        f"{out_of_order}"
        f"{allow_directory}"
        "- Write scope:\n"
        f"{scope}\n\n"
        "Repository-wide ledger event. No edit starts before this claim is re-read live. "
        "Read-only review remains parallel. No Auto-Runner.\n\n"
        f"Agent: {agent} ({role})"
    )


def rescope_comment(
    claim: ActiveClaim,
    scope: tuple[str, ...],
    agent: str,
    role: str,
    allow_directory_reason: str | None = None,
) -> str:
    validated_agent = _outbound_text(agent, "agent", maximum=128)
    validated_role = _outbound_text(role, "role", maximum=64)
    payload: dict[str, object] = {
        "action": "rescope",
        "agent": validated_agent,
        "claim_id": claim.claim_id,
        _identity_marker_key(claim.identity): _identity_marker_value(claim.identity),
        "role": validated_role,
        "scope": list(scope),
    }
    scope_lines = "\n".join(f"- `{path}`" for path in scope)
    allow_directory = ""
    if allow_directory_reason is not None:
        reason = _outbound_text(allow_directory_reason, "allow-directory reason", maximum=512)
        allow_directory = f"- Allow-directory reason: {reason}\n"
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## RESCOPE — build lane\n\n"
        f"- {_identity_label(claim.identity, claim.branch)}\n"
        f"- Owner: {validated_agent} ({validated_role})\n"
        f"- Base: `{claim.base}`\n"
        f"- Branch: `{claim.branch}`\n"
        f"- Claim ID: `{claim.claim_id}`\n"
        f"{allow_directory}"
        "- Write scope:\n"
        f"{scope_lines}\n\n"
        "Repository-wide ledger event. Claim id and base are unchanged. "
        "No Auto-Runner.\n\n"
        f"Agent: {validated_agent} ({validated_role})"
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
        _identity_marker_key(claim.identity): _identity_marker_value(claim.identity),
        "reason": validated_reason,
        "role": validated_role,
    }
    if coordinator_override:
        payload["claim_comment_id"] = claim.comment.identifier
    return _validated_comment(
        f"{_marker(payload)}\n"
        "## RELEASE — build lane\n\n"
        f"- {_identity_label(claim.identity, claim.branch)}\n"
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
    if not isinstance(claim.identity, IssueIdentity):
        # Guardrail (Entschieden #6): supersede stays ledger-issue-only, never a lane.
        raise ClaimError("ledger supersede requires an issue-identified claim")
    validated_agent = _outbound_text(agent, "agent", maximum=128)
    validated_role = _outbound_text(role, "role", maximum=64)
    validated_reason = _outbound_text(reason, "reason", maximum=512)
    payload: dict[str, object] = {
        "action": "supersede",
        "agent": validated_agent,
        "claim_comment_id": claim.comment.identifier,
        "claim_id": claim.claim_id,
        "issue": claim.identity.issue,
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


def _neutralized_claim_body(claim_id: str, survivor: ActiveClaim) -> str:
    """Neutralize a duplicate claim-id event so it stops parsing as a claim marker.

    The edited body deliberately does not start with a claim marker prefix, so
    `is_protocol_candidate` excludes it: the ledger's "was edited after publication"
    guard for trusted protocol comments never sees it again.
    """
    return _validated_comment(
        "## SUPERSEDED — duplicate claim id neutralized by reconcile\n\n"
        f"- Claim ID: `{claim_id}` (reused; a ledger claim id must stay unique)\n"
        f"- Superseded by: {survivor.agent} ({survivor.role}) — {survivor.comment.url}\n\n"
        "`agent-claim reconcile` neutralized this comment because its claim id was "
        "reused by the surviving claim linked above; the ledger reads that claim as "
        "the sole event for this id."
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
    matching = tuple(
        claim
        for claim in claims
        if isinstance(claim.identity, IssueIdentity) and claim.identity.issue == issue
    )
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
    # `discover_ledger` trusts `LEDGER_LABEL` on the ledger issue itself to find
    # it in one atomic request instead of scanning every open issue (#74); an
    # older ledger, bootstrapped before that label existed, never got it
    # attached, so reconcile is what backfills it going forward.
    client.add_label(LEDGER_ISSUE, LEDGER_LABEL)
    try:
        active_issues = {
            claim.identity.issue
            for claim in _ledger_claims(client)
            if isinstance(claim.identity, IssueIdentity)
        }
    except LedgerSuperseded:
        for issue in client.list_claimed_issues():
            client.remove_label(issue, claim_label())
        raise
    known_issues = active_issues | set(client.list_claimed_issues())
    for issue in sorted(known_issues):
        reconcile_issue_label(client, issue)
    return tuple(sorted(active_issues))


def _duplicate_lifecycles(
    aggregate: ClaimLedgerAggregate, claim_id: str
) -> tuple[tuple[ActiveClaim, tuple[IssueComment, ...]], ...]:
    """Pair each occurrence of a duplicated claim id with every comment that honored
    its termination (a release retry or claimant-then-coordinator pair both land here).

    Derived entirely from `ClaimLedgerAggregate.occurrences`/`terminated_by`, i.e. from
    what `_apply_terminal_event` itself did during the shared walk — never from an
    independent re-parse. An inert or foreign terminal event (one `_apply_terminal_event`
    did not honor, such as a `LedgerSupersede` posted outside its narrow window) never
    shows up here, so it can never be mistaken for a real release.
    """
    occurrences = aggregate.occurrences[claim_id]
    terminating_comments = aggregate.terminated_by.get(claim_id, ())
    return tuple(
        (occurrence, terminating_comments if index == 0 else ())
        for index, occurrence in enumerate(occurrences)
    )


@dataclass(frozen=True)
class DuplicateClaimRepair:
    """One duplicated claim id reconcile neutralized, for the operator-visible report."""

    claim_id: str
    superseded_comment_ids: tuple[int, ...]
    survivor_comment_id: int


def repair_duplicate_claims(client: IssueComments) -> tuple[DuplicateClaimRepair, ...]:
    """Tolerant reconcile pre-pass: neutralize safely-superseded duplicate claim ids.

    A same-claim-id re-claim poisons every strict reader (status/claim/release) with
    `claim id ... was reused`. This runs before those strict reads so `reconcile` can
    heal the ledger instead of erroring. For each duplicated id, the newest occurrence
    is kept as the survivor: for a released-then-reused id it is the only occurrence
    still live; for a same-agent self-re-claim, keeping the newest is deliberate
    because it reflects that agent's latest intent (this is not applied across
    identities: a same-agent duplicate that spans two different issues, two different
    lanes, or an issue and a lane still only keeps the newer identity's workstream,
    silently ending the older one). An older occurrence only auto-neutralizes when it
    is already released, or when it shares the survivor's agent and role. A
    still-active duplicate from a different agent is a real ownership conflict:
    reconcile refuses it loudly instead of picking a winner.
    Every duplicated id on the ledger is validated before any comment is edited, so
    one unsafe conflict never leaves a different, otherwise-safe repair half-applied.
    """
    aggregate = _aggregate_claim_events(client.list_protocol_candidates(LEDGER_ISSUE))
    plans: list[tuple[str, ActiveClaim, tuple[IssueComment, ...]]] = []
    for claim_id in aggregate.duplicate_claim_ids:
        lifecycles = _duplicate_lifecycles(aggregate, claim_id)
        survivor, _ = lifecycles[-1]
        superseded_comments: list[IssueComment] = []
        for occurrence, terminal_comments in lifecycles[:-1]:
            same_claimant = (occurrence.agent, occurrence.role) == (
                survivor.agent,
                survivor.role,
            )
            if not (terminal_comments or same_claimant):
                raise DuplicateClaimConflict(claim_id, occurrence, survivor)
            superseded_comments.append(occurrence.comment)
            superseded_comments.extend(terminal_comments)
        plans.append((claim_id, survivor, tuple(superseded_comments)))

    repairs: list[DuplicateClaimRepair] = []
    for claim_id, survivor, superseded_comments in plans:
        body = _neutralized_claim_body(claim_id, survivor)
        for superseded_comment in superseded_comments:
            client.neutralize_claim_comment(superseded_comment.identifier, body)
        repairs.append(
            DuplicateClaimRepair(
                claim_id=claim_id,
                superseded_comment_ids=tuple(
                    entry.identifier for entry in superseded_comments
                ),
                survivor_comment_id=survivor.comment.identifier,
            )
        )
    return tuple(repairs)


def _reconcile_identity(
    client: IssueComments, identity: ClaimIdentity, *, unclaimed_body: str | None = None
) -> None:
    """Reconcile the issue label/projection this identity owns, if it owns one.

    A lane claim owns no GitHub issue, so it has no label or projection to
    reconcile; reconcile_all_labels/reconcile_issue_label stay issue-only.
    """
    if isinstance(identity, IssueIdentity):
        reconcile_issue_label(client, identity.issue, unclaimed_body=unclaimed_body)


def _resource_holders(
    claims: tuple[ActiveClaim, ...], hold: ResourceHold, *, except_id: str
) -> tuple[ActiveClaim, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != except_id
        and claim.resource is not None
        and claim.resource.name == hold.name
        and claim.resource.value == hold.value
    )


def _assigned_request(request: ClaimRequest) -> ClaimRequest:
    if request.resource is None:
        if request.resource_value is not None:
            raise ClaimError("resource value requires a resource name")
        return request
    name = _outbound_resource_name(request.resource)
    if request.resource_value is None:
        return replace(request, resource=name)
    if (
        isinstance(request.resource_value, bool)
        or not isinstance(request.resource_value, int)
        or request.resource_value < 1
    ):
        raise ClaimError("resource value must be a positive integer")
    return replace(request, resource=name)


def acquire_claim(client: IssueComments, request: ClaimRequest) -> ActiveClaim:
    claimed, _observed = _acquire_claim_with_observed(client, request)
    return claimed


class ClaimPostedReconcileFailed(ClaimError):
    """The requested claim is live on the ledger, but the label/projection
    reconcile that normally follows a winning post failed.

    `acquire_claim` only reaches this step after confirming its own post won
    the ledger, so the claim itself is not in doubt — a caller must report it
    as live (never as a refusal), and separately surface what the reconcile
    failed on.
    """

    def __init__(self, claim: ActiveClaim, observed: tuple[ActiveClaim, ...], error: Exception):
        self.claim = claim
        self.observed = observed
        self.reconcile_error = error
        super().__init__(str(error))


def _acquire_claim_with_observed(
    client: IssueComments, request: ClaimRequest
) -> tuple[ActiveClaim, tuple[ActiveClaim, ...]]:
    """`acquire_claim`, plus the active claims its own post-mutation race check already read.

    The caller's advisory "touches" note (`conflicting_claims`) needs exactly
    that same post-mutation ledger snapshot; returning it here lets the CLI
    reuse it instead of paying for another full ledger-comments fetch right
    after this one (the wait `claim` was reported hanging on, since it landed
    after the mutating post was already visible on the ledger).
    """
    aggregate = _aggregate_claim_events(client.list_protocol_candidates(LEDGER_ISSUE))
    if request.claim_id in aggregate.seen_claim_ids:
        raise ClaimUnavailable(
            f"claim id {request.claim_id!r} is already on this ledger, active or "
            "released; release it, then claim again with a fresh --claim-id"
        )
    _reject_duplicate_claim_ids(aggregate)
    request = _assigned_request(request)
    standing = aggregate.active
    blocked_by = blocking_claims(standing, request)
    if blocked_by:
        owner = blocked_by[0]
        raise ClaimUnavailable(
            f"{_identity_summary(request.identity, request.branch)} is "
            f"claimed by {owner.agent} ({owner.role}) on "
            f"{_identity_summary(owner.identity, owner.branch)} branch {owner.branch}"
        )
    if request.resource is not None and request.resource_value is not None:
        hold = ResourceHold(request.resource, request.resource_value)
        holder = _resource_holders(standing, hold, except_id=request.claim_id)
        if holder:
            owner = holder[0]
            raise ClaimUnavailable(
                f"{hold.name} {hold.value} is held by {owner.agent} ({owner.role}) on "
                f"{_identity_summary(owner.identity, owner.branch)}"
            )

    client.post_comment(LEDGER_ISSUE, claim_comment(request))
    post_aggregate = _aggregate_claim_events(client.list_protocol_candidates(LEDGER_ISSUE))
    if request.claim_id in post_aggregate.duplicate_claim_ids:
        raise ClaimUnavailable(
            f"claim id {request.claim_id!r} claim race detected: another post reused "
            "this id while it was being posted; run agent-claim reconcile, then claim "
            "again with a fresh --claim-id"
        )
    _reject_duplicate_claim_ids(post_aggregate)
    observed = post_aggregate.active
    own = next((claim for claim in observed if claim.claim_id == request.claim_id), None)
    if own is None:
        raise ClaimError(
            f"{_identity_summary(request.identity, request.branch)} did not expose "
            "the posted claim id"
        )
    identity_competitors = blocking_claims(observed, own)
    if identity_competitors:
        winner = min(
            (own, *identity_competitors),
            key=lambda claim: (claim.comment.created_at, claim.comment.identifier),
        )
        if winner.claim_id != request.claim_id:
            client.post_comment(
                LEDGER_ISSUE,
                release_comment(own, request.agent, request.role, "claim race lost"),
            )
            _reconcile_identity(client, request.identity)
            _reconcile_identity(client, winner.identity)
            raise ClaimUnavailable(
                f"{_identity_summary(request.identity, request.branch)} claim race lost to "
                f"{winner.agent} ({winner.role}) on "
                f"{_identity_summary(winner.identity, winner.branch)} branch {winner.branch}"
            )
    if request.resource is not None and request.resource_value is not None:
        expected = ResourceHold(request.resource, request.resource_value)
        if own.resource != expected:
            holder = next(
                (claim for claim in observed if claim.resource == expected),
                None,
            )
            client.post_comment(
                LEDGER_ISSUE,
                release_comment(own, request.agent, request.role, "claim race lost"),
            )
            _reconcile_identity(client, request.identity)
            if holder is not None:
                _reconcile_identity(client, holder.identity)
                raise ClaimUnavailable(
                    f"{expected.name} {expected.value} is held by "
                    f"{holder.agent} ({holder.role}) on "
                    f"{_identity_summary(holder.identity, holder.branch)}"
                )
            raise ClaimUnavailable(
                f"{expected.name} {expected.value} is held by another claim"
            )
    elif request.resource is not None:
        hold = own.resource
        if hold is None or hold.name != request.resource:
            raise ClaimError(
                f"{_identity_summary(request.identity, request.branch)} requested "
                f"{request.resource} but derivation produced no hold"
            )

    try:
        _reconcile_identity(client, request.identity)
    except ClaimError as error:
        # The claim comment above already won the ledger (the earlier race
        # checks all passed), so a failure reconciling the issue's label or
        # projection must never surface as if the claim itself had failed.
        raise ClaimPostedReconcileFailed(own, observed, error) from error
    return own, observed


def _combined_scope(
    current: tuple[str, ...], add: tuple[str, ...], drop: tuple[str, ...]
) -> tuple[str, ...]:
    current_set = set(current)
    missing = next((path for path in drop if path not in current_set), None)
    if missing is not None:
        raise ClaimUnavailable(
            f"cannot drop {missing!r}; it is not in this claim's scope"
        )
    drop_set = set(drop)
    kept = tuple(path for path in current if path not in drop_set)
    added = tuple(path for path in add if path not in kept)
    if not kept and not added:
        raise ClaimUnavailable("rescope must leave a non-empty scope")
    combined = kept + added
    if combined == current:
        raise ClaimUnavailable("rescope does not change the claim scope")
    return _valid_scope(list(combined))


def _observe_rescoped_claim(
    client: IssueComments,
    identity: ClaimIdentity,
    selected: ActiveClaim,
    expected_scope: tuple[str, ...],
    *,
    expose: str,
    observe: str,
) -> tuple[tuple[ActiveClaim, ...], ActiveClaim]:
    aggregate = _aggregate_claim_events(client.list_protocol_candidates(LEDGER_ISSUE))
    _reject_duplicate_claim_ids(aggregate)
    observed = aggregate.active
    own = next((claim for claim in observed if claim.claim_id == selected.claim_id), None)
    if own is None:
        raise ClaimError(
            f"{_identity_summary(identity, selected.branch)} did not expose {expose}"
        )
    if own.scope != expected_scope:
        raise ClaimError(
            f"{_identity_summary(identity, selected.branch)} did not observe {observe}"
        )
    return observed, own


def _select_rescope_claim(
    claims: tuple[ActiveClaim, ...],
    identity: ClaimIdentity,
    agent: str,
    claim_id: str | None,
    *,
    branch: str | None,
) -> ActiveClaim:
    standing = _claims_for_identity(claims, identity, branch)
    if not standing:
        raise ClaimUnavailable(
            f"{_identity_summary(identity, branch or '')} has no active build claim"
        )
    if claim_id is None:
        if not branch:
            raise ClaimUnavailable(
                "rescope without --claim-id requires a non-empty current branch; "
                "pass --claim-id"
            )
        matches = tuple(
            claim
            for claim in standing
            if claim.agent == agent and claim.branch == branch
        )
        if len(matches) != 1:
            raise ClaimUnavailable(
                f"{_identity_summary(identity, branch)} has no unique claim for this "
                f"session on branch {branch!r}; pass --claim-id"
            )
        selected = matches[0]
    else:
        selected = next(
            (claim for claim in standing if claim.claim_id == claim_id),
            None,
        )
        if selected is None:
            raise ClaimUnavailable(
                f"{_identity_summary(identity, branch or '')} has no active claim "
                f"{claim_id!r}"
            )
    if agent != selected.agent:
        raise ClaimUnavailable("only the original claimant may rescope")
    if branch and selected.branch != branch:
        raise ClaimUnavailable(
            f"claim branch {selected.branch!r} does not match checkout branch {branch!r}"
        )
    return selected


def rescope_claim(
    client: IssueComments,
    identity: ClaimIdentity,
    agent: str,
    add: tuple[str, ...],
    drop: tuple[str, ...],
    claim_id: str | None,
    *,
    branch: str | None = None,
    allow_directory_reason: str | None = None,
) -> ActiveClaim:
    if not add and not drop:
        raise ClaimUnavailable("rescope requires --add or --drop")
    add_scope = _valid_scope(list(add)) if add else ()
    drop_scope = _valid_scope(list(drop)) if drop else ()
    observed = _ledger_claims(client)
    selected = _select_rescope_claim(
        observed, identity, agent, claim_id, branch=branch
    )
    new_scope = _combined_scope(selected.scope, add_scope, drop_scope)
    client.post_comment(
        LEDGER_ISSUE,
        rescope_comment(
            selected,
            new_scope,
            agent,
            selected.role,
            allow_directory_reason=allow_directory_reason,
        ),
    )
    _, own = _observe_rescoped_claim(
        client,
        identity,
        selected,
        new_scope,
        expose="the rescoped claim id",
        observe="the posted rescope",
    )

    _reconcile_identity(client, identity)
    return own


def _require_coordinator_override(role: str | None, reason: str | None) -> None:
    if role != "coordinator":
        raise ClaimUnavailable("a coordinator override requires --role coordinator")
    if reason is None:
        raise ClaimUnavailable("a coordinator override requires --reason")


def _claims_for_identity(
    claims: tuple[ActiveClaim, ...], identity: ClaimIdentity, branch: str | None
) -> tuple[ActiveClaim, ...]:
    """Restrict standing claims to the one issue or lane `identity` names.

    An issue identity already carries its number, so no branch is needed. A lane
    identity carries none (Entschieden #2), so the caller's checkout branch is the
    only way to tell which lane is meant — required even when `claim_id` is given
    explicitly, mirroring how issue release still scopes by issue number first.
    """
    if isinstance(identity, IssueIdentity):
        return tuple(
            claim
            for claim in claims
            if isinstance(claim.identity, IssueIdentity)
            and claim.identity.issue == identity.issue
        )
    if not branch:
        raise ClaimUnavailable(
            "lane release requires a non-empty current branch; check out the "
            "docs/ or fix/ lane branch, or pass an issue number"
        )
    return tuple(
        claim
        for claim in claims
        if isinstance(claim.identity, LaneIdentity) and claim.branch == branch
    )


def release_claim(
    client: IssueComments,
    identity: ClaimIdentity,
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
    standing = _claims_for_identity(_ledger_claims(client), identity, branch)
    if not standing:
        raise ClaimUnavailable(
            f"{_identity_summary(identity, branch or '')} has no active build claim"
        )
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
                f"{_identity_summary(identity, branch)} has no unique claim for this "
                f"session on branch {branch!r}; pass --claim-id"
            )
        selected = matches[0]
    else:
        selected = next(
            (claim for claim in standing if claim.claim_id == claim_id),
            None,
        )
        if selected is None:
            raise ClaimUnavailable(
                f"{_identity_summary(identity, branch or '')} has no active claim "
                f"{claim_id!r}"
            )
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
    _reconcile_identity(
        client,
        identity,
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
        or not isinstance(selected.identity, IssueIdentity)
        or selected.identity.issue != LEDGER_ISSUE
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
