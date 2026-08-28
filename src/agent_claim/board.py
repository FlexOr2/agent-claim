"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from . import protocol

DEFAULT_PRIORITY_LABELS = ("security", "data", "ci", "product", "ux", "cleanup")
CONFIG_PATH = Path(".agent-claim/board.toml")
CONTRACT_HEADING_PATTERN = re.compile(
    r"(?m)^#{1,6}[ \t]+(?P<name>Now|Next|Blocked by|Done when)[ \t]*$"
)
CONTRACT_FIELD_PATTERN = re.compile(
    r"(?m)^(?:\*\*(?P<bold_name>Now|Next|Blocked by|Done when):\*\*|"
    r"(?P<plain_name>Now|Next|Blocked by|Done when):)[ \t]*(?P<value>[^\r\n]*)$"
)
MARKDOWN_HEADING_PATTERN = re.compile(r"(?m)^#{1,6} .*$")
EXPECTATION_HEADING_PATTERN = re.compile(
    r"(?im)^#{1,6}[ \t]+(?:Erwartung|Erwartungen|Erwartungsliste)[ \t]*$"
)
PROPOSED_EXPECTATION_PATTERN = re.compile(
    r"\*\(Default:[ \t]*(?:yes|no|later)\)\*", re.IGNORECASE
)
RULED_EXPECTATION_PATTERN = re.compile(
    r"\*\(geregelt:[ \t]*(?:ja|NEIN(?:[^\r\n]*))\)\*", re.IGNORECASE
)
REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])#([1-9][0-9]*)")
CLOSING_REFERENCE_PATTERN = re.compile(
    r"(?im)\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?|"
    r"land(?:s|ed)?|implement(?:s|ed)?)\s*:?\s*#([1-9][0-9]*)"
)
NOTHING_BLOCKER_VALUES = frozenset({"nichts", "none", "keine", "-"})
CLAIM_OLD_AFTER = timedelta(hours=1)
CUT_HEADING_PATTERN = re.compile(r"(?m)^##[ \t]+Schnitt")
CUT_SECTION_HEADING_PATTERN = re.compile(r"(?m)^##[ \t]+")
SLICE_LINE_PATTERN = re.compile(r"(?m)^\*\*Scheibe [1-9][0-9]*: .+\*\*\s*$")


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]
    body: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_ref_name: str
    merged_at: str | None = None


@dataclass(frozen=True)
class BoardConfig:
    priority_labels: tuple[str, ...] = DEFAULT_PRIORITY_LABELS


@dataclass(frozen=True)
class Contract:
    now: str | None
    next: str | None
    blocked_by: str | None
    done_when: str | None

    @property
    def complete(self) -> bool:
        return (
            self.now is not None
            and self.next is not None
            and self.blocked_by is not None
            and self.done_when is not None
        )


class Stage(StrEnum):
    TEXT_ONLY = "text-only"
    CODE_LANDED = "code-landed"
    IN_FLIGHT = "in-flight"


class ExpectationState(StrEnum):
    NONE = "-"
    PROPOSED = "proposed"
    RULED = "ruled"


@dataclass(frozen=True)
class BoardItem:
    number: int
    title: str
    labels: tuple[str, ...]
    priority_category: int
    priority_bucket: str
    contract: Contract
    contract_complete: bool
    expectation_state: ExpectationState
    open_blockers: tuple[int, ...]
    stage: Stage
    age_days: int
    idle_days: int
    active_claim: str | None
    claim_age: str | None
    claim_old: bool
    unblocks_count: int
    single_concrete_next: bool
    score: int
    actionable: bool
    actionable_reason: str | None


@dataclass(frozen=True)
class Board:
    items: tuple[BoardItem, ...]
    ready_now: tuple[BoardItem, ...]
    stale: tuple[BoardItem, ...]


