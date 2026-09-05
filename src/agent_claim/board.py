"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from . import protocol

DEFAULT_PRIORITY_LABELS = ("security", "data", "ci", "product", "ux", "cleanup")
CONFIG_PATH = Path(".agent-claim/board.toml")
IDEA_REFINEMENT_STEP = "Problem neu prüfen und Item verfeinern"
BLOCKED_BY = "Blocked by"
CONTRACT_HEADING_PATTERN = re.compile(
    rf"(?m)^#{{1,6}}[ \t]+(?P<name>Now|Next|{BLOCKED_BY}|Done when)[ \t]*$"
)
CONTRACT_FIELD_PATTERN = re.compile(
    rf"(?m)^(?:\*\*(?P<bold_name>Now|Next|{BLOCKED_BY}|Done when):\*\*|"
    rf"(?P<plain_name>Now|Next|{BLOCKED_BY}|Done when):)[ \t]*(?P<value>[^\r\n]*)$"
)
BLOCKER_LIST_PATTERN = re.compile(r"#([1-9]\d*)(?:[ \t]*,[ \t]*#([1-9]\d*))*", re.ASCII)
NO_BLOCKERS = "nichts"
MARKDOWN_HEADING_PATTERN = re.compile(r"(?m)^#{1,6} .*$")
EXPECTATION_HEADING_PATTERN = re.compile(
    r"(?im)^#{1,6}[ \t]+(?:Erwartung|Erwartungen|Erwartungsliste)\b[^\n]*$"
)
DOTTED_DATE_PATTERN = re.compile(r"\b([0-3]?\d)\.([01]?\d)\.(20\d{2})\b")
OPERATOR_RULING_DATE_PATTERN = re.compile(
    r"GEREGELT:[ \t]*Operator[ \t]*([0-3]?\d)\.([01]?\d)\.(20\d{2})",
    re.IGNORECASE,
)
RULING_OLD_AFTER_LANDINGS = 10
STALE_IDLE_DAYS = 7
FROZEN_LINE_PATTERN = re.compile(
    r"(?m)^(?:[ \t]{0,3}>)*[ \t]{0,3}(?:\*\*Eingefroren bis:\*\*|Eingefroren bis:)"
    r"[ \t]*(?P<value>[^\r\n]*)$"
)
FROZEN_TRIGGER_PATTERN = re.compile(
    r"(?P<trigger>\S.*?)[ \t]*\(Operator,[ \t]*"
    r"(?P<day>[0-3]?\d)\.(?P<month>[01]?\d)\.(?P<year>20\d{2})\)"
)
# CommonMark fence delimiters: at most 3 leading spaces, then a run of 3+
# backticks or 3+ tildes. An OPENING delimiter may carry an info string after
# the run (` ```python `); a CLOSING delimiter may not — only trailing
# spaces/tabs are allowed after the run (` ``` `, never ` ```python `), so
# `Closing`'s stricter pattern requires nothing but whitespace to follow.
# A 4-space-indented code block (CommonMark's other fencing form) is not
# modeled here; see `_live_text` for why that gap is safe.
FENCE_OPENING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})")
FENCE_CLOSING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})[ \t]*$")
PROPOSED_EXPECTATION_PATTERN = re.compile(r"\*\(Default:[ \t]*(?:yes|no|later)\)\*", re.IGNORECASE)
# `ja` and `NEIN` both may carry trailing justification text before the
# closing `)*` (`*(geregelt: ja — Owner ist #567)*`, `*(geregelt: NEIN, it
# stays)*`) — real operator rulings cite an owner or a reservation on a
# "yes" as often as on a "no", so the two keywords take the same shape. The
# character right after the keyword must be the closing `)`, whitespace, an
# em dash `—`, or one of `, ; :` — every real separator seen in #79 and in
# #62's own tests (`ja — Owner`, `ja mit Schärfung,`, `ja, aber`, `NEIN, it
# stays`). A hyphen or any other letter-joining character is excluded on
# purpose: `ja-nein` is a contradiction in the ruling text, not a "yes".
RULED_EXPECTATION_PATTERN = re.compile(
    r"\*\(geregelt:[ \t]*(?:ja|NEIN)(?:[ \t,;:\u2014][^\r\n]*)?\)\*", re.IGNORECASE
)
# Both RULED_EXPECTATION_PATTERN and PROPOSED_EXPECTATION_PATTERN mark a
# CommonMark list item (`- ...` or `1. ...`): every expectation line in this
# contract is written as one. A line with that shape is a candidate
# expectation, whether or not it happens to carry either marker yet.
EXPECTATION_LINE_SHAPE_PATTERN = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")
REFERENCE_PATTERN = re.compile(r"(?<!\w)#([1-9]\d*)", re.ASCII)
# One issue named the way GitHub names it across repositories: `OWNER/REPO#n`,
# or `#n` for the repository the text itself lives in. Every typed line below
# embeds this one grammar, so a shorthand and its qualified spelling always
# parse to the same reference.
QUALIFIED_REFERENCE = (
    rf"(?:(?P<repository>{protocol.REPOSITORY_PATTERN.pattern}))?#(?P<number>[1-9]\d*)"
)
# GitHub links a keyword to a reference only on one line, separated by
# horizontal space and ending at the reference: `Closes#7`, a keyword whose
# reference sits on the next line, and `#7suffix` all leave the issue open, so
# reading them as a closure would report a landing GitHub never performs.
KEYWORD_SEPARATOR = r"[ \t]*:?[ \t]+"
REFERENCE_BOUNDARY = r"(?![A-Za-z0-9_])"
# The keywords GitHub itself closes an issue on when a pull request merges.
# Nothing else retires an item, so this is what a landing's typed closing
# reference is checked against.
CLOSING_KEYWORDS = r"close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?"
CLOSING_REFERENCE_PATTERN = re.compile(
    rf"(?im)\b(?:{CLOSING_KEYWORDS}){KEYWORD_SEPARATOR}"
    rf"{QUALIFIED_REFERENCE}{REFERENCE_BOUNDARY}",
    re.ASCII,
)
# The board's stage heuristic also believes a pull request that says it landed
# or implemented an issue. GitHub closes on neither word, so this wider set
# answers "which issue did this pull request work on", never "which issue does
# it retire".
LANDING_CLAIM_PATTERN = re.compile(
    rf"(?im)\b(?:{CLOSING_KEYWORDS}|land(?:s|ed)?|implement(?:s|ed)?)"
    rf"{KEYWORD_SEPARATOR}{QUALIFIED_REFERENCE}{REFERENCE_BOUNDARY}",
    re.ASCII,
)
WORK_ITEM_KIND = "work-item"
CLASSIFICATION_LINE_PATTERN = re.compile(r"(?im)^(?P<kind>Work-Item|No-Item):(?P<value>[^\r\n]*)$")
WORK_ITEM_VALUE_PATTERN = re.compile(QUALIFIED_REFERENCE, re.ASCII)
RECOVERY_STEP = "close or re-project"
# A slice's pull request must never close its still-open epic — that would
# retire the epic before its remaining slices exist. This repository's
# established substitute is a whole line opening with one of these markers
# (observed verbatim in atelier-2 PRs #848 "Part of #79.", #960 "Refs #956
# and #80", #965/#967 "Refs #<n> ..."). Anchoring to the start of the line
# is what keeps a casual mid-paragraph mention — "as noted in #79's plan" —
# from ever counting; only a dedicated reference line does. This is still a
# syntactic marker, not a validated relation: GitHub has no structured field
# for a non-closing PR-to-issue link, and this repository's own children use
# it inconsistently (see `_touched_without_closing`'s docstring for the
# named residual and the corroboration this module still requires).
TOUCHES_WITHOUT_CLOSING_LINE_PATTERN = re.compile(
    r"(?im)^(?:Refs?|References?|Part of|Teil von)\b[:\s].*$"
)
CLAIM_OLD_AFTER = timedelta(hours=1)
# The slice table's header cells, in order, compared case- and
# whitespace-insensitively (`_table_row_cells` already strips each cell).
SLICE_TABLE_HEADER_CELLS = ("#", "scheibe", "item", "hängt ab von")
_SLICE_TABLE_SEPARATOR_CELL_PATTERN = re.compile(r"^:?-+:?$")
_SLICE_TABLE_INDEX_PATTERN = re.compile(r"^[1-9]\d*$", re.ASCII)
_SLICE_TABLE_ITEM_LINK_PATTERN = re.compile(r"^#([1-9]\d*)$", re.ASCII)
UNDISPATCHED_SLICE_CELL = "—"
# The three slice-title forms seen in atelier-2 (`#79`): a parenthetical
# after the real title (`(#962 Scheibe 4)`, `(#962 slice 4)`) or a leading
# German phrase (`Scheibe 4 von #962`).
_SLICE_TITLE_PARENTHETICAL_PATTERN = re.compile(
    r"\(#(?P<parent>[1-9]\d*)[ \t]+(?:Scheibe|slice)[ \t]+(?P<slice>[1-9]\d*)\)",
    re.IGNORECASE | re.ASCII,
)
_SLICE_TITLE_VON_PATTERN = re.compile(
    r"Scheibe[ \t]+(?P<slice>[1-9]\d*)[ \t]+von[ \t]+#(?P<parent>[1-9]\d*)",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]
    body: str
    created_at: str
    updated_at: str


