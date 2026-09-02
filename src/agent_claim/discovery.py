"""Discover, adopt, and create the canonical claim ledger issue."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .github import GitHubIssueComments
from .protocol import (
    LEDGER_BODY_MARKER,
    LEDGER_LABEL,
    MARKER_SUFFIX,
    TRUSTED_ASSOCIATIONS,
    ClaimError,
    ClaimUnavailable,
    claim_label,
)


@dataclass(frozen=True)
class _LedgerIssue:
    number: int
    state: str
    locked: bool
    body: str
    author_association: str
    labels: tuple[str, ...]
    is_pull_request: bool


def _ledger_issue_rows(
    client: GitHubIssueComments, state: str = "all", *, label: str | None = None
) -> tuple[_LedgerIssue, ...]:
    label_filter = f"&labels={label}" if label else ""
    raw = client._run(
        [
            "api",
            "--paginate",
            f"repos/{client.repository}/issues?state={state}{label_filter}&per_page=100",
            "--jq",
            (
                ".[] | {number,state,locked,body,author_association,"
                'labels:(.labels | map(.name)),is_pull_request:has("pull_request")}'
            ),
        ]
    )
    rows: list[_LedgerIssue] = []
    for value in client._json_lines(raw, "ledger-issue"):
        if not isinstance(value, dict):
            raise ClaimError("GitHub returned a malformed ledger issue")
        number = value.get("number")
        labels = value.get("labels")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or value.get("state") not in {"open", "closed"}
            or not isinstance(value.get("locked"), bool)
            or not isinstance(value.get("body"), str)
            or not isinstance(value.get("author_association"), str)
            or not isinstance(labels, list)
            or not all(isinstance(label, str) for label in labels)
            or not isinstance(value.get("is_pull_request"), bool)
        ):
            raise ClaimError("GitHub returned a malformed ledger issue")
        rows.append(
            _LedgerIssue(
                number,
                value["state"],
                value["locked"],
                value["body"],
                value["author_association"],
                tuple(labels),
                value["is_pull_request"],
            )
        )
    return tuple(rows)


def _issue_first_line(issue: _LedgerIssue) -> str:
    return issue.body.partition("\n")[0]


def _foreign_contract(issue: _LedgerIssue) -> bool:
    first = _issue_first_line(issue)
    if first == LEDGER_BODY_MARKER:
        return False
    return first.startswith("<!-- ") and ("claim" in first or "ledger" in first)


def _trusted_ledger_issue(issue: _LedgerIssue) -> bool:
    return issue.author_association in TRUSTED_ASSOCIATIONS


def _select_ledger(rows: tuple[_LedgerIssue, ...]) -> int | None:
    """Resolve the canonical ledger from an issue-row snapshot, or raise on
    a locked-marker violation or a competing foreign coordination contract."""
    ledgers: list[int] = []
    foreign: list[int] = []
    for issue in rows:
        if issue.is_pull_request:
            continue
        if not _trusted_ledger_issue(issue):
            continue
        if issue.state == "open" and _foreign_contract(issue):
            foreign.append(issue.number)
            continue
        if _issue_first_line(issue) != LEDGER_BODY_MARKER or issue.state == "closed":
            continue
        if not issue.locked:
            raise ClaimUnavailable(f"ledger candidate #{issue.number} is not locked; run bootstrap")
        ledgers.append(issue.number)
    if foreign:
        raise ClaimError(
            f"another coordination contract exists on issue(s) {foreign}; refusing to compete"
        )
    return min(ledgers) if ledgers else None


def _open_issue_count(client: GitHubIssueComments) -> int:
    """The repository's live open-issue-and-pull-request count, from the
    single-request repository resource rather than a paginated listing."""
    raw = client._run(["api", f"repos/{client.repository}", "--jq", ".open_issues_count"])
    try:
        count = int(raw)
    except ValueError as error:
        raise ClaimError("GitHub returned a malformed open-issue count") from error
    if count < 0:
        raise ClaimError("GitHub returned a malformed open-issue count")
    return count


def discover_ledger(client: GitHubIssueComments) -> int | None:
    """Find the single open, locked protocol ledger without changing GitHub state.

    Every bootstrapped ledger is labelled `LEDGER_LABEL` (`_ensure_ledger_labels`
    attaches it, and `reconcile` backfills it onto an older, unlabelled ledger —
    see `protocol.reconcile_all_labels`), and only the canonical ledger ever
    carries it. Asking GitHub for that exact label is genuinely atomic under
    normal operation: the answer is at most one issue, always one response,
    one snapshot — never a fetch spanning multiple page requests that a
    concurrent open/close could shift an issue across.

    Only when the labelled query comes back empty — an unlabelled legacy
    ledger, or a genuine absence — does discovery fall back to scanning every
    open issue, which usually still fits one page today but is not provably
    atomic once it does not. The open-issue-count comparison below only
    detects a fetch that a mid-scan open/close made incomplete; it cannot
    prove completeness (an issue that closed on an already-read page and
    reopened, or shifted within the same fetch, could still leave the count
    unchanged while an issue moved across a page boundary). Either way this
    must fail loud rather than report "no ledger" — reporting that wrongly
    invites `bootstrap`, which would create a second, competing ledger next
    to one that still exists.
    """
    labelled = _select_ledger(_ledger_issue_rows(client, state="open", label=LEDGER_LABEL))
    if labelled is not None:
        return labelled
    rows = _ledger_issue_rows(client, state="open")
    ledger = _select_ledger(rows)
    if ledger is not None:
        return ledger
    if len(rows) != _open_issue_count(client):
        raise ClaimError(
            "ledger discovery fetch may be incomplete (the open-issue count "
            "changed mid-fetch); retry rather than bootstrap"
        )
    return None


def _ensure_ledger_labels(client: GitHubIssueComments, ledger: int) -> None:
    """Create both label definitions and attach `LEDGER_LABEL` to `ledger` itself.

    `claim_label(ledger)` is never attached here — it belongs on whichever
    other issues carry an active claim rooted in this ledger, applied by
    `protocol.reconcile_issue_label`; this only needs the definition to exist
    before that first attach.
    """
    for label, description in (
        (LEDGER_LABEL, "agent-claim canonical ledger"),
        (claim_label(ledger), "agent-claim active issue projection"),
    ):
        client._run(
            [
                "label",
                "create",
                label,
                "--repo",
                client.repository,
                "--color",
                "6f42c1",
                "--description",
                description,
                "--force",
            ]
        )
    client.add_label(ledger, LEDGER_LABEL)


def _create_ledger(client: GitHubIssueComments) -> int:
    body = (
        f"{LEDGER_BODY_MARKER}\n\n## Agent claim ledger\n\n"
        "This open, collaborator-locked issue serializes build-claim events."
    )
    raw = client._run(
        ["api", "--method", "POST", f"repos/{client.repository}/issues", "--input", "-"],
        input_data=json.dumps({"title": "Agent claim ledger", "body": body}).encode("utf-8"),
    )
    try:
        created = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ClaimError("GitHub returned invalid created-ledger JSON") from error
    if (
        not isinstance(created, dict)
        or isinstance(created.get("number"), bool)
        or not isinstance(created.get("number"), int)
        or created["number"] < 1
    ):
        raise ClaimError("GitHub did not return a created ledger number")
    number = created["number"]
    client._run(["api", "--method", "PUT", f"repos/{client.repository}/issues/{number}/lock"])
    return number


def bootstrap_ledger(client: GitHubIssueComments) -> int:
    """Create/adopt one ledger and make racing first starts converge to the earliest issue."""
    rows = _ledger_issue_rows(client)
    foreign = [
        issue.number
        for issue in rows
        if not issue.is_pull_request
        and _trusted_ledger_issue(issue)
        and issue.state == "open"
        and _foreign_contract(issue)
    ]
    if foreign:
        raise ClaimError(
            f"another coordination contract exists on issue(s) {foreign}; refusing to compete"
        )
    candidates: list[_LedgerIssue] = []
    for issue in rows:
        if (
            issue.is_pull_request
            or issue.state != "open"
            or _issue_first_line(issue) != LEDGER_BODY_MARKER
            or not _trusted_ledger_issue(issue)
        ):
            continue
        if issue.locked or issue.author_association in TRUSTED_ASSOCIATIONS:
            candidates.append(issue)
    if not candidates:
        _create_ledger(client)
    rows = _ledger_issue_rows(client)
    candidates = [
        issue
        for issue in rows
        if not issue.is_pull_request
        and issue.state == "open"
        and _issue_first_line(issue) == LEDGER_BODY_MARKER
        and _trusted_ledger_issue(issue)
        and (issue.locked or issue.author_association in TRUSTED_ASSOCIATIONS)
    ]
    if not candidates:
        raise ClaimError("bootstrap did not expose a trusted ledger candidate; retry")
    canonical = min(issue.number for issue in candidates)
    for issue in candidates:
        if not issue.locked:
            client._run(
                ["api", "--method", "PUT", f"repos/{client.repository}/issues/{issue.number}/lock"]
            )
    _ensure_ledger_labels(client, canonical)
    for issue in candidates:
        if issue.number == canonical:
            continue
        client.post_comment(
            issue.number,
            "<!-- agent-claim-ledger-duplicate:v1 "
            f"canonical={canonical}{MARKER_SUFFIX}\n\n"
            f"Superseded duplicate ledger; canonical ledger is #{canonical}.",
        )
        client._run(["issue", "close", str(issue.number), "--repo", client.repository])
    return canonical
