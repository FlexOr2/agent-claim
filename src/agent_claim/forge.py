"""The forge port: repository identity, typed failures, and the read/write surface.

`ForgeReader`/`ForgeWriter` are the provider-neutral contract every adapter
(today: GitHub) implements; `ForgeOperation` names every operation on that
contract and `Capability` answers, per operation, whether an adapter can
perform it at all. Nothing in this module or its callers branches on that
answer yet -- the GitHub adapter never refuses an operation -- so the first
real consumer is the GitLab adapter (decision record 0001, criterion D3).
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


class Capability(StrEnum):
    UNSUPPORTED = "unsupported"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class ForgeOperation(StrEnum):
    """Every port operation; each member's value is its Protocol method name."""

    # -- inherited from protocol.ClaimReader (3) ---------------------------
    LIST_PROTOCOL_CANDIDATES = "list_protocol_candidates"
    LIST_CLAIMED_ISSUES = "list_claimed_issues"
    VALIDATE_SUCCESSOR = "validate_successor"
    # -- inherited from protocol.ClaimWriter (5) ---------------------------
    POST_COMMENT = "post_comment"
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    UPSERT_PROJECTION = "upsert_projection"
    NEUTRALIZE_CLAIM_COMMENT = "neutralize_claim_comment"
    # -- forge-specific (9) --------------------------------------------------
    ITEM_REFERENCE = "item_reference"
    LANDING = "landing"
    PARENT_ISSUE = "parent_issue"
    OPEN_CHILDREN = "open_children"
    DEFAULT_BRANCH = "default_branch"
    LIST_OPEN_BOARD_ISSUES = "list_open_board_issues"
    LIST_BOARD_BLOCKERS = "list_board_blockers"
    LIST_OPEN_BOARD_PULL_REQUESTS = "list_open_board_pull_requests"
    LIST_RECENT_MERGED_BOARD_PULL_REQUESTS = "list_recent_merged_board_pull_requests"


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


class ForgeWriter(ForgeReader, protocol.ClaimWriter, Protocol):
    """`ForgeReader` plus every operation that mutates forge state."""