class BlockerState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MISSING = "missing"


@dataclass(frozen=True)
class BlockerReference:
    number: int
    state: BlockerState
    is_pull_request: bool
    closed_at: datetime | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_ref_name: str
    merged_at: str | None = None


@dataclass(frozen=True)
class PullRequestDetail:
    """One pull request read for its own sake, rather than for the board's stages."""

    number: int
    body: str
    base_ref_name: str
    head_ref_name: str
    head_repository: str
    author: str
    merged: bool


@dataclass(frozen=True)
class IssueReference:
    """One issue, always qualified: a same-repository `#n` is resolved at parse time."""

    repository: str
    number: int

    def __str__(self) -> str:
        return f"{self.repository}#{self.number}"


@dataclass(frozen=True)
class ParentIssue:
    """The issue GitHub records as an item's parent through its sub-issue relation."""

    reference: IssueReference
    body: str


class NoItemKind(StrEnum):
    DOCS = "docs"
    FIX = "fix"


@dataclass(frozen=True)
class WorkItemClassification:
    item: IssueReference

    def __str__(self) -> str:
        return f"Work-Item: {self.item}"


@dataclass(frozen=True)
class NoItemClassification:
    kind: NoItemKind

    def __str__(self) -> str:
        return f"No-Item: {self.kind.value}"


Classification = WorkItemClassification | NoItemClassification


@dataclass(frozen=True)
class ClassificationDefect:
    """Why a pull request's classification is not one this repository accepts."""

    message: str


@dataclass(frozen=True)
class SliceTableRow:
    """One row of a body's slice table (`#79`'s grammar).

    `item_issue` is the parsed `#n` when `item_cell` is a well-formed link;
    `None` covers both the undispatched marker (`item_cell ==
    UNDISPATCHED_SLICE_CELL`) and a malformed cell — `item_cell` itself is
    the one source of truth for telling those two apart, so this row never
    needs a separate status field to go stale against it.
    """

    index: int
    name: str
    item_cell: str
    item_issue: int | None


@dataclass(frozen=True)
class MalformedSliceTable:
    """A header line that looks like an attempted slice table but isn't one.

    "Looks like" is deliberately loose (starts with `#`, names `Scheibe`
    somewhere on the line) — the whole point is to catch a header that
    almost, but not quite, matches `SLICE_TABLE_HEADER_CELLS`, rather than
    silently treating it as ordinary prose and skipping the checks it was
    meant to carry.
    """

    line: str


@dataclass(frozen=True)
class MalformedSliceRow:
    """A pipe-shaped line inside a recognized slice table that isn't a
    well-formed row: the wrong column count, or a non-integer `#` cell."""

    line: str


SliceTableEntry = SliceTableRow | MalformedSliceTable | MalformedSliceRow


@dataclass(frozen=True)
class BoardConfig:
    priority_labels: tuple[str, ...] = DEFAULT_PRIORITY_LABELS
    idea_label: str | None = None


@dataclass(frozen=True)
class ContractDefect:
    field: str
    message: str


@dataclass(frozen=True)
class Contract:
    now: str | None
    next: str | None
    blocked_by: str | None
    done_when: str | None
    defects: tuple[ContractDefect, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            self.now is not None
            and self.next is not None
            and self.blocked_by is not None
            and self.done_when is not None
        )

    @property
    def projectionless(self) -> bool:
        return not any((self.now, self.next, self.blocked_by, self.done_when))

    @property
    def blocker_issues(self) -> frozenset[int]:
        return _blocker_references(self.blocked_by)


class Stage(StrEnum):
    TEXT_ONLY = "text-only"
    CODE_LANDED = "code-landed"
    IN_FLIGHT = "in-flight"


class ExpectationState(StrEnum):
    NONE = "-"
    PROPOSED = "proposed"
    RULED = "ruled"


