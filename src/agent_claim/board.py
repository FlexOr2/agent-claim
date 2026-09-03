"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from . import protocol

DEFAULT_PRIORITY_LABELS = ("security", "data", "ci", "product", "ux", "cleanup")
CONFIG_PATH = Path(".agent-claim/board.toml")
IDEA_REFINEMENT_STEP = "Problem neu prüfen und Item verfeinern"
CONTRACT_HEADING_PATTERN = re.compile(
    r"(?m)^#{1,6}[ \t]+(?P<name>Now|Next|Blocked by|Done when)[ \t]*$"
)
CONTRACT_FIELD_PATTERN = re.compile(
    r"(?m)^(?:\*\*(?P<bold_name>Now|Next|Blocked by|Done when):\*\*|"
    r"(?P<plain_name>Now|Next|Blocked by|Done when):)[ \t]*(?P<value>[^\r\n]*)$"
)
BLOCKER_LIST_PATTERN = re.compile(r"#([1-9][0-9]*)(?:[ \t]*,[ \t]*#([1-9][0-9]*))*")
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
FROZEN_LINE_PATTERN = re.compile(
    r"(?m)^(?:>[ \t]*)*(?:\*\*Eingefroren bis:\*\*|Eingefroren bis:)[ \t]*(?P<value>[^\r\n]*)$"
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
FENCE_OPENING_PATTERN = re.compile(r"^[ ]{0,3}(?P<run>`{3,}|~{3,})")
FENCE_CLOSING_PATTERN = re.compile(r"^[ ]{0,3}(?P<run>`{3,}|~{3,})[ \t]*$")
PROPOSED_EXPECTATION_PATTERN = re.compile(
    r"\*\(Default:[ \t]*(?:yes|no|later)\)\*", re.IGNORECASE
)
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
REFERENCE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])#([1-9][0-9]*)")
CLOSING_REFERENCE_PATTERN = re.compile(
    r"(?im)\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?|"
    r"land(?:s|ed)?|implement(?:s|ed)?)\s*:?\s*#([1-9][0-9]*)"
)
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
CUT_HEADING_PATTERN = re.compile(r"(?m)^##[ \t]+Schnitt")
CUT_SECTION_HEADING_PATTERN = re.compile(r"(?m)^##[ \t]+")
SLICE_LINE_PATTERN = re.compile(r"(?m)^\*\*Scheibe [1-9][0-9]*:[ \t]*\S.*?\*\*\s*$")
# The slice table's header cells, in order, compared case- and
# whitespace-insensitively (`_table_row_cells` already strips each cell).
SLICE_TABLE_HEADER_CELLS = ("#", "scheibe", "item", "hängt ab von")
_SLICE_TABLE_SEPARATOR_CELL_PATTERN = re.compile(r"^:?-+:?$")
_SLICE_TABLE_INDEX_PATTERN = re.compile(r"^[1-9][0-9]*$")
_SLICE_TABLE_ITEM_LINK_PATTERN = re.compile(r"^#([1-9][0-9]*)$")
UNDISPATCHED_SLICE_CELL = "—"
# A stricter sibling of `TOUCHES_WITHOUT_CLOSING_LINE_PATTERN`, deliberately
# not reused: that pattern accepts several markers and arbitrary trailing
# text because a PR may carry more than one reference in one line; an issue
# has exactly one parent, so this line-anchored form accepts only `Part of
# #<n>` (optionally followed by a period) and nothing else on the line — a
# second number, or trailing prose, means the line isn't read as a parent
# relation at all.
PART_OF_LINE_PATTERN = re.compile(r"(?im)^Part of #([1-9][0-9]*)\.?[ \t]*$")
# The three slice-title forms seen in atelier-2 (`#79`): a parenthetical
# after the real title (`(#962 Scheibe 4)`, `(#962 slice 4)`) or a leading
# German phrase (`Scheibe 4 von #962`).
_SLICE_TITLE_PARENTHETICAL_PATTERN = re.compile(
    r"\(#(?P<parent>[1-9][0-9]*)[ \t]+(?:Scheibe|slice)[ \t]+(?P<slice>[1-9][0-9]*)\)",
    re.IGNORECASE,
)
_SLICE_TITLE_VON_PATTERN = re.compile(
    r"Scheibe[ \t]+(?P<slice>[1-9][0-9]*)[ \t]+von[ \t]+#(?P<parent>[1-9][0-9]*)",
    re.IGNORECASE,
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
    """`items`, and therefore `ready_now`, are ordered `(priority_category, -score, number)`.

    `ready_now` and `stale` are filters over `items`; filtering never
    reorders, so `ready_now[0]` is always `items`' first actionable row —
    the same row a human reading `board` sees first. `next` relies on this.
    """

    items: tuple[BoardItem, ...]
    ready_now: tuple[BoardItem, ...]
    stale: tuple[BoardItem, ...]
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


def parse_contract(body: str) -> Contract:
    live_body = _live_text(body)
    sections: dict[str, str] = {}
    defects: list[ContractDefect] = []
    matches = sorted(
        (
            *CONTRACT_HEADING_PATTERN.finditer(live_body),
            *CONTRACT_FIELD_PATTERN.finditer(live_body),
        ),
        key=re.Match.start,
    )
    for index, match in enumerate(matches):
        if match.re is CONTRACT_HEADING_PATTERN:
            name = match.group("name")
            next_heading = MARKDOWN_HEADING_PATTERN.search(live_body, match.end())
            next_field = matches[index + 1] if index + 1 < len(matches) else None
            end = min(
                next_heading.start() if next_heading is not None else len(live_body),
                next_field.start() if next_field is not None else len(live_body),
            )
            value = live_body[match.end() : end].strip()
        else:
            name = match.group("bold_name") or match.group("plain_name")
            value = match.group("value").strip()
        if name in sections:
            defects.append(ContractDefect(name, f"duplicate {name} projection field"))
            continue
        sections[name] = value
    blocked_by = sections.get("Blocked by")
    if (
        blocked_by is not None
        and blocked_by != NO_BLOCKERS
        and BLOCKER_LIST_PATTERN.fullmatch(blocked_by) is None
    ):
        defects.append(
            ContractDefect(
                "Blocked by",
                "Blocked by must be exactly nichts or a comma-separated #N list",
            )
        )
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
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = tuple(cell.strip() for cell in stripped.split("|"))
    return cells if cells else None


def _is_slice_table_separator(line: str) -> bool:
    cells = _table_row_cells(line)
    return cells is not None and len(cells) == len(SLICE_TABLE_HEADER_CELLS) and all(
        _SLICE_TABLE_SEPARATOR_CELL_PATTERN.match(cell) is not None for cell in cells
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
        header_cells = _table_row_cells(lines[line_index])
        if header_cells is None or not _looks_like_slice_table_header(header_cells):
            line_index += 1
            continue
        well_formed_header = len(header_cells) == len(SLICE_TABLE_HEADER_CELLS) and tuple(
            cell.casefold() for cell in header_cells
        ) == SLICE_TABLE_HEADER_CELLS
        has_separator = line_index + 1 < len(lines) and _is_slice_table_separator(
            lines[line_index + 1]
        )
        if not well_formed_header or not has_separator:
            entries.append(MalformedSliceTable(lines[line_index].strip()))
            line_index += 1
            continue
        line_index += 2
        while line_index < len(lines):
            row_cells = _table_row_cells(lines[line_index])
            if row_cells is None:
                break
            if len(row_cells) != len(SLICE_TABLE_HEADER_CELLS) or _SLICE_TABLE_INDEX_PATTERN.match(
                row_cells[0]
            ) is None:
                entries.append(MalformedSliceRow(lines[line_index].strip()))
                line_index += 1
                continue
            entries.append(_slice_table_row(*row_cells[:3]))
            line_index += 1
    return tuple(entries)


def parent_line_numbers(body: str) -> frozenset[int]:
    """Every issue number named on its own `Part of #<n>` line."""
    return frozenset(
        int(match.group(1)) for match in PART_OF_LINE_PATTERN.finditer(_live_text(body))
    )


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
    start = datetime(ruling.year, ruling.month, ruling.day, tzinfo=timezone.utc) + timedelta(
        days=1
    )
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
        blocker
        for issue in issues
        for blocker in parse_contract(issue.body).blocker_issues
    )


def _with_blocker_defects(
    contract: Contract, blockers: dict[int, BlockerReference]
) -> Contract:
    pull_requests = tuple(
        blocker
        for blocker in sorted(contract.blocker_issues)
        if blockers[blocker].is_pull_request
    )
    if not pull_requests:
        return contract
    return replace(
        contract,
        defects=(
            *contract.defects,
            *(
                ContractDefect("Blocked by", f"blocker #{blocker} is a pull request")
                for blocker in pull_requests
            ),
        ),
    )


def _open_blockers(contract: Contract, blockers: dict[int, BlockerReference]) -> tuple[int, ...]:
    return tuple(
        blocker
        for blocker in sorted(contract.blocker_issues)
        if (
            not blockers[blocker].is_pull_request
            and blockers[blocker].state is BlockerState.OPEN
        )
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
    return age > CLAIM_OLD_AFTER


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


def _associated_issues(pull_requests: tuple[PullRequest, ...]) -> frozenset[int]:
    """Issues a pull request closes, read the way GitHub renders it.

    Routed through `_live_text` for the same reason every other marker in
    this module is: a fenced example of the closing-keyword convention
    ("Fixes #64" inside a code block, say) must document the syntax without
    silently closing #64.
    """
    return frozenset(
        reference
        for pull_request in pull_requests
        for reference in (
            int(number)
            for number in CLOSING_REFERENCE_PATTERN.findall(
                _live_text(f"{pull_request.title}\n{pull_request.body}")
            )
        )
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


def build_board(
    issues: tuple[Issue, ...],
    open_pull_requests: tuple[PullRequest, ...],
    recent_merged_pull_requests: tuple[PullRequest, ...],
    claims: tuple[protocol.ActiveClaim, ...],
    config: BoardConfig,
    *,
    blocker_references: tuple[BlockerReference, ...] | None = None,
    now: datetime | None = None,
    trunk_landings: tuple[datetime, ...] = (),
) -> Board:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    contracts = {issue.number: parse_contract(issue.body) for issue in issues}
    referenced_blockers = frozenset(
        blocker
        for contract in contracts.values()
        for blocker in contract.blocker_issues
    )
    if blocker_references is None:
        blocker_references = (
            *(
                BlockerReference(issue.number, BlockerState.OPEN, False)
                for issue in issues
            ),
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
    contracts = {
        issue.number: _with_blocker_defects(contracts[issue.number], blocker_by_number)
        for issue in issues
    }
    blockers = {
        issue.number: _open_blockers(contracts[issue.number], blocker_by_number)
        for issue in issues
    }
    freed_on = {
        issue.number: _freed_on(contracts[issue.number], blocker_by_number)
        for issue in issues
    }
    unblocks = {
        issue.number: sum(issue.number in other_blockers for other_blockers in blockers.values())
        for issue in issues
    }
    claims_by_issue = _claim_by_issue(claims)
    in_flight_references = _associated_issues(open_pull_requests) | _touched_without_closing(
        open_pull_requests
    )
    landed_references = _associated_issues(recent_merged_pull_requests) | _touched_without_closing(
        recent_merged_pull_requests
    )
    open_branches = frozenset(pr.head_ref_name for pr in open_pull_requests)

    items: list[BoardItem] = []
    for issue in issues:
        contract = contracts[issue.number]
        expectations = expectation_state(issue.body)
        progress = expectation_progress(issue.body)
        ruling_landings, ruling_old = ruling_freshness(issue.body, trunk_landings)
        frozen = frozen_trigger(issue.body)
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
        projectionless_idea = contract.projectionless and _has_label(
            issue.labels, config.idea_label
        )
        next_step = IDEA_REFINEMENT_STEP if projectionless_idea else contract.next
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
            frozen_trigger=frozen,
            active_claim=active_claim,
            open_blockers=blockers[issue.number],
            contract_complete=contract.complete,
            projectionless_idea=projectionless_idea,
        )
        items.append(
            BoardItem(
                number=issue.number,
                title=issue.title,
                labels=issue.labels,
                priority_category=priority_category,
                priority_bucket=priority_bucket,
                contract=contract,
                next_step=next_step,
                contract_complete=contract.complete,
                expectation_state=expectations,
                expectation_progress=progress,
                ruling_landings=ruling_landings,
                ruling_old=ruling_old,
                frozen_trigger=frozen,
                open_blockers=blockers[issue.number],
                freed_on=freed_on[issue.number],
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
    ordered = tuple(sorted(items, key=board_rank))
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
    # S2b owns FREED; claim checks consume blocker details only inside this process.
    payload.pop("blocker_references")
    for group in ("items", "ready_now", "stale"):
        for item in payload[group]:
            item.pop("freed_on")
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


def _brief(value: str | None, *, maximum: int = 48) -> str:
    if value is None:
        return "-"
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= maximum else one_line[: maximum - 1] + "…"
