"""The forge port: repository identity, typed failures, and the read/write surface.

`ForgeReader`/`ForgeWriter` are the provider-neutral contract every adapter
(today: GitHub) implements; `ForgeOperation` names every operation on that
contract and `Capability` answers, per operation, whether an adapter can
perform it at all. Nothing in this module or its callers branches on that
answer yet -- the GitHub adapter never refuses an operation -- so the first
real consumer is #112 (item kind as the native issue type; decision record
0001 ruling D3 maps kind read-only on GitLab Free, which cannot create a
custom `Container` type).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from . import board, protocol
from .protocol import ClaimError


class ForgeError(ClaimError):
    """An unclassified forge failure."""


class ForgeUnsupportedError(ForgeError):
    """The forge cannot perform this operation at all."""


class ForgePermissionDeniedError(ForgeError):
    """The forge refused the operation as an authorization failure."""


class ForgeNotFoundError(ForgeError):
    """The forge reports that the named subject does not exist."""


class ForgeTransientError(ForgeError):
    """The forge failed in a way a retry might not."""


class ForgeMalformedResponseError(ForgeError):
    """The forge's response could not be parsed into the expected shape."""


@dataclass(frozen=True)
class RepositoryId:
    """A repository's identity: the port owns this shape, an adapter owns its syntax."""

    host: str
    namespace: tuple[str, ...]
    name: str

    @property
    def path(self) -> str:
        return "/".join((*self.namespace, self.name))

    def __str__(self) -> str:
        return self.path


class ItemState(StrEnum):
    """A referenced work item's state, as seen from one repository."""

    OPEN = "open"
    CLOSED = "closed"
    MISSING = "missing"


@dataclass(frozen=True)
class ItemReference:
    state: ItemState
    title: str | None = None
    body: str | None = None


@dataclass(frozen=True)
class Landing:
    """One pull/merge request read for its own sake, not for the board's stages."""

    number: int
    author: str
    body: str
    source_repository: RepositoryId
    source_branch: str
    target_branch: str
    merged: bool


@dataclass(frozen=True)
class LedgerItem:
    """One issue read as ledger-discovery material -- an existing-ledger
    candidate or the row a foreign coordination contract is detected on."""

    number: int
    state: ItemState
    locked: bool
    body: str
    author_is_trusted: bool
    is_landing: bool


@dataclass(frozen=True)
class Listing:
    """A `list_items` result, plus the provenance discovery needs to judge it.

    `pages_fetched` is the number of paginated requests the adapter actually
    made to read `items` -- counted in its own paging loop, never inferred
    from `len(items)` against a duplicated per-page constant (a result sized
    at an exact multiple of the real page size would otherwise lie: a full
    page is never assumed to be the last one, so confirming absence costs one
    more request). `pages_fetched > 1` means the fetch spanned more than one
    round trip and can never be trusted to prove absence -- a concurrent
    open/close could have shifted an item across the page boundary.
    """

    items: tuple[LedgerItem, ...]
    pages_fetched: int


class Capability(StrEnum):
    UNSUPPORTED = "unsupported"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class ForgeOperation(StrEnum):
    """Every port operation; each member's value is its Protocol method name."""

    LIST_PROTOCOL_CANDIDATES = "list_protocol_candidates"
    LIST_CLAIMED_ISSUES = "list_claimed_issues"
    VALIDATE_SUCCESSOR = "validate_successor"
    POST_COMMENT = "post_comment"
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    UPSERT_PROJECTION = "upsert_projection"
    NEUTRALIZE_CLAIM_COMMENT = "neutralize_claim_comment"
    ITEM_REFERENCE = "item_reference"
    LANDING = "landing"
    PARENT_ISSUE = "parent_issue"
    OPEN_CHILDREN = "open_children"
    DEFAULT_BRANCH = "default_branch"
    LIST_OPEN_BOARD_ISSUES = "list_open_board_issues"
    LIST_BOARD_BLOCKERS = "list_board_blockers"
    LIST_OPEN_BOARD_PULL_REQUESTS = "list_open_board_pull_requests"
    LIST_RECENT_MERGED_BOARD_PULL_REQUESTS = "list_recent_merged_board_pull_requests"
    LIST_ITEMS = "list_items"
    OPEN_ITEM_COUNT = "open_item_count"
    ENSURE_LABEL = "ensure_label"
    CREATE_ITEM = "create_item"
    LOCK_ITEM = "lock_item"
    CLOSE_ITEM = "close_item"


class ForgeReader(protocol.ClaimReader, Protocol):
    @property
    def repository(self) -> RepositoryId: ...

    def capability(self, operation: ForgeOperation) -> Capability: ...

    def item_reference(self, number: int) -> ItemReference: ...

    def landing(self, number: int) -> Landing: ...

    def parent_issue(self, number: int) -> board.ParentIssue | None: ...

    def open_children(self, number: int) -> tuple[board.IssueReference, ...]: ...

    def default_branch(self) -> str: ...

    def list_open_board_issues(self) -> tuple[board.Issue, ...]: ...

    def list_board_blockers(
        self, numbers: frozenset[int]
    ) -> tuple[board.BlockerReference, ...]: ...

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]: ...

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]: ...

    def list_items(
        self, *, state: ItemState | None = None, label: str | None = None
    ) -> Listing: ...

    def open_item_count(self) -> int: ...


class ForgeWriter(ForgeReader, protocol.ClaimWriter, Protocol):
    """`ForgeReader` plus every operation that mutates forge state."""

    def ensure_label(self, name: str, *, colour: str, description: str) -> None: ...

    def create_item(self, *, title: str, body: str) -> int: ...

    def lock_item(self, number: int) -> None: ...

    def close_item(self, number: int) -> None: ...