@dataclass(frozen=True)
class ExpectationProgress:
    open: int
    total: int


@dataclass(frozen=True)
class BoardItem:
    number: int
    title: str
    labels: tuple[str, ...]
    priority_category: int
    priority_bucket: str
    contract: Contract
    next_step: str | None
    contract_complete: bool
    expectation_state: ExpectationState
    expectation_progress: ExpectationProgress
    ruling_landings: int | None
    ruling_old: bool | None
    frozen_trigger: str | None
    open_blockers: tuple[int, ...]
    freed_on: datetime | None
    freed_days: int | None
    stage: Stage
    age_days: int
    idle_days: int
    active_claim: str | None
    claim_age: str | None
    claim_old: bool
    unblocks_count: int
    score: int
    actionable: bool
    actionable_reason: str | None


@dataclass(frozen=True)
class Board:
    """`items`, and therefore `ready_now`, are ordered `(priority_category, -score, number)`.

    `ready_now`, `stale`, and `recovery` are filters over `items`; filtering
    never reorders, so `ready_now[0]` is always `items`' first actionable row
    — the same row a human reading `board` sees first. `next` relies on this.

    `recovery` holds the items a merged pull request declared as its work
    item while they stayed open: the landing happened, the bookkeeping did
    not.
    """

    items: tuple[BoardItem, ...]
    ready_now: tuple[BoardItem, ...]
    stale: tuple[BoardItem, ...]
    recovery: tuple[BoardItem, ...]
    blocker_references: tuple[BlockerReference, ...]


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
        priority_labels = DEFAULT_PRIORITY_LABELS
    else:
        if (
            not isinstance(labels, list)
            or not labels
            or not all(
                isinstance(label, str) and label.strip() == label and label for label in labels
            )
            or len(set(labels)) != len(labels)
        ):
            raise protocol.ClaimError(
                "board configuration priority_labels must be a non-empty list of unique labels"
            )
        priority_labels = tuple(labels)
    idea_label = raw.get("idea_label")
    if idea_label is not None and (
        not isinstance(idea_label, str) or idea_label.strip() != idea_label or not idea_label
    ):
        raise protocol.ClaimError("board configuration idea_label must be a non-empty label")
    return BoardConfig(priority_labels, idea_label)


def _contract_field_value(
    live_body: str, matches: list[re.Match[str]], index: int, match: re.Match[str]
) -> tuple[str, str]:
    if match.re is CONTRACT_HEADING_PATTERN:
        name = match.group("name")
        next_heading = MARKDOWN_HEADING_PATTERN.search(live_body, match.end())
        next_field = matches[index + 1] if index + 1 < len(matches) else None
        end = min(
            next_heading.start() if next_heading is not None else len(live_body),
            next_field.start() if next_field is not None else len(live_body),
        )
        return name, live_body[match.end() : end].strip()
    name = match.group("bold_name") or match.group("plain_name")
    return name, match.group("value").strip()


def _collect_contract_sections(
    live_body: str, matches: list[re.Match[str]]
) -> tuple[dict[str, str], list[ContractDefect]]:
    sections: dict[str, str] = {}
    defects: list[ContractDefect] = []
    for index, match in enumerate(matches):
        name, value = _contract_field_value(live_body, matches, index, match)
        if name in sections:
            defects.append(ContractDefect(name, f"duplicate {name} projection field"))
            continue
        sections[name] = value
    return sections, defects


def _validate_blocked_by(sections: dict[str, str], defects: list[ContractDefect]) -> str | None:
    blocked_by = sections.get(BLOCKED_BY)
    if (
        blocked_by is not None
        and blocked_by != NO_BLOCKERS
        and BLOCKER_LIST_PATTERN.fullmatch(blocked_by) is None
    ):
        defects.append(
            ContractDefect(
                BLOCKED_BY,
                f"{BLOCKED_BY} must be exactly nichts or a comma-separated #N list",
            )
        )
    return blocked_by


def parse_contract(body: str) -> Contract:
    live_body = _live_text(body)
    matches = sorted(
        (
            *CONTRACT_HEADING_PATTERN.finditer(live_body),
            *CONTRACT_FIELD_PATTERN.finditer(live_body),
        ),
        key=re.Match.start,
    )
    sections, defects = _collect_contract_sections(live_body, matches)
    blocked_by = _validate_blocked_by(sections, defects)
    return Contract(
        now=sections.get("Now") or None,
        next=sections.get("Next") or None,
        blocked_by=blocked_by,
        done_when=sections.get("Done when") or None,
        defects=tuple(defects),
    )


def expectation_heading(body: str) -> re.Match[str] | None:
    return EXPECTATION_HEADING_PATTERN.search(body)


def _expectation_block_text(body: str, heading: re.Match[str]) -> str:
    next_heading = MARKDOWN_HEADING_PATTERN.search(body, heading.end())
    return body[heading.end() : next_heading.start() if next_heading is not None else len(body)]


def _expectation_lines(body: str, heading: re.Match[str]) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in _expectation_block_text(body, heading).splitlines()
        if EXPECTATION_LINE_SHAPE_PATTERN.match(line.strip())
    )


def _expectation_block_state(body: str, heading: re.Match[str]) -> ExpectationState:
    """The state of one expectation block.

    The heading itself carries the ruling when it matches the operator's
    `GEREGELT: Operator DD.MM.YYYY` marker (issue #78): the contract requires
    example, counterexample and default per line, so a ruled block is
    necessarily prose, not a machine-parsable pattern on every line. A line
    that still carries the explicit proposal marker is a contradiction to
    surface, not to swallow under a ruled heading, so it still forces
    PROPOSED. A ruled heading only excuses lines that are not themselves
    shaped like an expectation item (EXPECTATION_LINE_SHAPE_PATTERN): a list
    item added later, under the same heading, without its own ruled marker
    is silence wearing the heading's ruling, not a ruling of its own, so it
    still forces PROPOSED. A heading with no lines beneath it rules nothing
    and is PROPOSED. Without the heading marker, every non-empty line must
    carry the ruled-line pattern (issue #62): silence never rules.
    """
    lines = tuple(
        line.strip() for line in _expectation_block_text(body, heading).splitlines() if line.strip()
    )
    if any(PROPOSED_EXPECTATION_PATTERN.search(line) for line in lines):
        return ExpectationState.PROPOSED
    if not lines:
        return ExpectationState.PROPOSED
    if OPERATOR_RULING_DATE_PATTERN.search(heading.group(0)) is not None:
        unruled_expectation_shaped_lines = (
            line
            for line in lines
            if EXPECTATION_LINE_SHAPE_PATTERN.match(line)
            and not RULED_EXPECTATION_PATTERN.search(line)
        )
        if any(unruled_expectation_shaped_lines):
            return ExpectationState.PROPOSED
        return ExpectationState.RULED
    if all(RULED_EXPECTATION_PATTERN.search(line) for line in lines):
        return ExpectationState.RULED
    return ExpectationState.PROPOSED


