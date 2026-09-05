"""GitHub adapter for the forge port."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import TypeVar

from . import board, forge, process, protocol
from .protocol import (
    MAX_PROTOCOL_BYTES,
    MAX_PROTOCOL_EVENTS,
    PROJECTION_MARKER_PATTERN,
    REPOSITORY_PATTERN,
    TRUSTED_ASSOCIATIONS,
    ClaimError,
    ClaimUnavailableError,
    IssueComment,
    _projection_ledger,
    _projection_marker,
    _validated_comment,
    claim_label,
    is_protocol_candidate,
)

_Page = TypeVar("_Page")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# gh 2.45 colorizes --jq output when it believes stdout is a TTY.
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
COMMENTS_PER_PAGE = 100
# `_projection_comments` still fetches one page per `gh` subprocess call and
# can stop as soon as a short page ends, so these genuinely bound how much it
# fetches before giving up and asking for a ledger rollover.
MAX_LEDGER_PAGES = 100
LEDGER_ROLLOVER_WARNING_PAGES = 80
# `list_protocol_candidates` fetches every comment page before it can inspect
# anything, so by the time either of these is checked the full cost has
# already been paid; they bound how much is held and processed afterward
# (and when to ask for a rollover), not the fetch cost itself.
MAX_LEDGER_COMMENTS = MAX_LEDGER_PAGES * COMMENTS_PER_PAGE
LEDGER_ROLLOVER_WARNING_COMMENTS = LEDGER_ROLLOVER_WARNING_PAGES * COMMENTS_PER_PAGE
MAX_RECENT_MERGED_PULL_REQUESTS = 1000
# GitHub's issue-comments listing is offset-paginated (a page past the last
# one comes back empty rather than erroring) and its merged-pull-request
# search accepts an exact-day filter, so both a ledger's comment pages and a
# board's merged-pull-request date shards are independent, order-agnostic
# fetches. Walking them one `gh` subprocess at a time made a growing ledger
# the dominant cost of `status`/`board`/`next`/`claim` (measured ~7-8s for an
# ~18-page ledger; ~0.8s fetched in parallel batches). This bounds how many
# `gh` subprocesses run at once, comfortably under GitHub's secondary rate
# limit for concurrent requests.
PARALLEL_FETCH_CONCURRENCY = 20
GH_TIMEOUT_SECONDS = 60
GH_QUIET_ENVIRONMENT = {
    "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}
API_ISSUE_STATES: dict[str, board.BlockerState] = {
    "open": board.BlockerState.OPEN,
    "closed": board.BlockerState.CLOSED,
}
_LEDGER_ITEM_STATES: dict[str, forge.ItemState] = {
    "open": forge.ItemState.OPEN,
    "closed": forge.ItemState.CLOSED,
}
# GitHub's issues-list pagination fills every page but the last, so a result
# strictly under this count could only have come from one request -- one live
# snapshot a concurrent open/close cannot have shifted an issue across.
ISSUES_PER_PAGE = 100
MALFORMED_PULL_REQUEST = "GitHub returned a malformed pull request"
# `HTTP 5xx` in #4.2's signal table: gh's combined output names the status
# code but never its class, so any 5xx is matched by digit rather than by an
# enumerated list of codes that would need to grow with the API.
_HTTP_SERVER_ERROR_PATTERN = re.compile(r"HTTP 5\d\d")
GITHUB_HOST = "github.com"
# Accepts both pinned remote forms, the SCP one included.
GITHUB_REMOTE_PATTERN = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def github_command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(GH_QUIET_ENVIRONMENT)
    return environment


def _repository_id(text: str) -> forge.RepositoryId:
    if re.fullmatch(REPOSITORY_PATTERN, text) is None:
        raise ClaimError("repository must be OWNER/REPO")
    namespace, _, name = text.partition("/")
    return forge.RepositoryId(GITHUB_HOST, (namespace,), name)


def discover_repository(
    explicit: str | None, *, remote_url: Callable[[], str]
) -> forge.RepositoryId:
    """Resolve the repository `--repo` did not name.

    Reads `gh repo view`'s stdout alone -- a separate-stream result, so a
    stderr warning can neither corrupt a good answer nor suppress the
    fall-back to the git remote below.
    """
    if explicit:
        return _repository_id(explicit)
    try:
        result = process.run_captured(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            env=github_command_environment(),
            timeout=GH_TIMEOUT_SECONDS,
        )
    except process.ExecutableMissingError:
        raise ClaimError("gh is required for issue claims") from None
    except process.ProcessTimedOutError:
        raise ClaimError("gh timed out while resolving the repository") from None
    cleaned = strip_ansi(result.stdout.decode("utf-8")).strip()
    if result.exit_status == 0 and cleaned:
        return _repository_id(cleaned)
    match = GITHUB_REMOTE_PATTERN.search(remote_url())
    if match is None:
        raise ClaimError("cannot resolve GitHub repository; pass --repo OWNER/REPO")
    return _repository_id(f"{match.group(1)}/{match.group(2)}")


def _head_repository(pull_request: dict[str, object]) -> forge.RepositoryId | None:
    """The identity of the repository whose branch a pull request proposes,
    or None when GitHub does not name both halves — a fork deleted after the
    pull request opened, say.
    """
    repository = pull_request.get("headRepository")
    owner = pull_request.get("headRepositoryOwner")
    name = repository.get("name") if isinstance(repository, dict) else None
    login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not isinstance(login, str):
        return None
    if re.fullmatch(REPOSITORY_PATTERN, f"{login}/{name}") is None:
        return None
    return forge.RepositoryId(GITHUB_HOST, (login,), name)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _query_days(start: date, end: date) -> tuple[date, ...]:
    """One calendar UTC day per merged-pull-request query shard, `start` through `end` inclusive."""
    if end < start:
        raise ClaimError("merged pull request window ends before it starts")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _decoded(result: process.BoundedResult, purpose: str) -> str:
    try:
        return strip_ansi(result.output.decode("utf-8")).strip()
    except UnicodeDecodeError as error:
        raise forge.ForgeMalformedResponseError(f"{purpose} returned non-UTF-8 output") from error


_ProcessFailureBuilder = Callable[[process.ProcessError, str], forge.ForgeError]
_PROCESS_FAILURE_BUILDERS: dict[type[process.ProcessError], _ProcessFailureBuilder] = {
    process.ProcessTimedOutError: (
        lambda error, purpose: forge.ForgeTransientError(f"{purpose} timed out")
    ),
    process.ProcessIoFailedError: (
        lambda error, purpose: forge.ForgeTransientError(
            f"{purpose} failed while {error.stage.value}: {error.detail}"
        )
    ),
    process.ProcessDidNotExitError: (
        lambda error, purpose: forge.ForgeTransientError(
            f"{purpose} did not exit after closing its output"
        )
    ),
    process.ProcessOutputTooLargeError: (
        lambda error, purpose: forge.ForgeMalformedResponseError(
            f"{purpose} exceeded its output limit"
        )
    ),
}


def _forge_failure(error: process.ProcessError, purpose: str) -> forge.ForgeError:
    """Translate a process failure that reached no forge response into a typed one."""
    return _PROCESS_FAILURE_BUILDERS[type(error)](error, purpose)


def _is_transient_signal(decoded: str) -> bool:
    return (
        _HTTP_SERVER_ERROR_PATTERN.search(decoded) is not None
        or "connection reset" in decoded
        or "timeout" in decoded
    )


def _nonzero_exit_failure(decoded: str, return_code: int, purpose: str) -> forge.ForgeError:
    """Classify a nonzero `gh` exit from its decoded combined output (#4.2).

    `gh`'s own exit code never carries the HTTP status, so this reads the
    same prose a human would; the fallback stays an unclassified `ForgeError`
    rather than guessing at retry safety.
    """
    if "HTTP 404" in decoded:
        return forge.ForgeNotFoundError(decoded)
    if "HTTP 401" in decoded or "HTTP 403" in decoded:
        return forge.ForgePermissionDeniedError(decoded)
    if _is_transient_signal(decoded):
        return forge.ForgeTransientError(decoded)
    return forge.ForgeError(decoded or f"{purpose} failed with exit {return_code}")


def _bounded_command(command: list[str], *, purpose: str, input_data: bytes | None = None) -> str:
    try:
        result = process.run_bounded(
            command,
            input_data=input_data,
            env=github_command_environment(),
            timeout=GH_TIMEOUT_SECONDS,
        )
    except process.ExecutableMissingError as error:
        raise ClaimError(f"{error.executable} is required for issue claims") from error
    except process.ProcessStartFailedError as error:
        raise ClaimError(f"cannot start {purpose}: {error.detail}") from error
    except process.ProcessError as error:
        raise _forge_failure(error, purpose) from error
    decoded = _decoded(result, purpose)
    if result.exit_status != 0:
        raise _nonzero_exit_failure(decoded, result.exit_status, purpose)
    return decoded


_READ_ONLY_OPERATIONS = (
    forge.ForgeOperation.LIST_PROTOCOL_CANDIDATES,
    forge.ForgeOperation.LIST_CLAIMED_ISSUES,
    forge.ForgeOperation.VALIDATE_SUCCESSOR,
    forge.ForgeOperation.ITEM_REFERENCE,
    forge.ForgeOperation.LANDING,
    forge.ForgeOperation.PARENT_ISSUE,
    forge.ForgeOperation.OPEN_CHILDREN,
    forge.ForgeOperation.DEFAULT_BRANCH,
    forge.ForgeOperation.LIST_OPEN_BOARD_ISSUES,
    forge.ForgeOperation.LIST_BOARD_BLOCKERS,
    forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS,
    forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS,
    forge.ForgeOperation.LIST_ITEMS,
    forge.ForgeOperation.OPEN_ITEM_COUNT,
)
_READ_WRITE_OPERATIONS = (
    forge.ForgeOperation.POST_COMMENT,
    forge.ForgeOperation.ADD_LABEL,
    forge.ForgeOperation.REMOVE_LABEL,
    forge.ForgeOperation.UPSERT_PROJECTION,
    forge.ForgeOperation.NEUTRALIZE_CLAIM_COMMENT,
    forge.ForgeOperation.ENSURE_LABEL,
    forge.ForgeOperation.CREATE_ITEM,
    forge.ForgeOperation.LOCK_ITEM,
    forge.ForgeOperation.CLOSE_ITEM,
)
# The GitHub adapter never refuses an operation: every member answers
# READ_ONLY or READ_WRITE, never UNSUPPORTED (decision record 0001 §2).
GITHUB_CAPABILITIES: Mapping[forge.ForgeOperation, forge.Capability] = MappingProxyType(
    {
        **dict.fromkeys(_READ_ONLY_OPERATIONS, forge.Capability.READ_ONLY),
        **dict.fromkeys(_READ_WRITE_OPERATIONS, forge.Capability.READ_WRITE),
    }
)


class GitHubForge:
    def __init__(
        self,
        repository: forge.RepositoryId,
        *,
        run: Callable[..., str] | None = None,
    ) -> None:
        self.repository = repository
        self._run = run if run is not None else self._gh
        self._rollover_warning_printed = False

    def _gh(self, arguments: list[str], *, input_data: bytes | None = None) -> str:
        return _bounded_command(
            ["gh", *arguments],
            purpose="GitHub issue coordination",
            input_data=input_data,
        )

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return GITHUB_CAPABILITIES[operation]

    def item_reference(self, number: int) -> forge.ItemReference:
        try:
            raw = self._run(
                ["api", f"repos/{self.repository}/issues/{number}", "--jq", "{state,title,body}"]
            )
        except forge.ForgeNotFoundError:
            return forge.ItemReference(forge.ItemState.MISSING)
        values = self._json_lines(raw, "issue reference")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue reference")
        value = values[0]
        state = value.get("state")
        title = value.get("title")
        body = value.get("body")
        if (
            state not in {"open", "closed"}
            or not isinstance(title, str)
            or (body is not None and not isinstance(body, str))
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue reference")
        return forge.ItemReference(
            forge.ItemState.OPEN if state == "open" else forge.ItemState.CLOSED,
            title,
            body or "",
        )

    def _json_lines(self, raw: str, description: str) -> tuple[object, ...]:
        """Parse compact NDJSON, pretty JSON, or a concatenated JSON sequence."""
        text = strip_ansi(raw).strip()
        if not text:
            return ()
        decoder = json.JSONDecoder()
        values: list[object] = []
        offset = 0
        length = len(text)
        try:
            while offset < length:
                while offset < length and text[offset].isspace():
                    offset += 1
                if offset >= length:
                    break
                value, offset = decoder.raw_decode(text, offset)
                values.append(value)
        except json.JSONDecodeError as error:
            raise forge.ForgeMalformedResponseError(
                f"GitHub returned invalid {description} JSON"
            ) from error
        return tuple(values)

    def _comment_page(self, issue: int, page: int) -> tuple[IssueComment, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/issues/{issue}/comments"
                f"?per_page={COMMENTS_PER_PAGE}&page={page}",
                "--jq",
                ".[] | {id,created_at,updated_at,body,author_association,html_url}",
            ]
        )
        return tuple(self._parse_comment(value) for value in self._json_lines(raw, "issue-comment"))

    def _fetch_pages(
        self, page: Callable[[int], tuple[_Page, ...]], *, per_page: int
    ) -> tuple[_Page, ...]:
        """Every page from `page` (1-indexed), the first fetched alone and the
        rest in concurrent batches of `PARALLEL_FETCH_CONCURRENCY`.

        A single-page listing (the common case for a small or fresh
        repository) costs exactly the one round trip it always did. A page
        past the last one returns an empty array rather than erroring, so
        once page 1 comes back full, a batch can ask for the next
        `PARALLEL_FETCH_CONCURRENCY` page numbers at once; the batch's last
        page coming back short of a full page is what ends the fetch, exactly
        as a single `gh api --paginate` call would stop, just without waiting
        for each page's round trip in turn.
        """
        first_page = page(1)
        if len(first_page) < per_page:
            return first_page
        pages: list[_Page] = list(first_page)
        start = 2
        while True:
            batch = range(start, start + PARALLEL_FETCH_CONCURRENCY)
            with ThreadPoolExecutor(max_workers=PARALLEL_FETCH_CONCURRENCY) as pool:
                fetched = list(pool.map(page, batch))
            for page_values in fetched:
                pages.extend(page_values)
            if len(fetched[-1]) < per_page:
                return tuple(pages)
            start += PARALLEL_FETCH_CONCURRENCY

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        all_comments = self._fetch_pages(
            lambda page: self._comment_page(issue, page), per_page=COMMENTS_PER_PAGE
        )
        total_comments = len(all_comments)
        if total_comments > MAX_LEDGER_COMMENTS:
            raise ClaimError(
                "claim ledger page limit reached; perform the documented ledger rollover"
            )
        if (
            total_comments >= LEDGER_ROLLOVER_WARNING_COMMENTS
            and not self._rollover_warning_printed
        ):
            print(
                f"WARNING: claim ledger has {total_comments} comments; "
                "schedule the documented rollover",
                file=sys.stderr,
            )
            self._rollover_warning_printed = True
        comments: list[IssueComment] = []
        protocol_bytes = 0
        for parsed in all_comments:
            if not is_protocol_candidate(parsed):
                continue
            protocol_bytes += len(parsed.body.encode("utf-8"))
            if len(comments) >= MAX_PROTOCOL_EVENTS or protocol_bytes > MAX_PROTOCOL_BYTES:
                raise ClaimError(
                    "claim ledger protocol limit reached; perform the documented ledger rollover"
                )
            comments.append(parsed)
        return tuple(comments)

    def _projection_comments(self, issue: int) -> tuple[IssueComment, ...]:
        projections: list[IssueComment] = []
        for page in range(1, MAX_LEDGER_PAGES + 1):
            page_comments = self._comment_page(issue, page)
            projections.extend(
                comment
                for comment in page_comments
                if comment.author_association in TRUSTED_ASSOCIATIONS
                and PROJECTION_MARKER_PATTERN.fullmatch(comment.body.partition("\n")[0]) is not None
            )
            if len(page_comments) < COMMENTS_PER_PAGE:
                return tuple(projections)
        raise ClaimError("owning issue comment limit reached during projection update")

    def _parse_comment(self, value: object) -> IssueComment:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError("GitHub issue-comment entry must be an object")
        identifier = value.get("id")
        created_at = value.get("created_at")
        updated_at = value.get("updated_at")
        body = value.get("body")
        association = value.get("author_association")
        url = value.get("html_url")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier < 1
            or not isinstance(created_at, str)
            or TIMESTAMP_PATTERN.fullmatch(created_at) is None
            or not isinstance(updated_at, str)
            or TIMESTAMP_PATTERN.fullmatch(updated_at) is None
            or not isinstance(body, str)
            or not isinstance(association, str)
            or not isinstance(url, str)
            or not url.startswith("https://github.com/")
        ):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed issue-comment entry"
            )
        return IssueComment(identifier, created_at, updated_at, body, association, url)

    def _board_issue(self, value: object) -> board.Issue:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed board issue")
        number = value.get("number")
        title = value.get("title")
        labels = value.get("labels")
        body = value.get("body")
        created_at = value.get("createdAt")
        updated_at = value.get("updatedAt")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(title, str)
            or not isinstance(labels, list)
            or not all(isinstance(label, str) for label in labels)
            or not isinstance(body, str)
            or not isinstance(created_at, str)
            or TIMESTAMP_PATTERN.fullmatch(created_at) is None
            or not isinstance(updated_at, str)
            or TIMESTAMP_PATTERN.fullmatch(updated_at) is None
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed board issue")
        return board.Issue(number, title, tuple(labels), body, created_at, updated_at)

    def _board_pull_request(self, value: object) -> board.PullRequest:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed board pull request"
            )
        number = value.get("number")
        title = value.get("title")
        body = value.get("body")
        if body is None:
            body = ""
        head_ref_name = value.get("headRefName")
        merged_at = value.get("mergedAt")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(title, str)
            or not isinstance(body, str)
            or not isinstance(head_ref_name, str)
            or (merged_at is not None and not isinstance(merged_at, str))
            or (isinstance(merged_at, str) and TIMESTAMP_PATTERN.fullmatch(merged_at) is None)
        ):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed board pull request"
            )
        return board.PullRequest(number, title, body, head_ref_name, merged_at)

    def _landing(self, value: object) -> forge.Landing:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        number = value.get("number")
        body = value.get("body")
        if body is None:
            body = ""
        base_ref_name = value.get("baseRefName")
        head_ref_name = value.get("headRefName")
        source_repository = _head_repository(value)
        author = value.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        merged_at = value.get("mergedAt")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(body, str)
            or not isinstance(base_ref_name, str)
            or not isinstance(head_ref_name, str)
            or source_repository is None
            or not isinstance(login, str)
            or not login
            or (merged_at is not None and not isinstance(merged_at, str))
            or (isinstance(merged_at, str) and TIMESTAMP_PATTERN.fullmatch(merged_at) is None)
        ):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        return forge.Landing(
            number,
            login,
            body,
            source_repository,
            head_ref_name,
            base_ref_name,
            merged_at is not None,
        )

    def landing(self, number: int) -> forge.Landing:
        raw = self._run(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repository.path,
                "--json",
                "number,body,baseRefName,headRefName,headRepository,"
                "headRepositoryOwner,author,mergedAt",
                "--jq",
                ".",
            ]
        )
        values = self._json_lines(raw, "pull request")
        if len(values) != 1:
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        landing = self._landing(values[0])
        if landing.number != number:
            raise ClaimError(f"GitHub answered for pull request #{landing.number}, not #{number}")
        return landing

    def _issue_reference(self, value: object, description: str) -> board.IssueReference:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        number = value.get("number")
        repository_url = value.get("repository")
        repository = (
            repository_url.rpartition("/repos/")[2] if isinstance(repository_url, str) else None
        )
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or repository is None
            or re.fullmatch(REPOSITORY_PATTERN, repository) is None
        ):
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        return board.IssueReference(repository, number)

    def _issue_state(self, value: object, description: str) -> board.BlockerState:
        state = value.get("state") if isinstance(value, dict) else None
        parsed = API_ISSUE_STATES.get(state) if isinstance(state, str) else None
        if parsed is None:
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        return parsed

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        """The issue GitHub records as `number`'s parent, or None when it has none."""
        try:
            raw = self._run(
                [
                    "api",
                    f"repos/{self.repository}/issues/{number}/parent",
                    "--jq",
                    '{number,repository:.repository_url,body:(.body // "")}',
                ]
            )
        except forge.ForgeNotFoundError:
            # The sub-issue endpoint answers "no parent" with an HTTP 404,
            # which the nonzero-exit classification (#4.2) reports as
            # `ForgeNotFoundError` -- that is an answer, not a failure.
            return None
        values = self._json_lines(raw, "parent issue")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed parent issue")
        body = values[0].get("body")
        if not isinstance(body, str):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed parent issue")
        return board.ParentIssue(self._issue_reference(values[0], "parent issue"), body)

    def open_children(self, number: int) -> tuple[board.IssueReference, ...]:
        """The parent's children GitHub still holds open.

        Every child's state is read here rather than filtered by `--jq`: a
        state this adapter does not understand would otherwise vanish and make
        a parent look childless, which is exactly the landing this check must
        refuse.
        """
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues/{number}/sub_issues?per_page=100",
                "--jq",
                ".[] | {number,repository:.repository_url,state}",
            ]
        )
        children: list[board.IssueReference] = []
        for value in self._json_lines(raw, "sub-issue"):
            reference = self._issue_reference(value, "sub-issue")
            if self._issue_state(value, "sub-issue") is board.BlockerState.OPEN:
                children.append(reference)
        return tuple(children)

    def default_branch(self) -> str:
        branch = self._run(["api", f"repos/{self.repository}", "--jq", ".default_branch"])
        if protocol.BRANCH_PATTERN.fullmatch(branch) is None:
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed default branch")
        return branch

    def _open_issue_page(self, page: int) -> tuple[object, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/issues"
                f"?state=open&per_page={COMMENTS_PER_PAGE}&page={page}",
                "--jq",
                (
                    # No `select` here (unlike the old single `--paginate` call):
                    # a page must report its true raw item count so a short page
                    # still correctly signals "no more pages" even when some of
                    # its items are pull requests, filtered out below instead.
                    '.[] | {number,title,labels:(.labels | map(.name)),body:(.body // ""),'
                    "createdAt:.created_at,updatedAt:.updated_at,"
                    'isPullRequest:has("pull_request")}'
                ),
            ]
        )
        return self._json_lines(raw, "board issue")

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        values = self._fetch_pages(self._open_issue_page, per_page=COMMENTS_PER_PAGE)
        return tuple(
            self._board_issue(value)
            for value in values
            if not (isinstance(value, dict) and value.get("isPullRequest"))
        )

    MALFORMED_BOARD_BLOCKER = "GitHub returned a malformed board blocker"

    def _board_blocker(self, number: int) -> board.BlockerReference:
        try:
            raw = self._run(
                [
                    "api",
                    f"repos/{self.repository}/issues/{number}",
                    "--jq",
                    '{number,state,closedAt:.closed_at,isPullRequest:has("pull_request")}',
                ]
            )
        except forge.ForgeNotFoundError:
            return board.BlockerReference(number, board.BlockerState.MISSING, False)
        values = self._json_lines(raw, "board blocker")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_BLOCKER)
        value = values[0]
        returned_number = value.get("number")
        state = value.get("state")
        closed_at = value.get("closedAt")
        is_pull_request = value.get("isPullRequest")
        blocker_state = API_ISSUE_STATES.get(state) if isinstance(state, str) else None
        if (
            isinstance(returned_number, bool)
            or returned_number != number
            or blocker_state is None
            or not isinstance(is_pull_request, bool)
            or (closed_at is not None and not isinstance(closed_at, str))
            or (isinstance(closed_at, str) and TIMESTAMP_PATTERN.fullmatch(closed_at) is None)
            or (blocker_state is board.BlockerState.CLOSED and closed_at is None)
        ):
            raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_BLOCKER)
        parsed_closed_at = None
        if closed_at is not None:
            try:
                parsed_closed_at = datetime.fromisoformat(closed_at)
            except ValueError as error:
                raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_BLOCKER) from error
            if parsed_closed_at.tzinfo is None:
                raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_BLOCKER)
            parsed_closed_at = parsed_closed_at.astimezone(UTC)
        return board.BlockerReference(
            number,
            blocker_state,
            is_pull_request,
            parsed_closed_at,
        )

    def list_board_blockers(self, numbers: frozenset[int]) -> tuple[board.BlockerReference, ...]:
        if not numbers:
            return ()
        with ThreadPoolExecutor(max_workers=min(len(numbers), PARALLEL_FETCH_CONCURRENCY)) as pool:
            return tuple(pool.map(self._board_blocker, sorted(numbers)))

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repository.path,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,body,headRefName",
                "--jq",
                ".[]",
            ]
        )
        return tuple(
            self._board_pull_request(value)
            for value in self._json_lines(raw, "open board pull request")
        )

    def _merged_pull_requests_for_day(self, day: date) -> tuple[board.PullRequest, ...]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repository.path,
                "--state",
                "merged",
                "--search",
                f"merged:{day.isoformat()}",
                "--limit",
                str(MAX_RECENT_MERGED_PULL_REQUESTS),
                "--json",
                "number,title,body,headRefName,mergedAt",
                "--jq",
                ".[]",
            ]
        )
        return tuple(
            self._board_pull_request(value)
            for value in self._json_lines(raw, "merged board pull request")
        )

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        cutoff = since.astimezone(UTC)
        days = _query_days(cutoff.date(), datetime.now(UTC).date())
        with ThreadPoolExecutor(max_workers=min(len(days), PARALLEL_FETCH_CONCURRENCY)) as pool:
            shards = list(pool.map(self._merged_pull_requests_for_day, days))
        # GitHub's search date qualifier is an exact UTC day, so slicing the
        # window this way turns one query that walks `since` to today through
        # GraphQL cursor pagination (measured ~4-9s for a three-week, ~630-PR
        # window) into independent single-page requests fetched in parallel
        # (~1-2s for the same window). A day whose own shard fills its limit
        # is now the only way a merged pull request can go missing (the old
        # single query's cap instead truncated the *whole* window), so that is
        # what the residual warning below watches for.
        saturated_days = tuple(
            day
            for day, shard in zip(days, shards, strict=True)
            if len(shard) >= MAX_RECENT_MERGED_PULL_REQUESTS
        )
        if saturated_days:
            print(
                "WARNING: merged pull request history is capped at "
                f"{MAX_RECENT_MERGED_PULL_REQUESTS} results for "
                f"{', '.join(day.isoformat() for day in saturated_days)}; "
                "an older landing that day could be missing from a board/next stage",
                file=sys.stderr,
            )
        recent: list[board.PullRequest] = []
        for pull_request in (pr for shard in shards for pr in shard):
            if pull_request.merged_at is None:
                continue
            try:
                merged_at = datetime.fromisoformat(pull_request.merged_at)
            except ValueError as error:
                raise forge.ForgeMalformedResponseError(
                    "GitHub returned a malformed merged board pull request"
                ) from error
            if merged_at >= cutoff:
                recent.append(pull_request)
        return tuple(recent)

    def list_claimed_issues(self) -> tuple[int, ...]:
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues?state=all&labels={claim_label()}&per_page=100",
                "--jq",
                '.[] | select(has("pull_request") | not) | .number',
            ]
        )
        issues: list[int] = []
        for value in self._json_lines(raw, "claimed-issue"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise forge.ForgeMalformedResponseError(
                    "GitHub returned a malformed claimed-issue entry"
                )
            issues.append(value)
        return tuple(issues)

    def validate_successor(self, issue: int) -> None:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/issues/{issue}",
                "--jq",
                '{number,state,locked,comments,is_pull_request:has("pull_request")}',
            ]
        )
        values = self._json_lines(raw, "successor-issue")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed successor issue")
        successor = values[0]
        number = successor.get("number")
        comments = successor.get("comments")
        if (
            isinstance(number, bool)
            or number != issue
            or successor.get("state") != "open"
            or successor.get("locked") is not True
            or isinstance(comments, bool)
            or comments != 0
            or successor.get("is_pull_request") is not False
        ):
            raise ClaimUnavailableError(
                f"successor #{issue} must be an open, empty, collaborator-locked issue"
            )

    def _patch_comment_body(self, comment_id: int, body: str) -> None:
        self._run(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/issues/comments/{comment_id}",
                "--input",
                "-",
            ],
            input_data=json.dumps({"body": body}).encode("utf-8"),
        )

    def upsert_projection(
        self,
        issue: int,
        body: str,
        *,
        create: bool = True,
        adopt_stale: bool = False,
    ) -> bool:
        validated = _validated_comment(body)
        all_projections = self._projection_comments(issue)
        current_marker = _projection_marker()
        projections = tuple(
            comment
            for comment in all_projections
            if comment.body.partition("\n")[0] == current_marker
        )
        adoptable_projections = tuple(
            comment
            for comment in all_projections
            if (_projection_ledger(comment) or 0) <= protocol.LEDGER_ISSUE
        )
        has_newer_projection = any(
            (_projection_ledger(comment) or 0) > protocol.LEDGER_ISSUE
            for comment in all_projections
        )
        if adopt_stale and adoptable_projections:
            projections = adoptable_projections
        if not projections:
            if has_newer_projection:
                raise ClaimError("owning issue has a projection from a newer ledger generation")
            if not create:
                return False
            self.post_comment(issue, validated)
            projections = tuple(
                comment
                for comment in self._projection_comments(issue)
                if comment.body.partition("\n")[0] == current_marker
            )
        if not projections:
            raise ClaimError(f"issue #{issue} did not expose its posted claim projection")
        ordered = sorted(
            projections,
            key=lambda comment: (comment.created_at, comment.identifier),
        )
        owner, *duplicates = ordered
        if owner.body != validated:
            self._patch_comment_body(owner.identifier, validated)
        for duplicate in duplicates:
            self._run(
                [
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{self.repository}/issues/comments/{duplicate.identifier}",
                ]
            )
        return True

    def neutralize_claim_comment(self, comment_id: int, body: str) -> None:
        self._patch_comment_body(comment_id, _validated_comment(body))

    def post_comment(self, issue: int, body: str) -> str:
        encoded = _validated_comment(body).encode("utf-8")
        return self._run(
            ["issue", "comment", str(issue), "--repo", self.repository.path, "--body-file", "-"],
            input_data=encoded,
        )

    def add_label(self, issue: int, label: str) -> None:
        self._run(
            ["issue", "edit", str(issue), "--repo", self.repository.path, "--add-label", label]
        )

    def remove_label(self, issue: int, label: str) -> None:
        self._run(
            ["issue", "edit", str(issue), "--repo", self.repository.path, "--remove-label", label]
        )

    def _ledger_item(self, value: object) -> forge.LedgerItem:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed ledger issue")
        number = value.get("number")
        author_association = value.get("author_association")
        raw_state = value.get("state")
        item_state = _LEDGER_ITEM_STATES.get(raw_state) if isinstance(raw_state, str) else None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or item_state is None
            or not isinstance(value.get("locked"), bool)
            or not isinstance(value.get("body"), str)
            or not isinstance(author_association, str)
            or not isinstance(value.get("is_landing"), bool)
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed ledger issue")
        return forge.LedgerItem(
            number,
            item_state,
            value["locked"],
            value["body"],
            author_association in TRUSTED_ASSOCIATIONS,
            value["is_landing"],
        )

    def _ledger_items_page(
        self, page_number: int, *, query_state: str, label_filter: str
    ) -> tuple[object, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/issues?state={query_state}{label_filter}"
                f"&per_page={ISSUES_PER_PAGE}&page={page_number}",
                "--jq",
                (
                    ".[] | {number,state,locked,body,author_association,"
                    'is_landing:has("pull_request")}'
                ),
            ]
        )
        return self._json_lines(raw, "ledger-issue")

    def list_items(
        self, *, state: forge.ItemState | None = None, label: str | None = None
    ) -> forge.Listing:
        """Every matching issue, plus the true count of pages this fetch took.

        Fetched one page at a time (never `--paginate`, which would hide that
        count inside `gh`) so `pages_fetched` is the fact it claims to be, not
        a guess derived from `len(items)` against the per-page size: a full
        page is never assumed to be the last one, so an exact multiple of
        `ISSUES_PER_PAGE` still costs the extra page that proves nothing
        follows.
        """
        query_state = "all" if state is None else state.value
        label_filter = f"&labels={label}" if label else ""
        items: list[forge.LedgerItem] = []
        pages_fetched = 0
        page_number = 1
        while True:
            page_values = self._ledger_items_page(
                page_number, query_state=query_state, label_filter=label_filter
            )
            pages_fetched += 1
            items.extend(self._ledger_item(value) for value in page_values)
            if len(page_values) < ISSUES_PER_PAGE:
                return forge.Listing(tuple(items), pages_fetched)
            page_number += 1

    def open_item_count(self) -> int:
        raw = self._run(["api", f"repos/{self.repository}", "--jq", ".open_issues_count"])
        try:
            count = int(raw)
        except ValueError as error:
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed open-issue count"
            ) from error
        if count < 0:
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed open-issue count")
        return count

    def ensure_label(self, name: str, *, colour: str, description: str) -> None:
        self._run(
            [
                "label",
                "create",
                name,
                "--repo",
                self.repository.path,
                "--color",
                colour,
                "--description",
                description,
                "--force",
            ]
        )

    def create_item(self, *, title: str, body: str) -> int:
        raw = self._run(
            ["api", "--method", "POST", f"repos/{self.repository}/issues", "--input", "-"],
            input_data=json.dumps({"title": title, "body": body}).encode("utf-8"),
        )
        try:
            created = json.loads(raw)
        except json.JSONDecodeError as error:
            raise forge.ForgeMalformedResponseError(
                "GitHub returned invalid created-ledger JSON"
            ) from error
        if (
            not isinstance(created, dict)
            or isinstance(created.get("number"), bool)
            or not isinstance(created.get("number"), int)
            or created["number"] < 1
        ):
            raise forge.ForgeMalformedResponseError("GitHub did not return a created ledger number")
        return created["number"]

    def lock_item(self, number: int) -> None:
        self._run(["api", "--method", "PUT", f"repos/{self.repository}/issues/{number}/lock"])

    def close_item(self, number: int) -> None:
        self._run(["issue", "close", str(number), "--repo", self.repository.path])
