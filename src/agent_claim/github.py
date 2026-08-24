"""GitHub issue-comment adapter for the claim ledger."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import time

from . import protocol
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

TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
COMMENTS_PER_PAGE = 100
MAX_LEDGER_PAGES = 100
LEDGER_ROLLOVER_WARNING_PAGES = 80
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
GH_TIMEOUT_SECONDS = 60


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
        decoded = output.decode("utf-8").strip()
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
        values: list[object] = []
        try:
            for line in raw.splitlines():
                if line.strip():
                    values.append(json.loads(line))
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

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        comments: list[IssueComment] = []
        protocol_bytes = 0
        total_comments = 0
        for page in range(1, MAX_LEDGER_PAGES + 1):
            page_comments = self._comment_page(issue, page)
            total_comments += len(page_comments)
            for parsed in page_comments:
                if not is_protocol_candidate(parsed):
                    continue
                protocol_bytes += len(parsed.body.encode("utf-8"))
                if (
                    len(comments) >= MAX_PROTOCOL_EVENTS
                    or protocol_bytes > MAX_PROTOCOL_BYTES
                ):
                    raise ClaimError(
                        "claim ledger protocol limit reached; perform the "
                        "documented ledger rollover"
                    )
                comments.append(parsed)
            if len(page_comments) < COMMENTS_PER_PAGE:
                if (
                    page >= LEDGER_ROLLOVER_WARNING_PAGES
                    and not self._rollover_warning_printed
                ):
                    print(
                        f"WARNING: claim ledger has {total_comments} comments; "
                        "schedule the documented rollover",
                        file=sys.stderr,
                    )
                    self._rollover_warning_printed = True
                return tuple(comments)
        raise ClaimError(
            "claim ledger page limit reached; perform the documented ledger rollover"
        )

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