def expectation_state(body: str) -> ExpectationState:
    headings = tuple(EXPECTATION_HEADING_PATTERN.finditer(body))
    if not headings:
        return ExpectationState.NONE
    block_states = tuple(_expectation_block_state(body, heading) for heading in headings)
    if any(state is ExpectationState.PROPOSED for state in block_states):
        return ExpectationState.PROPOSED
    return ExpectationState.RULED


def expectation_progress(body: str) -> ExpectationProgress:
    lines = tuple(
        line
        for heading in EXPECTATION_HEADING_PATTERN.finditer(body)
        for line in _expectation_lines(body, heading)
    )
    return ExpectationProgress(
        open=sum(not RULED_EXPECTATION_PATTERN.search(line) for line in lines), total=len(lines)
    )


def _parse_dotted_date(day: str, month: str, year: str) -> date:
    try:
        return date(int(year), int(month), int(day))
    except ValueError as error:
        raise protocol.ClaimError(
            f"expectation heading has an invalid date {day}.{month}.{year}"
        ) from error


def parse_ruling_date(body: str) -> date:
    """The date of the ruling shown for freshness (issue #62's "old" hint).

    Reads only the first expectation heading matched by
    EXPECTATION_HEADING_PATTERN. A body with several dated `## Erwartungen…`
    blocks (issue #78) therefore has its freshness driven by block order,
    not by the oldest or most relevant ruling — a known residual, left
    unfixed here. EXPECTATION_HEADING_PATTERN itself requires the heading to
    start with "Erwartung"/"Erwartungen"/"Erwartungsliste"; a heading like
    "Geregelte Erwartungen …" is not matched at all and contributes neither
    a state nor a date. Both gaps are named, not widened, by issue #78.
    """
    heading = expectation_heading(body)
    if heading is None:
        raise protocol.ClaimError("ruled expectations have no readable date")
    line = heading.group(0)
    operator = OPERATOR_RULING_DATE_PATTERN.search(line)
    if operator is not None:
        return _parse_dotted_date(*operator.groups())
    dates = {
        _parse_dotted_date(day, month, year)
        for day, month, year in DOTTED_DATE_PATTERN.findall(line)
    }
    if len(dates) == 1:
        return next(iter(dates))
    if not dates:
        raise protocol.ClaimError("ruled expectations have no readable date")
    raise protocol.ClaimError("ruled expectations have more than one date")


def _opening_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_OPENING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


def _closing_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_CLOSING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


def _live_text(body: str) -> str:
    """The body's non-fenced lines, joined back in order — what GitHub renders as prose.

    Walks the body once carrying CommonMark fence state: a line opens a fence
    (an info string after the run is allowed, e.g. ` ```python `), and only a
    later line with the *same* fence character, a run at least as long, and
    nothing but trailing whitespace after the run closes it again — a line
    like ` ```python ` never closes a fence, even one opened with backticks,
    because CommonMark forbids an info string on a closing delimiter; it is
    read as fence content instead. An opened fence that never closes runs to
    the end of the document, exactly as GitHub renders it — so an operator
    who left a fence unclosed, or wrote an info string on what they meant as
    a close, sees the same code block the tool does; there is no invisible
    divergence. `#72`'s own body fences its example this way, and it must
    never itself read as live.

    Not modeled: a 4-space-indented code block (CommonMark's other fencing
    form). A marker written there is read as live — visible on `board`/`next`
    and correctable by fencing it properly, never a silent divergence.
    """
    live_lines: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in body.splitlines():
        if fence_char is None:
            opening = _opening_fence_delimiter(line)
            if opening is not None:
                fence_char, fence_length = opening
                continue
            live_lines.append(line)
            continue
        closing = _closing_fence_delimiter(line)
        if closing is not None and closing[0] == fence_char and closing[1] >= fence_length:
            fence_char, fence_length = None, 0
        # Still inside the fence (or just closed it): never scanned for a marker.
    return "\n".join(live_lines)


def _table_row_cells(line: str) -> tuple[str, ...] | None:
    """A markdown table row's cells, or None when `line` isn't table-shaped.

    Leading/trailing `|` are optional, matching both the pipe-fenced style
    every slice table in this repository uses and the bare form CommonMark
    also allows.
    """
    stripped = line.strip()
    if "|" not in stripped:
        return None
    stripped = stripped.removeprefix("|")
    stripped = stripped.removesuffix("|")
    cells = tuple(cell.strip() for cell in stripped.split("|"))
    return cells or None


def _is_slice_table_separator(line: str) -> bool:
    cells = _table_row_cells(line)
    return (
        cells is not None
        and len(cells) == len(SLICE_TABLE_HEADER_CELLS)
        and all(_SLICE_TABLE_SEPARATOR_CELL_PATTERN.match(cell) is not None for cell in cells)
    )


def _slice_table_row(index: str, name: str, item_cell: str) -> SliceTableRow:
    if item_cell == UNDISPATCHED_SLICE_CELL:
        return SliceTableRow(int(index), name, item_cell, None)
    link = _SLICE_TABLE_ITEM_LINK_PATTERN.match(item_cell)
    return SliceTableRow(int(index), name, item_cell, int(link.group(1)) if link else None)


_SLICE_TABLE_HEADER_TRIGGER_WORDS = frozenset({"scheibe", "slice", "item"})


def _looks_like_slice_table_header(cells: tuple[str, ...]) -> bool:
    """A loose, deliberately over-eager heuristic: a `#`-first pipe row that
    also names one of the slice table's real column words — `Scheibe`,
    `Slice`, `Item`, or a `Hängt ab...` column — is an attempted slice
    table, whether or not it turns out well-formed. Catching it here —
    rather than only the exact header shape — is what makes a near-miss
    header (including the English "Slice" spelling) fail loud instead of
    reading as ordinary prose. `#` alone never counts: an ordinary table
    that happens to start with a `#` column stays untouched.
    """
    if cells[0].strip() != "#":
        return False
    return any(
        cell.strip().casefold() in _SLICE_TABLE_HEADER_TRIGGER_WORDS
        or cell.strip().casefold().startswith("hängt ab")
        for cell in cells[1:]
    )