def load_config(path: Path = CONFIG_PATH) -> BoardConfig:
    if not path.exists():
        return BoardConfig()
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise protocol.ClaimError(f"cannot read board configuration {path}: {error}") from error
    labels = raw.get("priority_labels")
    if labels is None:
        return BoardConfig()
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(label, str) and label.strip() == label and label for label in labels)
        or len(set(labels)) != len(labels)
    ):
        raise protocol.ClaimError(
            "board configuration priority_labels must be a non-empty list of unique labels"
        )
    return BoardConfig(tuple(labels))


def parse_contract(body: str) -> Contract:
    sections: dict[str, str] = {}
    matches = sorted(
        (
            *CONTRACT_HEADING_PATTERN.finditer(body),
            *CONTRACT_FIELD_PATTERN.finditer(body),
        ),
        key=re.Match.start,
    )
    for match in matches:
        if match.re is CONTRACT_HEADING_PATTERN:
            name = match.group("name")
            next_heading = MARKDOWN_HEADING_PATTERN.search(body, match.end())
            end = next_heading.start() if next_heading is not None else len(body)
            value = body[match.end() : end].strip()
        else:
            name = match.group("bold_name") or match.group("plain_name")
            value = match.group("value").strip()
        sections[name] = value
    return Contract(
        now=sections.get("Now") or None,
        next=sections.get("Next") or None,
        blocked_by=sections.get("Blocked by"),
        done_when=sections.get("Done when") or None,
    )


def expectation_state(body: str) -> ExpectationState:
    expectation_heading = EXPECTATION_HEADING_PATTERN.search(body)
    if expectation_heading is None:
        return ExpectationState.NONE
    next_heading = MARKDOWN_HEADING_PATTERN.search(body, expectation_heading.end())
    expectation_block = body[
        expectation_heading.end() : next_heading.start() if next_heading is not None else len(body)
    ]
    expectation_lines = tuple(
        line.strip() for line in expectation_block.splitlines() if line.strip()
    )
    if (
        not expectation_lines
        or any(PROPOSED_EXPECTATION_PATTERN.search(line) for line in expectation_lines)
        or not all(RULED_EXPECTATION_PATTERN.search(line) for line in expectation_lines)
    ):
        return ExpectationState.PROPOSED
    return ExpectationState.RULED


def _references(text: str | None) -> frozenset[int]:
    if text is None:
        return frozenset()
    return frozenset(int(number) for number in REFERENCE_PATTERN.findall(text))


def _nothing_blocker_line(line: str) -> bool:
    cleaned = line.strip().rstrip(".-").casefold()
    return not cleaned or cleaned in NOTHING_BLOCKER_VALUES


def _blocker_references(text: str | None) -> frozenset[int]:
    if text is None:
        return frozenset()
    lines = tuple(line.strip() for line in text.splitlines() if line.strip())
    if not lines or all(_nothing_blocker_line(line) for line in lines):
        return frozenset()
    return _references(text)


def has_cut(body: str) -> bool:
    heading = CUT_HEADING_PATTERN.search(body)
    if heading is None:
        return False
    next_heading = CUT_SECTION_HEADING_PATTERN.search(body, heading.end())
    end = next_heading.start() if next_heading is not None else len(body)
    return SLICE_LINE_PATTERN.search(body[heading.end() : end]) is not None


def claim_age(created_at: str, now: datetime) -> timedelta:
    return now.astimezone(timezone.utc) - _timestamp(created_at)


def _floored_claim_minutes(age: timedelta) -> int:
    return max(0, int(age.total_seconds())) // 60


def format_claim_age(age: timedelta) -> str:
    hours, minutes = divmod(_floored_claim_minutes(age), 60)
    return f"{hours}h {minutes}m"


def claim_is_old(age: timedelta) -> bool:
    return _floored_claim_minutes(age) > _floored_claim_minutes(CLAIM_OLD_AFTER)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp") from error
    if parsed.tzinfo is None:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp")
    return parsed.astimezone(timezone.utc)


def _single_concrete_next(value: str | None) -> bool:
    if value is None:
        return False
    lines = tuple(line.strip(" -\t") for line in value.splitlines() if line.strip())
    return len(lines) == 1 and lines[0].casefold() not in {"tbd", "todo", "unknown"}


