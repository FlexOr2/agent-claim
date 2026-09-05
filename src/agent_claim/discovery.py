"""Discover, adopt, and create the canonical claim ledger issue."""

from __future__ import annotations

from . import forge
from .protocol import (
    LEDGER_BODY_MARKER,
    LEDGER_LABEL,
    MARKER_SUFFIX,
    TRUSTED_ASSOCIATIONS,
    ClaimError,
    ClaimUnavailableError,
    claim_label,
)

# GitHub's issues-list pagination fills every page but the last, so a result
# strictly under this count could only have come from one request — one live
# snapshot a concurrent open/close cannot have shifted an issue across.
ISSUES_PER_PAGE = 100
LEDGER_LABEL_COLOUR = "6f42c1"


def _issue_first_line(item: forge.LedgerItem) -> str:
    return item.body.partition("\n")[0]


def _foreign_contract(item: forge.LedgerItem) -> bool:
    first = _issue_first_line(item)
    if first == LEDGER_BODY_MARKER:
        return False
    return first.startswith("<!-- ") and ("claim" in first or "ledger" in first)


def _trusted_ledger_issue(item: forge.LedgerItem) -> bool:
    return item.author_association in TRUSTED_ASSOCIATIONS


def _select_ledger(items: tuple[forge.LedgerItem, ...]) -> int | None:
    """Resolve the canonical ledger from an issue-row snapshot, or raise on
    a locked-marker violation or a competing foreign coordination contract."""
    ledgers: list[int] = []
    foreign: list[int] = []
    for item in items:
        if item.is_landing:
            continue
        if not _trusted_ledger_issue(item):
            continue
        if item.state is forge.ItemState.OPEN and _foreign_contract(item):
            foreign.append(item.number)
            continue
        if _issue_first_line(item) != LEDGER_BODY_MARKER or item.state is forge.ItemState.CLOSED:
            continue
        if not item.locked:
            raise ClaimUnavailableError(
                f"ledger candidate #{item.number} is not locked; run bootstrap"
            )
        ledgers.append(item.number)
    if foreign:
        raise ClaimError(
            f"another coordination contract exists on issue(s) {foreign}; refusing to compete"
        )
    return min(ledgers) if ledgers else None


def discover_ledger(client: forge.ForgeReader) -> int | None:
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
    open issue. That scan can only report absence (return `None`) when it
    was a single page: `len(items) < ISSUES_PER_PAGE` is the only condition
    under which the fetch was provably one snapshot, since GitHub's
    pagination fills every page but the last. A fallback spanning more than
    one page can never prove absence, no matter how the counts line up — an
    issue that closed on an already-consumed page while another opened could
    leave `len(items)` exactly equal to the live open-issue count while still
    hiding an unlabelled legacy ledger that shifted across the page
    boundary — so that case always fails loud instead. Within a genuinely
    single-page fetch, the open-issue-count comparison is still worth
    keeping as an additional detector: it cannot involve a page-boundary
    shift, but the count itself comes from a separate request that could
    still have raced an open or close between the two calls. Whichever
    check trips, this must fail loud rather than report "no ledger" —
    reporting that wrongly invites `bootstrap`, which would create a
    second, competing ledger next to one that still exists.
    """
    labelled = _select_ledger(client.list_items(state=forge.ItemState.OPEN, label=LEDGER_LABEL))
    if labelled is not None:
        return labelled
    items = client.list_items(state=forge.ItemState.OPEN)
    ledger = _select_ledger(items)
    if ledger is not None:
        return ledger
    if len(items) >= ISSUES_PER_PAGE:
        page_count = (len(items) + ISSUES_PER_PAGE - 1) // ISSUES_PER_PAGE
        raise ClaimError(
            f"could not establish ledger absence over {page_count} pages of "
            "open issues; retry, do not bootstrap"
        )
    if len(items) != client.open_item_count():
        raise ClaimError(
            "ledger discovery fetch may be incomplete (the open-issue count "
            "changed mid-fetch); retry rather than bootstrap"
        )
    return None


def _ensure_ledger_labels(client: forge.ForgeWriter, ledger: int) -> None:
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
        client.ensure_label(label, colour=LEDGER_LABEL_COLOUR, description=description)
    client.add_label(ledger, LEDGER_LABEL)


def _create_ledger(client: forge.ForgeWriter) -> int:
    body = (
        f"{LEDGER_BODY_MARKER}\n\n## Agent claim ledger\n\n"
        "This open, collaborator-locked issue serializes build-claim events."
    )
    number = client.create_item(title="Agent claim ledger", body=body)
    client.lock_item(number)
    return number


def _refuse_competing_contracts(items: tuple[forge.LedgerItem, ...]) -> None:
    foreign = [
        item.number
        for item in items
        if not item.is_landing
        and _trusted_ledger_issue(item)
        and item.state is forge.ItemState.OPEN
        and _foreign_contract(item)
    ]
    if foreign:
        raise ClaimError(
            f"another coordination contract exists on issue(s) {foreign}; refusing to compete"
        )


def _trusted_ledger_candidates(items: tuple[forge.LedgerItem, ...]) -> tuple[forge.LedgerItem, ...]:
    return tuple(
        item
        for item in items
        if not item.is_landing
        and item.state is forge.ItemState.OPEN
        and _issue_first_line(item) == LEDGER_BODY_MARKER
        and _trusted_ledger_issue(item)
        and (item.locked or item.author_association in TRUSTED_ASSOCIATIONS)
    )


def _converge_on_canonical_ledger(
    client: forge.ForgeWriter, candidates: tuple[forge.LedgerItem, ...]
) -> int:
    canonical = min(item.number for item in candidates)
    for item in candidates:
        if not item.locked:
            client.lock_item(item.number)
    _ensure_ledger_labels(client, canonical)
    for item in candidates:
        if item.number == canonical:
            continue
        client.post_comment(
            item.number,
            "<!-- agent-claim-ledger-duplicate:v1 "
            f"canonical={canonical}{MARKER_SUFFIX}\n\n"
            f"Superseded duplicate ledger; canonical ledger is #{canonical}.",
        )
        client.close_item(item.number)
    return canonical


def bootstrap_ledger(client: forge.ForgeWriter) -> int:
    """Create/adopt one ledger and make racing first starts converge to the earliest issue."""
    items = client.list_items()
    _refuse_competing_contracts(items)
    if not _trusted_ledger_candidates(items):
        _create_ledger(client)
    candidates = _trusted_ledger_candidates(client.list_items())
    if not candidates:
        raise ClaimError("bootstrap did not expose a trusted ledger candidate; retry")
    return _converge_on_canonical_ledger(client, candidates)