def _slice_table_header_at(lines: list[str], line_index: int) -> tuple[bool, bool] | None:
    """Whether the candidate header at `line_index` is well-formed and separated.

    None when the line is not even a `#`-first slice-table attempt.
    """
    header_cells = _table_row_cells(lines[line_index])
    if header_cells is None or not _looks_like_slice_table_header(header_cells):
        return None
    well_formed_header = (
        len(header_cells) == len(SLICE_TABLE_HEADER_CELLS)
        and tuple(cell.casefold() for cell in header_cells) == SLICE_TABLE_HEADER_CELLS
    )
    has_separator = line_index + 1 < len(lines) and _is_slice_table_separator(lines[line_index + 1])
    return well_formed_header, has_separator


def _slice_table_rows(lines: list[str], start: int) -> tuple[tuple[SliceTableEntry, ...], int]:
    """The row block following a well-formed header, and the line index after it."""
    entries: list[SliceTableEntry] = []
    line_index = start
    while line_index < len(lines):
        row_cells = _table_row_cells(lines[line_index])
        if row_cells is None:
            break
        if (
            len(row_cells) != len(SLICE_TABLE_HEADER_CELLS)
            or _SLICE_TABLE_INDEX_PATTERN.match(row_cells[0]) is None
        ):
            entries.append(MalformedSliceRow(lines[line_index].strip()))
            line_index += 1
            continue
        entries.append(_slice_table_row(*row_cells[:3]))
        line_index += 1
    return tuple(entries), line_index


def parse_slice_table(body: str) -> tuple[SliceTableEntry, ...]:
    """Every slice table entry in `body` (`#79`'s grammar): a well-formed
    row, or a `MalformedSliceTable`/`MalformedSliceRow` marking a near-miss.

    A slice table is a markdown table whose header cells are exactly `#`,
    `Scheibe`, `Item`, `Hängt ab von`, in that order, case- and
    whitespace-insensitively, followed by a separator row — the shape
    atelier-2 #962 carries since 02.09. Any `#`-first row naming `Scheibe`
    that doesn't match that shape exactly (wrong columns, no separator) is
    `MalformedSliceTable` rather than silently ignored prose. Every table in
    the body is parsed, not just the first. Reads only `_live_text`, so a
    fenced example of the grammar never counts.
    """
    lines = _live_text(body).splitlines()
    entries: list[SliceTableEntry] = []
    line_index = 0
    while line_index < len(lines):
        header = _slice_table_header_at(lines, line_index)
        if header is None:
            line_index += 1
            continue
        well_formed_header, has_separator = header
        if not well_formed_header or not has_separator:
            entries.append(MalformedSliceTable(lines[line_index].strip()))
            line_index += 1
            continue
        row_entries, line_index = _slice_table_rows(lines, line_index + 2)
        entries.extend(row_entries)
    return tuple(entries)


def _issue_reference(match: re.Match[str], repository: str) -> IssueReference:
    return IssueReference(match.group("repository") or repository, int(match.group("number")))


def _references_matching(
    pattern: re.Pattern[str], text: str, repository: str
) -> frozenset[IssueReference]:
    """Routed through `_live_text` for the same reason every other marker in
    this module is: a fenced example of the closing-keyword convention
    ("Fixes #64" inside a code block, say) must document the syntax without
    silently closing #64.
    """
    return frozenset(
        _issue_reference(match, repository) for match in pattern.finditer(_live_text(text))
    )


def closing_references(text: str, repository: str) -> frozenset[IssueReference]:
    """Every issue merging this text closes, by GitHub's own keywords."""
    return _references_matching(CLOSING_REFERENCE_PATTERN, text, repository)


def _single_classification_match(
    matches: tuple[re.Match[str], ...],
) -> re.Match[str] | ClassificationDefect:
    """The one classification line a body must carry, or why it doesn't have one."""
    if not matches:
        return ClassificationDefect("carries no `Work-Item:` or `No-Item:` line")
    work_items = tuple(match for match in matches if match.group("kind").lower() == WORK_ITEM_KIND)
    if len(work_items) > 1:
        named = " and ".join(match.group("value").strip(" \t") for match in work_items[:2])
        return ClassificationDefect(f"names two work items, {named}; split it")
    if len(matches) > 1:
        return ClassificationDefect(
            f"carries {len(matches)} classification lines; exactly one is required"
        )
    return matches[0]


def _work_item_classification(value: str, repository: str) -> Classification | ClassificationDefect:
    reference = WORK_ITEM_VALUE_PATTERN.fullmatch(value)
    if reference is None:
        return ClassificationDefect(
            f"carries `Work-Item: {value}`; a work item reads OWNER/REPO#n or #n"
        )
    return WorkItemClassification(_issue_reference(reference, repository))


def _no_item_classification(value: str) -> Classification | ClassificationDefect:
    if value.lower() not in {kind.value for kind in NoItemKind}:
        return ClassificationDefect(
            f"carries `No-Item: {value}`; an issue-less pull request is docs or fix"
        )
    return NoItemClassification(NoItemKind(value.lower()))


def parse_pull_request_classification(
    body: str, repository: str
) -> Classification | ClassificationDefect:
    """The one `Work-Item:`/`No-Item:` line a pull request body must carry.

    A pull request either lands one work item and closes it, or declares
    itself issue-less documentation or a fix. Nothing else in a body names an
    item: a dispatched slice is its own item, and its pull request closes it.
    """
    matches = tuple(CLASSIFICATION_LINE_PATTERN.finditer(_live_text(body)))
    selected = _single_classification_match(matches)
    if isinstance(selected, ClassificationDefect):
        return selected
    value = selected.group("value").strip(" \t")
    if selected.group("kind").lower() == WORK_ITEM_KIND:
        return _work_item_classification(value, repository)
    return _no_item_classification(value)


def declared_work_items(pull_requests: tuple[PullRequest, ...], repository: str) -> frozenset[int]:
    """The issues of `repository` that these pull requests declare as their work item."""
    declared: set[int] = set()
    for pull_request in pull_requests:
        classification = parse_pull_request_classification(pull_request.body, repository)
        if (
            isinstance(classification, WorkItemClassification)
            and classification.item.repository == repository
        ):
            declared.add(classification.item.number)
    return frozenset(declared)


def slice_title_match(title: str) -> tuple[int, int] | None:
    """`(slice number, parent issue)` when `title` looks like a dispatched slice.

    Matches the three forms `#79` names: `(#<n> Scheibe <k>)`, `(#<n> slice
    <k>)`, and `Scheibe <k> von #<n>`. A title carrying none of them returns
    None — the heuristic simply has nothing to check.
    """
    match = _SLICE_TITLE_PARENTHETICAL_PATTERN.search(title) or _SLICE_TITLE_VON_PATTERN.search(
        title
    )
    if match is None:
        return None
    return int(match.group("slice")), int(match.group("parent"))