def _claim_by_issue(claims: tuple[protocol.ActiveClaim, ...]) -> dict[int, protocol.ActiveClaim]:
    issue_claims = (
        claim for claim in claims if isinstance(claim.identity, protocol.IssueIdentity)
    )
    return {claim.identity.issue: claim for claim in issue_claims}


def _priority_index(labels: tuple[str, ...], config: BoardConfig) -> int | None:
    priorities = {label.casefold(): index for index, label in enumerate(config.priority_labels)}
    matches = (priorities[label.casefold()] for label in labels if label.casefold() in priorities)
    return min(matches, default=None)


def _priority_bucket(
    labels: tuple[str, ...], config: BoardConfig, unblocks_count: int
) -> tuple[int, str]:
    index = _priority_index(labels, config)
    blocker_category = min(3, len(config.priority_labels))
    if index is not None and index < blocker_category:
        return index, config.priority_labels[index]
    if unblocks_count:
        return blocker_category, "blocker"
    if index is not None:
        return index + 1, config.priority_labels[index]
    return len(config.priority_labels) + 1, "unlabelled"


def _associated_issues(pull_requests: tuple[PullRequest, ...]) -> frozenset[int]:
    return frozenset(
        reference
        for pull_request in pull_requests
        for reference in (
            int(number)
            for number in CLOSING_REFERENCE_PATTERN.findall(
                f"{pull_request.title}\n{pull_request.body}"
            )
        )
    )


def build_board(
    issues: tuple[Issue, ...],
    open_pull_requests: tuple[PullRequest, ...],
    recent_merged_pull_requests: tuple[PullRequest, ...],
    claims: tuple[protocol.ActiveClaim, ...],
    config: BoardConfig,
    *,
    now: datetime | None = None,
) -> Board:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issue_numbers = frozenset(issue.number for issue in issues)
    open_references = issue_numbers | frozenset(pr.number for pr in open_pull_requests)
    contracts = {issue.number: parse_contract(issue.body) for issue in issues}
    blockers = {
        issue.number: tuple(
            sorted(_blocker_references(contracts[issue.number].blocked_by) & open_references)
        )
        for issue in issues
    }
    unblocks = {
        issue.number: sum(issue.number in other_blockers for other_blockers in blockers.values())
        for issue in issues
    }
    claims_by_issue = _claim_by_issue(claims)
    in_flight_references = _associated_issues(open_pull_requests)
    landed_references = _associated_issues(recent_merged_pull_requests)
    open_branches = frozenset(pr.head_ref_name for pr in open_pull_requests)

    items: list[BoardItem] = []
    for issue in issues:
        contract = contracts[issue.number]
        expectations = expectation_state(issue.body)
        claim = claims_by_issue.get(issue.number)
        in_flight = issue.number in in_flight_references or (
            claim is not None and claim.branch in open_branches
        )
        if in_flight:
            stage = Stage.IN_FLIGHT
        elif issue.number in landed_references:
            stage = Stage.CODE_LANDED
        else:
            stage = Stage.TEXT_ONLY
        age_days = max(0, (observed_at - _timestamp(issue.created_at)).days)
        idle_days = max(0, (observed_at - _timestamp(issue.updated_at)).days)
        single_next = _single_concrete_next(contract.next)
        priority_category, priority_bucket = _priority_bucket(
            issue.labels, config, unblocks[issue.number]
        )
        score = 0
        score += 20 * unblocks[issue.number]
        score += {Stage.IN_FLIGHT: 30, Stage.CODE_LANDED: 20, Stage.TEXT_ONLY: -20}[stage]
        score += 10 if single_next else 0
        if claim is None:
            active_claim = None
            claim_age_text = None
            claim_old = False
        else:
            active_claim = f"{claim.agent} ({claim.role})"
            age = claim_age(claim.comment.created_at, observed_at)
            claim_age_text = format_claim_age(age)
            claim_old = claim_is_old(age)
        actionable_reason = _actionable_reason(
            active_claim=active_claim,
            open_blockers=blockers[issue.number],
            contract_complete=contract.complete,
            expectation_state=expectations,
        )
        items.append(
            BoardItem(
                number=issue.number,
                title=issue.title,
                labels=issue.labels,
                priority_category=priority_category,
                priority_bucket=priority_bucket,
                contract=contract,
                contract_complete=contract.complete,
                expectation_state=expectations,
                open_blockers=blockers[issue.number],
                stage=stage,
                age_days=age_days,
                idle_days=idle_days,
                active_claim=active_claim,
                claim_age=claim_age_text,
                claim_old=claim_old,
                unblocks_count=unblocks[issue.number],
                single_concrete_next=single_next,
                score=score,
                actionable=actionable_reason is None,
                actionable_reason=actionable_reason,
            )
        )
    ordered = tuple(
        sorted(items, key=lambda item: (item.priority_category, -item.score, item.number))
    )
    return Board(
        items=ordered,
        ready_now=tuple(
            item
            for item in ordered
            if item.actionable
        ),
        stale=tuple(
            item
            for item in ordered
            if item.idle_days > 7 and item.stage is Stage.TEXT_ONLY
        ),
    )


