"""GitHub issue-comment adapter for the claim ledger."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar

from . import board, protocol
from .protocol import (
    MAX_PROTOCOL_BYTES,
    MAX_PROTOCOL_EVENTS,
    PROJECTION_MARKER_PATTERN,
    REPOSITORY_PATTERN,
    TRUSTED_ASSOCIATIONS,
    ClaimError,
    ClaimUnavailable,
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
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
GH_TIMEOUT_SECONDS = 60
GH_QUIET_ENVIRONMENT = {
    "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}
API_BLOCKER_STATES: dict[str, board.BlockerState] = {
    "open": board.BlockerState.OPEN,
    "closed": board.BlockerState.CLOSED,
}


def github_command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(GH_QUIET_ENVIRONMENT)
    return environment


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _query_days(start: date, end: date) -> tuple[date, ...]:
    """One calendar UTC day per merged-pull-request query shard, `start` through `end` inclusive."""
    if end < start:
        raise ClaimError("merged pull request window ends before it starts")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _bounded_command(
    command: list[str], *, purpose: str, input_data: bytes | None = None
) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=github_command_environment(),
        )
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise ClaimError(f"{command[0]} is required for issue claims") from error
        raise ClaimError(f"cannot start {purpose}: {error}") from error
    selector: selectors.BaseSelector | None = None
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + GH_TIMEOUT_SECONDS
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        pending_input = memoryview(input_data) if input_data is not None else None
        if pending_input is not None:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise ClaimError(f"{purpose} timed out")
            try:
                events = selector.select(remaining)
            except OSError as error:
                raise ClaimError(f"{purpose} failed while waiting for I/O: {error}") from error
            if not events:
                _stop_process(process)
                raise ClaimError(f"{purpose} timed out")
            for key, _ in events:
                if key.data == "stdin":
                    assert pending_input is not None
                    try:
                        written = os.write(key.fileobj.fileno(), pending_input)
                    except BrokenPipeError:
                        written = len(pending_input)
                    except OSError as error:
                        _stop_process(process)
                        raise ClaimError(
                            f"{purpose} failed while sending bounded input: {error}"
                        ) from error
                    pending_input = pending_input[written:]
                    if not pending_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except OSError as error:
                    raise ClaimError(f"{purpose} failed while reading output: {error}") from error
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_COMMAND_OUTPUT_BYTES:
                    _stop_process(process)
                    raise ClaimError(f"{purpose} exceeded its output limit")
        try:
            return_code = process.wait(timeout=1)
        except subprocess.TimeoutExpired as error:
            _stop_process(process)
            raise ClaimError(f"{purpose} did not exit after closing its output") from error
    except OSError as error:
        raise ClaimError(f"{purpose} failed while coordinating I/O: {error}") from error
    finally:
        try:
            if selector is not None:
                selector.close()
        except OSError:
            pass
        finally:
            _close_process_streams(process)
            if process.poll() is None:
                _stop_process(process)
    try:
        decoded = strip_ansi(output.decode("utf-8")).strip()
    except UnicodeDecodeError as error:
        raise ClaimError(f"{purpose} returned non-UTF-8 output") from error
    if return_code != 0:
        raise ClaimError(decoded or f"{purpose} failed with exit {return_code}")
    return decoded


class GitHubIssueComments:
    def __init__(self, repository: str):
        if REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise ClaimError("repository must be OWNER/REPO")
        self.repository = repository
        self._rollover_warning_printed = False

    def _run(self, arguments: list[str], *, input_data: bytes | None = None) -> str:
        return _bounded_command(
            ["gh", *arguments],
            purpose="GitHub issue coordination",
            input_data=input_data,
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
            raise ClaimError(f"GitHub returned invalid {description} JSON") from error
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
        return tuple(
            self._parse_comment(value)
            for value in self._json_lines(raw, "issue-comment")
        )

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
                    "claim ledger protocol limit reached; perform the "
                    "documented ledger rollover"
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
                and PROJECTION_MARKER_PATTERN.fullmatch(
                    comment.body.partition("\n")[0]
                )
                is not None
            )
            if len(page_comments) < COMMENTS_PER_PAGE:
                return tuple(projections)
        raise ClaimError("owning issue comment limit reached during projection update")

    def _parse_comment(self, value: object) -> IssueComment:
        if not isinstance(value, dict):
            raise ClaimError("GitHub issue-comment entry must be an object")
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
            raise ClaimError("GitHub returned a malformed issue-comment entry")
        return IssueComment(identifier, created_at, updated_at, body, association, url)

    def _board_issue(self, value: object) -> board.Issue:
        if not isinstance(value, dict):
            raise ClaimError("GitHub returned a malformed board issue")
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
            raise ClaimError("GitHub returned a malformed board issue")
        return board.Issue(number, title, tuple(labels), body, created_at, updated_at)

    def _board_pull_request(self, value: object) -> board.PullRequest:
        if not isinstance(value, dict):
            raise ClaimError("GitHub returned a malformed board pull request")
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
            raise ClaimError("GitHub returned a malformed board pull request")
        return board.PullRequest(number, title, body, head_ref_name, merged_at)

    def _pull_request_detail(self, value: object) -> board.PullRequestDetail:
        if not isinstance(value, dict):
            raise ClaimError("GitHub returned a malformed pull request")
        number = value.get("number")
        body = value.get("body")
        if body is None:
            body = ""
        base_ref_name = value.get("baseRefName")
        head_ref_name = value.get("headRefName")
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
            or not isinstance(login, str)
            or not login
            or (merged_at is not None and not isinstance(merged_at, str))
            or (isinstance(merged_at, str) and TIMESTAMP_PATTERN.fullmatch(merged_at) is None)
        ):
            raise ClaimError("GitHub returned a malformed pull request")
        return board.PullRequestDetail(
            number, body, base_ref_name, head_ref_name, login, merged_at is not None
        )

    def pull_request_detail(self, number: int) -> board.PullRequestDetail:
        raw = self._run(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repository,
                "--json",
                "number,body,baseRefName,headRefName,author,mergedAt",
                "--jq",
                ".",
            ]
        )
        values = self._json_lines(raw, "pull request")
        if len(values) != 1:
            raise ClaimError("GitHub returned a malformed pull request")
        return self._pull_request_detail(values[0])

    def _issue_reference(self, value: object, description: str) -> board.IssueReference:
        if not isinstance(value, dict):
            raise ClaimError(f"GitHub returned a malformed {description}")
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
            or REPOSITORY_PATTERN.fullmatch(repository) is None
        ):
            raise ClaimError(f"GitHub returned a malformed {description}")
        return board.IssueReference(repository, number)

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
        except ClaimError as error:
            # The sub-issue endpoint answers "no parent" with an HTTP 404, which
            # `gh api` reports in its combined output; that is an answer, not a
            # failure.
            if "HTTP 404" in str(error):
                return None
            raise
        values = self._json_lines(raw, "parent issue")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ClaimError("GitHub returned a malformed parent issue")
        body = values[0].get("body")
        if not isinstance(body, str):
            raise ClaimError("GitHub returned a malformed parent issue")
        return board.ParentIssue(self._issue_reference(values[0], "parent issue"), body)

    def open_sub_issues(self, number: int) -> tuple[board.IssueReference, ...]:
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues/{number}/sub_issues?per_page=100",
                "--jq",
                '.[] | select(.state == "open") | {number,repository:.repository_url}',
            ]
        )
        return tuple(
            self._issue_reference(value, "sub-issue")
            for value in self._json_lines(raw, "sub-issue")
        )

    def default_branch(self) -> str:
        branch = self._run(
            ["api", f"repos/{self.repository}", "--jq", ".default_branch"]
        )
        if protocol.BRANCH_PATTERN.fullmatch(branch) is None:
            raise ClaimError("GitHub returned a malformed default branch")
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
                    'createdAt:.created_at,updatedAt:.updated_at,'
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
        except ClaimError as error:
            if "HTTP 404" in str(error):
                return board.BlockerReference(number, board.BlockerState.MISSING, False)
            raise
        values = self._json_lines(raw, "board blocker")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ClaimError("GitHub returned a malformed board blocker")
        value = values[0]
        returned_number = value.get("number")
        state = value.get("state")
        closed_at = value.get("closedAt")
        is_pull_request = value.get("isPullRequest")
        blocker_state = API_BLOCKER_STATES.get(state) if isinstance(state, str) else None
        if (
            isinstance(returned_number, bool)
            or returned_number != number
            or blocker_state is None
            or not isinstance(is_pull_request, bool)
            or (closed_at is not None and not isinstance(closed_at, str))
            or (
                isinstance(closed_at, str)
                and TIMESTAMP_PATTERN.fullmatch(closed_at) is None
            )
            or (blocker_state is board.BlockerState.CLOSED and closed_at is None)
        ):
            raise ClaimError("GitHub returned a malformed board blocker")
        parsed_closed_at = None
        if closed_at is not None:
            try:
                parsed_closed_at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise ClaimError("GitHub returned a malformed board blocker") from error
            if parsed_closed_at.tzinfo is None:
                raise ClaimError("GitHub returned a malformed board blocker")
            parsed_closed_at = parsed_closed_at.astimezone(timezone.utc)
        return board.BlockerReference(
            number,
            blocker_state,
            is_pull_request,
            parsed_closed_at,
        )

    def list_board_blockers(
        self, numbers: frozenset[int]
    ) -> tuple[board.BlockerReference, ...]:
        if not numbers:
            return ()
        with ThreadPoolExecutor(
            max_workers=min(len(numbers), PARALLEL_FETCH_CONCURRENCY)
        ) as pool:
            return tuple(pool.map(self._board_blocker, sorted(numbers)))

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repository,
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
                self.repository,
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
        cutoff = since.astimezone(timezone.utc)
        days = _query_days(cutoff.date(), datetime.now(timezone.utc).date())
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
            day for day, shard in zip(days, shards) if len(shard) >= MAX_RECENT_MERGED_PULL_REQUESTS
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
                merged_at = datetime.fromisoformat(pull_request.merged_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise ClaimError("GitHub returned a malformed merged board pull request") from error
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
                ".[] | select(has(\"pull_request\") | not) | .number",
            ]
        )
        issues: list[int] = []
        for value in self._json_lines(raw, "claimed-issue"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ClaimError("GitHub returned a malformed claimed-issue entry")
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
            raise ClaimError("GitHub returned a malformed successor issue")
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
            raise ClaimUnavailable(
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
                raise ClaimError(
                    "owning issue has a projection from a newer ledger generation"
                )
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
            ["issue", "comment", str(issue), "--repo", self.repository, "--body-file", "-"],
            input_data=encoded,
        )

    def add_label(self, issue: int, label: str) -> None:
        self._run(["issue", "edit", str(issue), "--repo", self.repository, "--add-label", label])

    def remove_label(self, issue: int, label: str) -> None:
        self._run(
            ["issue", "edit", str(issue), "--repo", self.repository, "--remove-label", label]
        )