def frozen_trigger(body: str) -> str | None:
    """The operator's frozen-marker trigger sentence, or None when the item is not frozen.

    A line `Eingefroren bis: <trigger> (Operator, DD.MM.YYYY)` — bold or plain,
    matching the Now/Next/Blocked by/Done when field grammar, optionally
    prefixed by blockquote `>` markers — freezes the item. The tool checks
    only this form, never who wrote it: authority over freezing is the
    coordination contract's, not this parser's. Fenced text is documentation,
    never a live marker (see `_live_text`); a blockquoted marker is still
    live — this repo already quotes operator rulings, so a quoted freeze line
    reads as the freeze itself. A malformed marker outside a fence still
    fails loud: a real typo must stay visible.
    """
    line = FROZEN_LINE_PATTERN.search(_live_text(body))
    if line is None:
        return None
    match = FROZEN_TRIGGER_PATTERN.fullmatch(line.group("value").strip())
    if match is None:
        raise protocol.ClaimError(
            "frozen marker must read "
            "'Eingefroren bis: <trigger in one sentence> (Operator, DD.MM.YYYY)'"
        )
    _parse_dotted_date(match.group("day"), match.group("month"), match.group("year"))
    return match.group("trigger").strip()


def landings_since(trunk_landings: tuple[datetime, ...], ruling: date) -> int:
    start = datetime(ruling.year, ruling.month, ruling.day, tzinfo=UTC) + timedelta(days=1)
    return sum(1 for moment in trunk_landings if moment >= start)


def ruling_freshness(
    body: str, trunk_landings: tuple[datetime, ...]
) -> tuple[int | None, bool | None]:
    if expectation_state(body) is not ExpectationState.RULED:
        return None, None
    count = landings_since(trunk_landings, parse_ruling_date(body))
    return count, count >= RULING_OLD_AFTER_LANDINGS


def _references(text: str | None) -> frozenset[int]:
    if text is None:
        return frozenset()
    return frozenset(int(number) for number in REFERENCE_PATTERN.findall(text))


def _blocker_references(text: str | None) -> frozenset[int]:
    if text is None or text == NO_BLOCKERS or BLOCKER_LIST_PATTERN.fullmatch(text) is None:
        return frozenset()
    return _references(text)


def blocker_references(issues: tuple[Issue, ...]) -> frozenset[int]:
    return frozenset(
        blocker for issue in issues for blocker in parse_contract(issue.body).blocker_issues
    )


def _with_blocker_defects(contract: Contract, blockers: dict[int, BlockerReference]) -> Contract:
    pull_requests = tuple(
        blocker for blocker in sorted(contract.blocker_issues) if blockers[blocker].is_pull_request
    )
    if not pull_requests:
        return contract
    return replace(
        contract,
        defects=(
            *contract.defects,
            *(
                ContractDefect(BLOCKED_BY, f"blocker #{blocker} is a pull request")
                for blocker in pull_requests
            ),
        ),
    )


def _open_blockers(contract: Contract, blockers: dict[int, BlockerReference]) -> tuple[int, ...]:
    return tuple(
        blocker
        for blocker in sorted(contract.blocker_issues)
        if (not blockers[blocker].is_pull_request and blockers[blocker].state is BlockerState.OPEN)
    )


def _freed_on(contract: Contract, blockers: dict[int, BlockerReference]) -> datetime | None:
    issue_blockers = tuple(
        blockers[blocker]
        for blocker in contract.blocker_issues
        if not blockers[blocker].is_pull_request
    )
    if not issue_blockers or any(
        blocker.state is not BlockerState.CLOSED for blocker in issue_blockers
    ):
        return None
    return max(
        (blocker.closed_at for blocker in issue_blockers if blocker.closed_at is not None),
        default=None,
    )


def claim_age(created_at: str, now: datetime) -> timedelta:
    return now.astimezone(UTC) - _timestamp(created_at)


def _floored_claim_minutes(age: timedelta) -> int:
    return max(0, int(age.total_seconds())) // 60


def format_claim_age(age: timedelta) -> str:
    hours, minutes = divmod(_floored_claim_minutes(age), 60)
    return f"{hours}h {minutes}m"


def claim_is_old(age: timedelta) -> bool:
    return age > CLAIM_OLD_AFTER


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp") from error
    if parsed.tzinfo is None:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp")
    return parsed.astimezone(UTC)


def _single_concrete_next(value: str | None) -> bool:
    if value is None:
        return False
    lines = tuple(line.strip(" -\t") for line in value.splitlines() if line.strip())
    return len(lines) == 1 and lines[0].casefold() not in {"tbd", "todo", "unknown"}


def _claim_by_issue(claims: tuple[protocol.ActiveClaim, ...]) -> dict[int, protocol.ActiveClaim]:
    issue_claims = (claim for claim in claims if isinstance(claim.identity, protocol.IssueIdentity))
    return {claim.identity.issue: claim for claim in issue_claims}


def _priority_index(labels: tuple[str, ...], config: BoardConfig) -> int | None:
    priorities = {label.casefold(): index for index, label in enumerate(config.priority_labels)}
    matches = (priorities[label.casefold()] for label in labels if label.casefold() in priorities)
    return min(matches, default=None)


def _has_label(labels: tuple[str, ...], label: str | None) -> bool:
    return label is not None and any(item.casefold() == label.casefold() for item in labels)


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


def _associated_issues(pull_requests: tuple[PullRequest, ...], repository: str) -> frozenset[int]:
    """Issues of `repository` that these pull requests close or claim to land."""
    return frozenset(
        reference.number
        for pull_request in pull_requests
        for reference in _references_matching(
            LANDING_CLAIM_PATTERN, f"{pull_request.title}\n{pull_request.body}", repository
        )
        if reference.repository == repository
    )