def highest_scored_actionable(board: Board) -> BoardItem | None:
    return max(board.ready_now, key=lambda item: item.score, default=None)


def board_json(board: Board) -> str:
    return json.dumps(asdict(board), default=lambda value: value.value)


def render(board: Board) -> str:
    rows = [
        (
            "SCORE",
            "ISSUE",
            "PRIORITY",
            "STAGE",
            "CONTRACT",
            "EXPECT",
            "NEXT",
            "AGE",
            "IDLE",
            "CLAIM",
            "ACTIONABLE",
            "BLOCKERS",
            "UNBLOCKS",
            "TITLE",
        ),
        *(
            (
                str(item.score),
                f"#{item.number}",
                item.priority_bucket,
                item.stage.value,
                _contract_summary(item.contract),
                item.expectation_state.value,
                _brief(item.contract.next),
                str(item.age_days),
                str(item.idle_days),
                _claim_cell(item),
                "yes" if item.actionable else f"no: {item.actionable_reason}",
                ",".join(f"#{number}" for number in item.open_blockers) or "-",
                str(item.unblocks_count),
                item.title,
            )
            for item in board.items
        ),
    ]
    widths = tuple(max(len(row[index]) for row in rows) for index in range(len(rows[0])))
    table = "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    )
    ready = ", ".join(f"#{item.number}" for item in board.ready_now) or "none"
    stale = ", ".join(f"#{item.number}" for item in board.stale) or "none"
    return f"{table}\n\nREADY NOW\n{ready}\n\nSTALE\n{stale}"


def _contract_summary(contract: Contract) -> str:
    present = (
        name
        for name, value in (
            ("Now", contract.now),
            ("Next", contract.next),
            ("Blocked by", contract.blocked_by),
            ("Done when", contract.done_when),
        )
        if value is not None
    )
    return ", ".join(present) or "-"


def _actionable_reason(
    *,
    active_claim: str | None,
    open_blockers: tuple[int, ...],
    contract_complete: bool,
    expectation_state: ExpectationState,
) -> str | None:
    if expectation_state is ExpectationState.PROPOSED:
        return "Erwartungen ungeregelt"
    if active_claim is not None:
        return "claimed"
    if open_blockers:
        return "blocked by " + ", ".join(f"#{number}" for number in open_blockers)
    if not contract_complete:
        return "body incomplete"
    return None


def _claim_cell(item: BoardItem) -> str:
    if item.active_claim is None:
        return "-"
    suffix = " old" if item.claim_old else ""
    return f"{item.active_claim} {item.claim_age}{suffix}"


def _brief(value: str | None, *, maximum: int = 48) -> str:
    if value is None:
        return "-"
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= maximum else one_line[: maximum - 1] + "…"