def _touched_without_closing(pull_requests: tuple[PullRequest, ...]) -> frozenset[int]:
    """Issues a pull request advances without closing — an epic's slices, typically.

    The coordination contract requires a slice to become its own item at
    dispatch, so an epic's work lands through its children's pull requests,
    which deliberately avoid a closing keyword against the epic itself (see
    `TOUCHES_WITHOUT_CLOSING_LINE_PATTERN`). Without this, an epic that is
    cut correctly can never earn a landed or in-flight stage.

    Named residual: this is a syntactic marker, not a validated parent-child
    relation. `unblocks`/`open_blockers` (this module's one real relation)
    only connect two issues through a structured `Blocked by` field; GitHub
    exposes no equivalent structured field for a non-closing PR-to-issue
    link, and this repository's own children reference their epic through
    inconsistent free text (a title suffix, a "Nachbarn" list, a "Refs"/"Part
    of" line) — there is no honest typed relation here to check against. A
    foreign pull request that writes a dedicated, single "Refs #N" line for
    an unrelated reason still confers a stage; that risk is real and is not
    eliminated below, only narrowed. The one real narrowing available:
    every observed genuine slice-to-epic reference (#848, #960, #965) names
    its epic a second time elsewhere in the same pull request, in
    substantive prose — never only in the trailer line — so a marker with no
    corroborating mention elsewhere in the text is dropped. Fenced code
    blocks are never live text (`_live_text`), matching every other marker
    this module reads.
    """
    touched: set[int] = set()
    for pull_request in pull_requests:
        live = _live_text(f"{pull_request.title}\n{pull_request.body}")
        marked = frozenset(
            number
            for line in TOUCHES_WITHOUT_CLOSING_LINE_PATTERN.findall(live)
            for number in _references(line)
        )
        if not marked:
            continue
        corroborated = _references(TOUCHES_WITHOUT_CLOSING_LINE_PATTERN.sub("", live))
        touched |= marked & corroborated
    return frozenset(touched)


def board_rank(item: BoardItem) -> tuple[int, int, int]:
    """The one order `items`, `ready_now`, and every "is X ahead of Y" comparison share.

    `build_board` sorts by this key; any caller that needs to know whether
    one item outranks another — the out-of-order warning, for instance —
    reads this instead of re-deriving its own notion of "ahead", which is
    exactly how `board` and `next` fell out of agreement before.
    """
    return (item.priority_category, -item.score, item.number)


def _validated_blocker_by_number(
    issues: tuple[Issue, ...],
    open_pull_requests: tuple[PullRequest, ...],
    contracts: dict[int, Contract],
    blocker_references: tuple[BlockerReference, ...] | None,
) -> tuple[dict[int, BlockerReference], tuple[BlockerReference, ...]]:
    """The by-number blocker map, and the resolved `blocker_references` behind it.

    Raises when GitHub did not return every blocker a contract names, or omitted
    `closed_at` for one it reports closed.
    """
    referenced_blockers = frozenset(
        blocker for contract in contracts.values() for blocker in contract.blocker_issues
    )
    if blocker_references is None:
        blocker_references = (
            *(BlockerReference(issue.number, BlockerState.OPEN, False) for issue in issues),
            *(
                BlockerReference(pull_request.number, BlockerState.OPEN, True)
                for pull_request in open_pull_requests
            ),
        )
    blocker_by_number = {reference.number: reference for reference in blocker_references}
    missing_blockers = referenced_blockers - blocker_by_number.keys()
    if missing_blockers:
        missing = min(missing_blockers)
        raise protocol.ClaimError(f"GitHub did not return blocker #{missing}")
    invalid_closed_blockers = tuple(
        reference.number
        for reference in blocker_by_number.values()
        if reference.state is BlockerState.CLOSED and reference.closed_at is None
    )
    if invalid_closed_blockers:
        raise protocol.ClaimError(
            f"GitHub did not return closed_at for blocker #{min(invalid_closed_blockers)}"
        )
    return blocker_by_number, blocker_references


@dataclass(frozen=True)
class _BoardBuildContext:
    """Per-run board state that every issue's `BoardItem` is derived against."""

    contracts: dict[int, Contract]
    blockers: dict[int, tuple[int, ...]]
    freed_on: dict[int, datetime | None]
    unblocks: dict[int, int]
    claims_by_issue: dict[int, protocol.ActiveClaim]
    in_flight_references: frozenset[int]
    landed_references: frozenset[int]
    open_branches: frozenset[str]
    trunk_landings: tuple[datetime, ...]


def _board_stage(
    issue: Issue,
    claim: protocol.ActiveClaim | None,
    *,
    in_flight_references: frozenset[int],
    landed_references: frozenset[int],
    open_branches: frozenset[str],
) -> Stage:
    in_flight = issue.number in in_flight_references or (
        claim is not None and claim.branch in open_branches
    )
    if in_flight:
        return Stage.IN_FLIGHT
    if issue.number in landed_references:
        return Stage.CODE_LANDED
    return Stage.TEXT_ONLY


def _claim_projection(
    claim: protocol.ActiveClaim | None, observed_at: datetime
) -> tuple[str | None, str | None, bool]:
    """The (active_claim, claim_age, claim_old) trio a `BoardItem` shows for `claim`."""
    if claim is None:
        return None, None, False
    age = claim_age(claim.comment.created_at, observed_at)
    return f"{claim.agent} ({claim.role})", format_claim_age(age), claim_is_old(age)


def _board_score(stage: Stage, unblocks_count: int, single_next: bool) -> int:
    score = 20 * unblocks_count
    score += {Stage.IN_FLIGHT: 30, Stage.CODE_LANDED: 20, Stage.TEXT_ONLY: -20}[stage]
    score += 10 if single_next else 0
    return score


def _board_item(
    issue: Issue, context: _BoardBuildContext, config: BoardConfig, observed_at: datetime
) -> BoardItem:
    contract = context.contracts[issue.number]
    freed_at = context.freed_on[issue.number]
    ruling_landings, ruling_old = ruling_freshness(issue.body, context.trunk_landings)
    frozen = frozen_trigger(issue.body)
    claim = context.claims_by_issue.get(issue.number)
    stage = _board_stage(
        issue,
        claim,
        in_flight_references=context.in_flight_references,
        landed_references=context.landed_references,
        open_branches=context.open_branches,
    )
    single_next = _single_concrete_next(contract.next)
    projectionless_idea = contract.projectionless and _has_label(issue.labels, config.idea_label)
    next_step = IDEA_REFINEMENT_STEP if projectionless_idea else contract.next
    unblocks_count = context.unblocks[issue.number]
    priority_category, priority_bucket = _priority_bucket(issue.labels, config, unblocks_count)
    active_claim, claim_age_text, claim_old = _claim_projection(claim, observed_at)
    open_blockers = context.blockers[issue.number]
    actionable_reason = _actionable_reason(
        frozen_trigger=frozen,
        active_claim=active_claim,
        open_blockers=open_blockers,
        contract_complete=contract.complete,
        projectionless_idea=projectionless_idea,
    )
    return BoardItem(
        number=issue.number,
        title=issue.title,
        labels=issue.labels,
        priority_category=priority_category,
        priority_bucket=priority_bucket,
        contract=contract,
        next_step=next_step,
        contract_complete=contract.complete,
        expectation_state=expectation_state(issue.body),
        expectation_progress=expectation_progress(issue.body),
        ruling_landings=ruling_landings,
        ruling_old=ruling_old,
        frozen_trigger=frozen,
        open_blockers=open_blockers,
        freed_on=freed_at,
        freed_days=(None if freed_at is None else max(0, (observed_at - freed_at).days)),
        stage=stage,
        age_days=max(0, (observed_at - _timestamp(issue.created_at)).days),
        idle_days=max(0, (observed_at - _timestamp(issue.updated_at)).days),
        active_claim=active_claim,
        claim_age=claim_age_text,
        claim_old=claim_old,
        unblocks_count=unblocks_count,
        score=_board_score(stage, unblocks_count, single_next),
        actionable=actionable_reason is None,
        actionable_reason=actionable_reason,
    )


def build_board(  # noqa: PLR0913  # protocol/board slice, #103
    issues: tuple[Issue, ...],
    open_pull_requests: tuple[PullRequest, ...],
    recent_merged_pull_requests: tuple[PullRequest, ...],
    claims: tuple[protocol.ActiveClaim, ...],
    config: BoardConfig,
    *,
    repository: str,
    blocker_references: tuple[BlockerReference, ...] | None = None,
    now: datetime | None = None,
    trunk_landings: tuple[datetime, ...] = (),
) -> Board:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    contracts = {issue.number: parse_contract(issue.body) for issue in issues}
    blocker_by_number, blocker_references = _validated_blocker_by_number(
        issues, open_pull_requests, contracts, blocker_references
    )
    contracts = {
        issue.number: _with_blocker_defects(contracts[issue.number], blocker_by_number)
        for issue in issues
    }
    blockers = {
        issue.number: _open_blockers(contracts[issue.number], blocker_by_number) for issue in issues
    }
    unblocks = {
        issue.number: sum(issue.number in other_blockers for other_blockers in blockers.values())
        for issue in issues
    }
    context = _BoardBuildContext(
        contracts=contracts,
        blockers=blockers,
        freed_on={
            issue.number: _freed_on(contracts[issue.number], blocker_by_number) for issue in issues
        },
        unblocks=unblocks,
        claims_by_issue=_claim_by_issue(claims),
        in_flight_references=_associated_issues(open_pull_requests, repository)
        | _touched_without_closing(open_pull_requests),
        landed_references=_associated_issues(recent_merged_pull_requests, repository)
        | _touched_without_closing(recent_merged_pull_requests),
        open_branches=frozenset(pr.head_ref_name for pr in open_pull_requests),
        trunk_landings=trunk_landings,
    )
    landed_work_items = declared_work_items(recent_merged_pull_requests, repository)
    ordered = tuple(
        sorted(
            (_board_item(issue, context, config, observed_at) for issue in issues),
            key=board_rank,
        )
    )
    return Board(
        items=ordered,
        ready_now=tuple(item for item in ordered if item.actionable),
        stale=tuple(
            item
            for item in ordered
            if item.idle_days > STALE_IDLE_DAYS and item.stage is Stage.TEXT_ONLY
        ),
        recovery=tuple(
            item
            for item in ordered
            if item.number in landed_work_items and item.number != protocol.LEDGER_ISSUE
        ),
        blocker_references=blocker_references,
    )


def highest_scored_actionable(board: Board) -> BoardItem | None:
    """The one item `next` recommends — always `board`'s own top row.

    `ready_now` is a filtered view of `items`, which `build_board` orders by
    `(priority_category, -score, number)`; filtering preserves that order, so
    its first element is `board`'s own top-ranked actionable row. Two
    commands over one board must not disagree, so this reads that order
    instead of maximizing score on its own — an unlabelled item with a
    higher score must never outrank a human's priority label.
    """
    return next(iter(board.ready_now), None)


def board_json(board: Board) -> str:
    payload = asdict(board)
    payload.pop("blocker_references")
    for group in ("items", "ready_now", "stale", "recovery"):
        for item in payload[group]:
            freed_on = item["freed_on"]
            item["freed_on"] = (
                None if freed_on is None else freed_on.astimezone(UTC).date().isoformat()
            )
    return json.dumps(payload, default=lambda value: value.value)


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
            "FREED",
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
                _expectation_cell(item),
                _brief(item.next_step),
                str(item.age_days),
                str(item.idle_days),
                _freed_cell(item),
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
    recovery = ", ".join(f"#{item.number}" for item in board.recovery) or "none"
    return (
        f"{table}\n\nREADY NOW\n{ready}\n\nSTALE\n{stale}\n\nRECOVERY ({RECOVERY_STEP})\n{recovery}"
    )


def _contract_summary(contract: Contract) -> str:
    present = (
        name
        for name, value in (
            ("Now", contract.now),
            ("Next", contract.next),
            (BLOCKED_BY, contract.blocked_by),
            ("Done when", contract.done_when),
        )
        if value is not None
    )
    return ", ".join(present) or "-"


def _actionable_reason(
    *,
    frozen_trigger: str | None,
    active_claim: str | None,
    open_blockers: tuple[int, ...],
    contract_complete: bool,
    projectionless_idea: bool,
) -> str | None:
    if frozen_trigger is not None:
        return f"frozen: {frozen_trigger}"
    if active_claim is not None:
        return "claimed"
    if open_blockers:
        return "blocked by " + ", ".join(f"#{number}" for number in open_blockers)
    if not contract_complete and not projectionless_idea:
        return "body incomplete"
    return None


def _expectation_cell(item: BoardItem) -> str:
    if item.expectation_state is ExpectationState.NONE:
        return "-"
    if item.expectation_state is ExpectationState.PROPOSED:
        return f"{item.expectation_progress.open}/{item.expectation_progress.total}"
    count = 0 if item.ruling_landings is None else item.ruling_landings
    suffix = " old" if item.ruling_old else ""
    return f"ruled {count}{suffix}"


def _claim_cell(item: BoardItem) -> str:
    if item.active_claim is None:
        return "-"
    suffix = " old" if item.claim_old else ""
    return f"{item.active_claim} {item.claim_age}{suffix}"


def _freed_cell(item: BoardItem) -> str:
    if item.freed_on is None or item.freed_days is None:
        return "-"
    freed_date = item.freed_on.astimezone(UTC).date().isoformat()
    return f"{freed_date} ({item.freed_days} d)"


def _brief(value: str | None, *, maximum: int = 48) -> str:
    if value is None:
        return "-"
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= maximum else one_line[: maximum - 1] + "…"
