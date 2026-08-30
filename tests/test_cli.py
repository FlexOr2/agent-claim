from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_claim import board, checkout, discovery, github, protocol
from agent_claim import cli as issue_claim
from agent_claim.cli import (  # noqa: E402
    MAX_COMMENT_BYTES,
    ActiveClaim,
    ClaimantRelease,
    ClaimError,
    ClaimRequest,
    ClaimUnavailable,
    DuplicateClaimConflict,
    DuplicateClaimRepair,
    GitHubIssueComments,
    InvalidClaimMarker,
    IssueComment,
    IssueIdentity,
    LaneIdentity,
    LedgerSupersede,
    LedgerSuperseded,
    _repository,
    _status,
    acquire_claim,
    active_claims,
    claim_comment,
    claim_label,
    claims_conflict,
    claims_holding_path,
    is_protocol_candidate,
    parse_claim_event,
    reconcile_all_labels,
    reconcile_issue_label,
    release_claim,
    release_comment,
    repair_duplicate_claims,
    rescope_claim,
    supersede_comment,
    supersede_ledger,
)

issue_claim.configure_ledger(71)
LEDGER_ISSUE = 71

_LIVE_VERSIONED_PATHS = checkout.versioned_paths
_LIVE_TRUNK_LANDING_TIMES = checkout.trunk_landing_times

BASE = "a" * 40


def ledger_row(
    number: int,
    *,
    body: str = issue_claim.LEDGER_BODY_MARKER,
    state: str = "open",
    locked: bool = True,
    association: str = "OWNER",
) -> dict[str, object]:
    return {
        "number": number,
        "state": state,
        "locked": locked,
        "body": body,
        "author_association": association,
        "labels": [],
        "is_pull_request": False,
    }


def ledger_client(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]
) -> tuple[GitHubIssueComments, list[list[str]]]:
    client = GitHubIssueComments("example/agent-claim")
    observed: list[list[str]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        issue_path = f"repos/{client.repository}/issues"
        if arguments[:3] == ["api", "--paginate", f"{issue_path}?state=all&per_page=100"]:
            return "\n".join(json.dumps(row) for row in rows)
        if arguments[:3] == ["api", "--paginate", f"{issue_path}?state=open&per_page=100"]:
            return "\n".join(json.dumps(row) for row in rows if row["state"] == "open")
        return ""

    monkeypatch.setattr(client, "_run", run)
    return client, observed


def test_discovery_requires_a_locked_canonical_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = ledger_client(monkeypatch, [ledger_row(9), ledger_row(10)])
    assert issue_claim.discover_ledger(client) == 9

    unlocked, _ = ledger_client(monkeypatch, [ledger_row(2, locked=False)])
    with pytest.raises(ClaimUnavailable, match="not locked"):
        issue_claim.discover_ledger(unlocked)


def test_discovery_refuses_other_machine_coordination_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = ledger_client(
        monkeypatch, [ledger_row(4, body="<!-- another-claim-ledger:v1 -->")]
    )
    with pytest.raises(ClaimError, match="refusing to compete"):
        issue_claim.discover_ledger(client)


def test_untrusted_exact_and_arbitrary_markers_have_no_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ledger_row(1, association="NONE"),
        ledger_row(2),
        ledger_row(3, body="<!-- arbitrary-claim-ledger:v1 -->", association="NONE"),
    ]
    client, observed = ledger_client(monkeypatch, rows)

    assert issue_claim.discover_ledger(client) == 2
    assert issue_claim.bootstrap_ledger(client) == 2
    assert not any(arguments[:3] == ["issue", "close", "1"] for arguments in observed)
    assert not any(arguments[:3] == ["issue", "close", "3"] for arguments in observed)


def test_bootstrap_repairs_trusted_legacy_marker_and_closes_later_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, observed = ledger_client(
        monkeypatch,
        [ledger_row(2, locked=False), ledger_row(3)],
    )

    assert issue_claim.bootstrap_ledger(client) == 2
    assert ["api", "--method", "PUT", "repos/example/agent-claim/issues/2/lock"] in observed
    assert ["issue", "close", "3", "--repo", "example/agent-claim"] in observed
    assert any(
        arguments[:3] == ["label", "create", issue_claim.LEDGER_LABEL]
        for arguments in observed
    )


def test_bootstrap_creates_and_locks_a_ledger_when_none_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    created = False
    observed: list[list[str]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        nonlocal created
        observed.append(arguments)
        if arguments[:2] == ["api", "--paginate"]:
            return json.dumps(ledger_row(11)) if created else ""
        if arguments[:4] == ["api", "--method", "POST", "repos/example/agent-claim/issues"]:
            created = True
            return json.dumps({"number": 11})
        return ""

    monkeypatch.setattr(client, "_run", run)

    assert issue_claim.bootstrap_ledger(client) == 11
    assert ["api", "--method", "PUT", "repos/example/agent-claim/issues/11/lock"] in observed


def test_bootstrap_ignores_an_untrusted_unlocked_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, observed = ledger_client(
        monkeypatch,
        [ledger_row(1, locked=False, association="NONE"), ledger_row(2)],
    )
    assert issue_claim.discover_ledger(client) == 2
    assert issue_claim.bootstrap_ledger(client) == 2
    assert not any(arguments[-1].endswith("/issues/1/lock") for arguments in observed)


def comment(
    identifier: int,
    body: str,
    *,
    created_at: str | None = None,
    association: str = "OWNER",
) -> IssueComment:
    return IssueComment(
        identifier=identifier,
        created_at=created_at or f"2026-08-21T00:00:{identifier:02d}Z",
        updated_at=created_at or f"2026-08-21T00:00:{identifier:02d}Z",
        body=body,
        author_association=association,
        url=f"https://github.com/example/agent-claim/issues/71#issuecomment-{identifier}",
    )


def request(
    claim_id: str = "claim-a",
    agent: str = "Codex Sol",
    *,
    issue: int | None = 71,
    lane: bool = False,
    role: str = "builder",
    branch: str | None = None,
    scope: tuple[str, ...] = ("docs/COORDINATION.md", "scripts/issue_claim.py"),
    resource: str | None = None,
    resource_value: int | None = None,
) -> ClaimRequest:
    """Build a `ClaimRequest`, issue-identified by default or lane-identified via `lane=True`.

    `issue=None` implies `lane=True` (mirrors the CLI's own "omitted issue number
    means lane mode" rule) so parametrized tables can drive both identity kinds
    from one `issue`/`lane` axis without hand-building identities at every call site.
    """
    lane = lane or issue is None
    identity: protocol.ClaimIdentity = (
        protocol.LaneIdentity() if lane else protocol.IssueIdentity(issue)
    )
    default_branch = f"docs/lane-{claim_id}" if lane else f"codex/issue-{issue}-claims"
    return ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=BASE,
        branch=branch or default_branch,
        scope=scope,
        claim_id=claim_id,
        resource=resource,
        resource_value=resource_value,
    )


def _claims_client(*standing: ClaimRequest) -> FakeComments:
    return FakeComments(
        {
            LEDGER_ISSUE: [
                comment(index, claim_comment(claimed))
                for index, claimed in enumerate(standing, start=1)
            ]
        }
    )


@dataclass
class FakeComments:
    comments: dict[int, list[IssueComment]] = field(default_factory=dict)
    labels: set[int] = field(default_factory=set)
    other_labels: dict[str, set[int]] = field(default_factory=dict)
    valid_successors: set[int] = field(default_factory=set)
    inject_before_next_ledger_post: IssueComment | None = None
    inject_after_next_ledger_post: IssueComment | None = None
    inject_during_next_add: IssueComment | None = None
    inject_during_next_remove: IssueComment | None = None
    fail_add_label: bool = False
    fail_remove_label: bool = False
    board_issues: tuple[board.Issue, ...] = ()
    board_open_pull_requests: tuple[board.PullRequest, ...] = ()
    board_merged_pull_requests: tuple[board.PullRequest, ...] = ()

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        return tuple(
            entry
            for entry in self.comments.get(issue, [])
            if protocol.is_protocol_candidate(entry)
        )

    def post_comment(self, issue: int, body: str) -> str:
        if issue == protocol.LEDGER_ISSUE and self.inject_before_next_ledger_post is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_before_next_ledger_post
            )
            self.inject_before_next_ledger_post = None
        identifier = max(
            (
                entry.identifier
                for entries in self.comments.values()
                for entry in entries
            ),
            default=0,
        ) + 1
        posted = comment(identifier, body)
        self.comments.setdefault(issue, []).append(posted)
        if issue == protocol.LEDGER_ISSUE and self.inject_after_next_ledger_post is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_after_next_ledger_post
            )
            self.inject_after_next_ledger_post = None
        return posted.url

    def add_label(self, issue: int, label: str) -> None:
        assert label == claim_label()
        if self.fail_add_label:
            raise ClaimError("label add failed")
        if self.inject_during_next_add is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(self.inject_during_next_add)
            self.inject_during_next_add = None
        self.labels.add(issue)

    def remove_label(self, issue: int, label: str) -> None:
        assert label == claim_label()
        if self.fail_remove_label:
            raise ClaimError("label remove failed")
        if self.inject_during_next_remove is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_during_next_remove
            )
            self.labels.add(self.inject_during_next_remove_event.identity.issue)
            self.inject_during_next_remove = None
        self.labels.discard(issue)

    @property
    def inject_during_next_remove_event(self):
        assert self.inject_during_next_remove is not None
        event = parse_claim_event(self.inject_during_next_remove)
        assert event is not None
        return event

    def list_claimed_issues(self) -> tuple[int, ...]:
        return tuple(sorted(self.labels))

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        return self.board_issues

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return self.board_open_pull_requests

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        return self.board_merged_pull_requests

    def validate_successor(self, issue: int) -> None:
        if issue not in self.valid_successors:
            raise ClaimUnavailable(
                f"successor #{issue} must be an open, empty, collaborator-locked issue"
            )

    def upsert_projection(
        self,
        issue: int,
        body: str,
        *,
        create: bool = True,
        adopt_stale: bool = False,
    ) -> bool:
        entries = self.comments.setdefault(issue, [])
        all_projections = [
            entry
            for entry in entries
            if issue_claim.PROJECTION_MARKER_PATTERN.fullmatch(
                entry.body.partition("\n")[0]
            )
            is not None
        ]
        projections = [
            entry
            for entry in all_projections
            if entry.body.partition("\n")[0] == issue_claim._projection_marker()
        ]
        adoptable_projections = [
            entry
            for entry in all_projections
            if (issue_claim._projection_ledger(entry) or 0) <= protocol.LEDGER_ISSUE
        ]
        has_newer_projection = any(
            (issue_claim._projection_ledger(entry) or 0) > protocol.LEDGER_ISSUE
            for entry in all_projections
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
            self.post_comment(issue, body)
            projections = [self.comments[issue][-1]]
        owner, *duplicates = sorted(
            projections,
            key=lambda entry: (entry.created_at, entry.identifier),
        )
        owner_index = entries.index(owner)
        entries[owner_index] = replace(owner, body=body, updated_at=owner.created_at)
        duplicate_ids = {entry.identifier for entry in duplicates}
        entries[:] = [entry for entry in entries if entry.identifier not in duplicate_ids]
        return True

    def neutralize_claim_comment(self, comment_id: int, body: str) -> None:
        for entries in self.comments.values():
            for index, entry in enumerate(entries):
                if entry.identifier == comment_id:
                    # A real PATCH bumps updated_at; mirror that so the "was edited
                    # after publication" guard stays live for anything a caller
                    # neutralizes without also stripping its claim marker prefix.
                    edited_at = f"2026-08-22T00:00:{entry.identifier:02d}Z"
                    entries[index] = replace(entry, body=body, updated_at=edited_at)
                    return
        raise ClaimError(f"comment {comment_id} not found for neutralization")


def test_board_projects_fixture_json_without_github_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    issues_json = [
        {
            "number": 10,
            "title": "Security boundary",
            "labels": ["security"],
            "body": (
                "## Now\nInspect.\n\n## Next\nLand #10.\n\n## Blocked by\nNone."
                "\n\n## Done when\nMerged."
            ),
            "createdAt": "2026-08-10T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
        },
        {
            "number": 11,
            "title": "Product dependency",
            "labels": ["product"],
            "body": (
                "## Now\nImplement.\n\n## Next\nReview implementation.\n\n## Blocked by\n#10"
                "\n\n## Done when\nReleased."
            ),
            "createdAt": "2026-08-12T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
        },
        {
            "number": 12,
            "title": "Old notes",
            "labels": ["ux"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-01T00:00:00Z",
            "updatedAt": "2026-08-10T00:00:00Z",
        },
        {
            "number": 13,
            "title": "Cleanup landed",
            "labels": ["cleanup"],
            "body": (
                "## Now\nVerify.\n\n## Next\nClose issue.\n\n## Blocked by\nNone."
                "\n\n## Done when\nReleased."
            ),
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-19T00:00:00Z",
        },
        {
            "number": 14,
            "title": "Older cleanup",
            "labels": ["cleanup"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
        },
    ]
    open_prs_json = [
        {"number": 90, "title": "Fixes #10", "body": "", "headRefName": "other", "mergedAt": None},
        {
            "number": 91,
            "title": "In progress",
            "body": "",
            "headRefName": "codex/issue-11-claims",
            "mergedAt": None,
        },
        {
            "number": 93,
            "title": "Planning note",
            "body": None,
            "headRefName": "notes",
            "mergedAt": None,
        },
    ]
    merged_prs_json = [
        {
            "number": 92,
            "title": "Fixes #13",
            "body": "",
            "headRefName": "codex/issue-13-cleanup",
            "mergedAt": "2026-08-20T12:00:00Z",
        },
        {
            "number": 94,
            "title": "Fixes #14",
            "body": "",
            "headRefName": "codex/issue-14-cleanup",
            "mergedAt": "2026-08-06T23:59:59Z",
        }
    ]
    active = request("board-claim", issue=11, branch="codex/issue-11-claims")
    ledger_comment = {
        "id": 1,
        "created_at": "2026-08-20T12:00:00Z",
        "updated_at": "2026-08-20T12:00:00Z",
        "body": claim_comment(active),
        "author_association": "OWNER",
        "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-1",
    }
    client = GitHubIssueComments("example/agent-claim")
    observed: list[list[str]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        assert input_data is None
        observed.append(arguments)
        endpoint = next((argument for argument in arguments if argument.startswith("repos/")), "")
        if "/comments?" in endpoint:
            rows = [ledger_comment]
        elif "/issues?" in endpoint:
            rows = issues_json
        elif arguments[:2] == ["pr", "list"] and "open" in arguments:
            rows = open_prs_json
        elif arguments[:2] == ["pr", "list"] and "merged" in arguments:
            rows = merged_prs_json
        else:
            pytest.fail(f"unexpected board request: {arguments}")
        return "\n".join(json.dumps(row) for row in rows)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, tzinfo=timezone.utc)

    monkeypatch.setattr(client, "_run", run)
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out
    assert "CONTRACT" in rendered and "NEXT" in rendered and "ACTIONABLE" in rendered
    assert "#10" in rendered
    assert "no: claimed" in rendered
    assert all("--method" not in arguments for arguments in observed)
    assert all("--jq" in arguments for arguments in observed)
    merged_request = next(arguments for arguments in observed if "merged" in arguments)
    assert "merged:>=2026-08-07" in merged_request

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    first = payload["items"][0]
    ten = next(item for item in payload["items"] if item["number"] == 10)
    eleven = next(item for item in payload["items"] if item["number"] == 11)
    thirteen = next(item for item in payload["items"] if item["number"] == 13)
    fourteen = next(item for item in payload["items"] if item["number"] == 14)
    assert first["number"] == 10
    assert ten["stage"] == "in-flight"
    assert ten["unblocks_count"] == 1
    assert ten["contract"]["next"] == "Land #10."
    assert ten["contract_complete"] is True
    assert ten["actionable"] is True
    assert ten["actionable_reason"] is None
    assert eleven["active_claim"] == "Codex Sol (builder)"
    assert eleven["actionable_reason"] == "claimed"
    assert thirteen["stage"] == "code-landed"
    assert fourteen["stage"] == "text-only"
    assert fourteen["actionable_reason"] == "body incomplete"
    assert [item["number"] for item in payload["ready_now"]] == [10, 13]
    assert [item["number"] for item in payload["stale"]] == [12]
    assert next(item for item in payload["items"] if item["number"] == 12)["stage"] == "text-only"
    assert 11 not in [item["number"] for item in payload["ready_now"]]


def test_board_exposes_all_expectation_states(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issues = (
        board_issue(10, "No expectations", complete_contract("Claim #10.")),
        board_issue(
            11,
            "Proposed expectations",
            complete_contract("Claim #11.")
            + "\n\n"
            + expectation_block("- Name it. *(Default: no)*"),
        ),
        board_issue(
            12,
            "Ruled expectations",
            complete_contract("Claim #12.")
            + "\n\n"
            + expectation_block("- Name it. *(geregelt: ja)*"),
        ),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out
    assert "EXPECT" in rendered
    assert "-         Claim #10." in next(
        line for line in rendered.splitlines() if "No expectations" in line
    )
    assert "proposed  Claim #11." in next(
        line for line in rendered.splitlines() if "Proposed expectations" in line
    )
    assert "ruled 0   Claim #12." in next(
        line for line in rendered.splitlines() if "Ruled expectations" in line
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    expectation_states = {
        item["number"]: item["expectation_state"]
        for item in json.loads(capsys.readouterr().out)["items"]
    }
    assert expectation_states == {10: "-", 11: "proposed", 12: "ruled"}


def board_issue(
    number: int,
    title: str,
    body: str,
    *,
    labels: tuple[str, ...] = (),
) -> board.Issue:
    return board.Issue(
        number,
        title,
        labels,
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )


def complete_contract(next_step: str, *, blocked_by: str = "") -> str:
    return (
        "## Now\nWork is ready.\n\n"
        f"## Next\n{next_step}\n\n"
        f"## Blocked by\n{blocked_by}\n\n"
        "## Done when\nThe work is merged."
    )


def expectation_block(
    *lines: str, heading: str = "Erwartung (refine-Lauf 28.08.2026)"
) -> str:
    return f"## {heading}\n" + "\n".join(lines)


@pytest.mark.parametrize(
    ("issues", "claims", "arguments", "expected_exit", "expected_output"),
    [
        pytest.param(
            (
                board_issue(10, "Lower work", complete_contract("Claim #10.")),
                board_issue(11, "Top work", complete_contract("Claim #11.")),
                board_issue(12, "Depends on top", "## Blocked by\n#11"),
            ),
            (),
            ("next",),
            0,
            "#11 score 10: Top work\nNext: Claim #11.\n",
            id="names_the_highest_scored_actionable_item",
        ),
        pytest.param(
            (
                board_issue(10, "Lower work", complete_contract("Claim #10.")),
                board_issue(11, "Top work", complete_contract("Claim #11.")),
                board_issue(12, "Depends on top", "## Blocked by\n#11"),
            ),
            (),
            ("next", "--json"),
            0,
            {
                "number": 11,
                "score": 10,
                "title": "Top work",
                "next": "Claim #11.",
                "skipped": [],
                "ruling_landings": None,
                "ruling_old": None,
            },
            id="emits_the_highest_scored_actionable_item_as_json",
        ),
        pytest.param(
            (board_issue(10, "Incomplete", "## Now\nInvestigate."),),
            (),
            ("next",),
            3,
            "",
            id="returns_three_when_every_item_is_incomplete",
        ),
        pytest.param(
            (board_issue(10, "Claimed", complete_contract("Claim #10.")),),
            (request(issue=10),),
            ("next",),
            3,
            "",
            id="returns_three_when_every_item_is_claimed",
        ),
        pytest.param(
            (
                board_issue(9, "Open blocker", complete_contract("Claim #9.")),
                board_issue(10, "Blocked", complete_contract("Claim #10.", blocked_by="#9")),
            ),
            (),
            ("next",),
            0,
            "#9 score 10: Open blocker\nNext: Claim #9.\n",
            id="excludes_items_with_open_blockers",
        ),
    ],
)
def test_next_reports_the_highest_scored_actionable_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    issues: tuple[board.Issue, ...],
    claims: tuple[ClaimRequest, ...],
    arguments: tuple[str, ...],
    expected_exit: int,
    expected_output: str | dict[str, object],
) -> None:
    client = _claims_client(*claims)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == expected_exit
    rendered = capsys.readouterr().out

    if isinstance(expected_output, str):
        assert rendered == expected_output
    else:
        assert json.loads(rendered) == expected_output


@pytest.mark.parametrize(
    ("expectations", "expected_state", "expected_exit", "expected_output"),
    [
        pytest.param(
            "",
            board.ExpectationState.NONE,
            0,
            "#10 score -10: Work\nNext: Claim #10.\n",
            id="no_expectation_block_remains_actionable",
        ),
        pytest.param(
            expectation_block("- Name it. *(Default: yes)*"),
            board.ExpectationState.PROPOSED,
            3,
            "No actionable item.\n\nSKIPPED\n#10: Erwartungen ungeregelt\n",
            id="proposed_expectations_are_skipped",
        ),
        pytest.param(
            expectation_block("- Name it without a ruling."),
            board.ExpectationState.PROPOSED,
            3,
            "No actionable item.\n\nSKIPPED\n#10: Erwartungen ungeregelt\n",
            id="unmarked_expectations_are_skipped",
        ),
        pytest.param(
            expectation_block("- Name it. *(geregelt: maybe)*"),
            board.ExpectationState.PROPOSED,
            3,
            "No actionable item.\n\nSKIPPED\n#10: Erwartungen ungeregelt\n",
            id="malformed_expectations_are_skipped",
        ),
        pytest.param(
            expectation_block(
                "- Name it. *(geregelt: ja)*",
                "- Remove it. *(geregelt: NEIN, it stays)*",
            ),
            board.ExpectationState.RULED,
            0,
            "#10 score -10: Work\nNext: Claim #10.\n",
            id="fully_ruled_expectations_remain_actionable",
        ),
        pytest.param(
            expectation_block(
                "- Name it. *(geregelt: NEIN, not for this release)*",
                "- Remove it. *(Default: later)*",
                heading="Erwartungsliste",
            ),
            board.ExpectationState.PROPOSED,
            3,
            "No actionable item.\n\nSKIPPED\n#10: Erwartungen ungeregelt\n",
            id="mixed_expectations_are_skipped",
        ),
    ],
)
def test_next_reports_expectation_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    expectations: str,
    expected_state: board.ExpectationState,
    expected_exit: int,
    expected_output: str,
) -> None:
    issue = board_issue(
        10,
        "Work",
        "\n\n".join(part for part in (complete_contract("Claim #10."), expectations) if part),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == expected_exit
    assert capsys.readouterr().out == expected_output

    projected = board.build_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=timezone.utc)
    )
    assert projected.items[0].expectation_state is expected_state


def test_next_json_names_skipped_proposed_expectations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    proposed = board_issue(
        11,
        "Needs rulings",
        complete_contract("Claim #11.")
        + "\n\n"
        + expectation_block("- Name it. *(Default: no)*", heading="Erwartungen"),
    )
    ruled = board_issue(
        10,
        "Ready work",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (proposed, ruled))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "number": 10,
        "score": -10,
        "title": "Ready work",
        "next": "Claim #10.",
        "skipped": [{"number": 11, "reason": "Erwartungen ungeregelt"}],
        "ruling_landings": 0,
        "ruling_old": False,
    }


def test_claim_does_not_treat_proposed_expectations_as_an_out_of_order_competitor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _claims_client()
    proposed = board_issue(
        11,
        "Needs rulings",
        complete_contract("Claim #11.")
        + "\n\n"
        + expectation_block("- Name it. *(Default: yes)*"),
        labels=("security",),
    )
    claimed_request = request(issue=10, scope=("src/work.py",))
    monkeypatch.setattr(
        client,
        "list_open_board_issues",
        lambda: (board_issue(10, "Ready work", complete_contract("Claim #10.")), proposed),
    )
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 0
    )
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("out_of_order_reason", "expected_comment_reason"),
    [
        pytest.param(
            None,
            None,
            id="warns_without_a_reason",
        ),
        pytest.param(
            "Urgent customer incident.",
            "Out-of-order reason: Urgent customer incident.",
            id="records_an_explicit_reason",
        ),
    ],
)
def test_claim_warns_about_a_higher_scored_actionable_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    out_of_order_reason: str | None,
    expected_comment_reason: str | None,
) -> None:
    client = _claims_client()
    issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", "## Blocked by\n#11"),
    )
    claimed_request = replace(
        request("out-of-order", issue=10, scope=("src/lower.py",)),
        out_of_order_reason=out_of_order_reason,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    arguments = [
        "--repo",
        "example/agent-claim",
        "claim",
        "10",
        "--agent",
        "Codex Sol",
        "--scope",
        "src/lower.py",
    ]
    if out_of_order_reason is not None:
        arguments.extend(("--out-of-order", out_of_order_reason))

    assert issue_claim.main(arguments) == 0
    output = capsys.readouterr().out

    assert "WARNING" in output
    assert "#11" in output
    comment_body = client.comments[LEDGER_ISSUE][-1].body
    if expected_comment_reason is None:
        assert "Out-of-order reason:" not in comment_body
    else:
        assert expected_comment_reason in comment_body


@pytest.mark.parametrize(
    ("issue", "claims", "blocker_is_open", "expected"),
    [
        pytest.param(
            board_issue(10, "Ready", complete_contract("Claim #10.")),
            (),
            True,
            (True, None),
            id="ready",
        ),
        pytest.param(
            board_issue(10, "Claimed", complete_contract("Claim #10.")),
            (request(issue=10),),
            True,
            (False, "claimed"),
            id="claimed",
        ),
        pytest.param(
            board_issue(10, "Blocked", complete_contract("Claim #10.", blocked_by="#9")),
            (),
            True,
            (False, "blocked by #9"),
            id="blocked",
        ),
        pytest.param(
            board_issue(10, "Unblocked", complete_contract("Claim #10.", blocked_by="#9")),
            (),
            False,
            (True, None),
            id="closed_blocker",
        ),
        pytest.param(
            board_issue(10, "Incomplete", "## Now\nInvestigate."),
            (),
            True,
            (False, "body incomplete"),
            id="incomplete",
        ),
    ],
)
def test_board_reports_each_item_actionability_reason(
    issue: board.Issue,
    claims: tuple[ClaimRequest, ...],
    blocker_is_open: bool,
    expected: tuple[bool, str | None],
) -> None:
    blocker = board_issue(9, "Blocker", complete_contract("Claim #9."))
    projected = board.build_board(
        (blocker, issue) if blocker_is_open else (issue,),
        (),
        (),
        tuple(
            claim
            for request_value in claims
            if (claim := parse_claim_event(comment(1, claim_comment(request_value)))) is not None
        ),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    item = next(item for item in projected.items if item.number == issue.number)

    assert (item.actionable, item.actionable_reason) == expected


def test_board_collects_every_open_blocker_from_prose() -> None:
    blocked = board_issue(
        10,
        "Blocked",
        complete_contract(
            "Claim #10.",
            blocked_by="#790 Reparaturrunde (review) und #642 P3",
        ),
    )
    projected = board.build_board(
        (
            blocked,
            board_issue(642, "P3", complete_contract("Claim #642.")),
            board_issue(790, "Review", complete_contract("Claim #790.")),
        ),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    item = next(item for item in projected.items if item.number == 10)

    assert item.open_blockers == (642, 790)
    assert item.actionable is False
    assert item.actionable_reason == "blocked by #642, #790"


@pytest.mark.parametrize("blocked_by", ["nichts", "none", "None.", "keine", "-"])
def test_board_treats_nothing_blocker_values_as_unblocked(blocked_by: str) -> None:
    issue = board_issue(10, "Ready", complete_contract("Claim #10.", blocked_by=blocked_by))
    projected = board.build_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert projected.items[0].open_blockers == ()
    assert projected.items[0].actionable is True
    assert projected.items[0].actionable_reason is None


def test_board_parses_the_last_atelier_contract_projection() -> None:
    contract = board.parse_contract(
        "## Earlier section\n"
        "**Now:** An earlier section-local status.\n"
        "Next: An earlier section-local next step.\n"
        "**Blocked by:** #99\n"
        "Done when: The earlier section is complete.\n\n"
        "## Current projection\n"
        "**Now:** Fix the board parser.\n"
        "Next: Add a regression test.\n"
        "**Blocked by:** #47\n"
        "Done when: The review findings are resolved.\n"
    )

    assert contract == board.Contract(
        now="Fix the board parser.",
        next="Add a regression test.",
        blocked_by="#47",
        done_when="The review findings are resolved.",
    )


def test_board_reads_priority_configuration_from_the_checkout_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toplevel = tmp_path / "checkout"
    configuration_directory = toplevel / ".agent-claim"
    configuration_directory.mkdir(parents=True)
    (configuration_directory / "board.toml").write_text(
        'priority_labels = ["ux", "security"]\n'
    )
    nested_directory = toplevel / "src" / "agent_claim"
    nested_directory.mkdir(parents=True)
    monkeypatch.chdir(nested_directory)
    observed: list[list[str]] = []

    def git_output(arguments: list[str]) -> str:
        observed.append(arguments)
        return str(toplevel)

    class BoardClient:
        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (
                board.Issue(
                    20,
                    "Security issue",
                    ("security",),
                    "",
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:00Z",
                ),
                board.Issue(
                    21,
                    "UX issue",
                    ("ux",),
                    "",
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:00Z",
                ),
            )

        def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
            return ()

        def list_recent_merged_board_pull_requests(
            self, since: datetime
        ) -> tuple[board.PullRequest, ...]:
            return ()

    monkeypatch.setattr(checkout, "_git_output", git_output)
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    projected = issue_claim._board(BoardClient(), ())

    assert [item.number for item in projected.items] == [21, 20]
    assert observed == [["rev-parse", "--show-toplevel"]]


@pytest.mark.parametrize(
    ("updated_at", "expected_stale"),
    [
        ("2026-08-14T00:00:00Z", False),
        ("2026-08-13T00:00:00Z", True),
    ],
)
def test_board_marks_text_only_items_stale_only_after_seven_idle_days(
    updated_at: str, expected_stale: bool
) -> None:
    issue = board.Issue(22, "Idle issue", (), "", "2026-08-01T00:00:00Z", updated_at)

    projected = board.build_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=timezone.utc)
    )

    assert [item.number for item in projected.stale] == ([22] if expected_stale else [])


def test_board_ranks_a_real_blocker_ahead_of_a_blocked_product_item() -> None:
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    blocker = board.Issue(
        20,
        "Unlabelled prerequisite",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    product = board.Issue(
        21,
        "Product work",
        ("product",),
        "## Blocked by\n#20",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )

    projected = board.build_board(
        (blocker, product), (), (), (), board.BoardConfig(), now=now
    )

    assert [item.number for item in projected.items] == [20, 21]
    assert projected.items[0].unblocks_count == 1
    assert projected.items[1].open_blockers == (20,)


def test_board_category_order_keeps_ci_ahead_of_a_high_scoring_blocker() -> None:
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    ci = board.Issue(30, "CI", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    blocker = board.Issue(31, "Blocker", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    dependent = board.Issue(
        32,
        "Dependent",
        (),
        "## Blocked by\n#31",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    open_pull_request = board.PullRequest(90, "Fixes #31", "", "branch")

    projected = board.build_board(
        (ci, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
        now=now,
    )

    assert [item.number for item in projected.items[:2]] == [30, 31]
    assert projected.items[1].score > projected.items[0].score


def test_board_configuration_requires_unique_ordered_labels(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text('priority_labels = ["ux", "security"]\n')
    assert board.load_config(config_path).priority_labels == ("ux", "security")

    config_path.write_text("priority_labels = []\n")
    with pytest.raises(ClaimError, match="priority_labels"):
        board.load_config(config_path)


def marker(
    payload: dict[str, object], *, legacy: bool = False, attributed: bool = True
) -> str:
    version = "v1" if legacy else "v2"
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body = f"<!-- agent-claim:{version} {encoded} -->"
    agent = payload.get("agent")
    role = payload.get("role")
    if attributed and isinstance(agent, str) and isinstance(role, str):
        body += f"\n\nAgent: {agent} ({role})"
    return body


def release_event(claim, *, agent: str | None = None, role: str | None = None) -> str:
    return release_comment(
        claim,
        agent or claim.agent,
        role or claim.role,
        "landed",
    )


@pytest.mark.parametrize(
    ("lane", "expected_identity", "expected_branch"),
    [
        (False, IssueIdentity(71), "codex/issue-71-claims"),
        (True, LaneIdentity(), "docs/lane-claim-a"),
    ],
)
def test_claim_marker_round_trips_visible_contract(
    lane: bool, expected_identity: protocol.ClaimIdentity, expected_branch: str
) -> None:
    body = claim_comment(request(lane=lane))
    parsed = parse_claim_event(comment(1, body))

    assert parsed is not None
    assert parsed.identity == expected_identity
    assert parsed.claim_id == "claim-a"
    assert parsed.base == BASE
    assert parsed.branch == expected_branch
    assert parsed.scope == ("docs/COORDINATION.md", "scripts/issue_claim.py")
    assert "Agent: Codex Sol (builder)" in body
    assert "Auto-Runner" in body


def _marker_payload_keys(body: str) -> frozenset[str]:
    first_line = body.partition("\n")[0]
    encoded = first_line[len(protocol.MARKER_PREFIX) : -len(protocol.MARKER_SUFFIX)]
    return frozenset(json.loads(encoded))


def test_lane_and_issue_claim_markers_use_different_key_sets() -> None:
    """Compatibility evidence for Entschieden #4: a pre-issue-38 reader always calls
    `_required_issue` on a non-legacy claim marker before dispatching on action; a
    lane marker never carries an `issue` key, so that reader fails loud on the whole
    ledger instead of silently skipping the comment it cannot understand."""
    issue_keys = _marker_payload_keys(claim_comment(request(lane=False)))
    lane_keys = _marker_payload_keys(claim_comment(request(lane=True)))

    assert "issue" in issue_keys and "lane" not in issue_keys
    assert "lane" in lane_keys and "issue" not in lane_keys
    assert issue_keys != lane_keys


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(
            {"action": "claim", "issue": 71, "lane": True},
            "must not carry both issue and lane",
            id="both-issue-and-lane",
        ),
        pytest.param(
            {"action": "claim", "lane": "yes"},
            "lane field must be true",
            id="lane-not-exactly-true",
        ),
        pytest.param(
            {"action": "claim"},
            "issue must be a positive integer",
            id="neither-issue-nor-lane",
        ),
    ],
)
def test_marker_identity_discriminator_refuses_ambiguous_or_missing_keys(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(InvalidClaimMarker, match=match):
        parse_claim_event(comment(1, marker(payload)))


def test_protocol_parser_returns_action_specific_types() -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    released = parse_claim_event(comment(2, release_event(claimed)))
    assert isinstance(released, ClaimantRelease)
    assert released.reason == "landed"


def test_untrusted_claim_and_release_markers_are_ignored() -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert claimed is not None
    release = release_event(claimed)

    comments = (
        comment(1, claim_comment(request()), association="NONE"),
        comment(2, release, association="NONE"),
    )

    assert [parse_claim_event(entry) for entry in comments] == [None, None]
    assert active_claims(comments) == ()

    still_active = active_claims(
        (
            comment(1, claim_comment(request())),
            comment(2, release, association="NONE"),
        )
    )
    assert [claim.claim_id for claim in still_active] == ["claim-a"]


@pytest.mark.parametrize(
    "body",
    [
        "Review quotes <!-- agent-claim:v1 … --> as evidence.",
        "> <!-- agent-claim:v2 {} -->",
        "```html\n<!-- agent-claim:v2 {} -->\n```",
        "ordinary first line\n<!-- agent-claim:v2 {} -->",
    ],
)
def test_marker_is_protocol_only_as_the_exact_first_line(body: str) -> None:
    assert parse_claim_event(comment(1, body)) is None


def test_edited_protocol_comment_fails_loud() -> None:
    edited = comment(1, claim_comment(request()))
    edited = IssueComment(
        edited.identifier,
        edited.created_at,
        "2026-08-21T00:01:00Z",
        edited.body,
        edited.author_association,
        edited.url,
    )

    with pytest.raises(InvalidClaimMarker, match="edited after publication"):
        parse_claim_event(edited)


def test_fake_neutralize_claim_comment_bumps_updated_at_like_a_real_patch() -> None:
    """`FakeComments.neutralize_claim_comment` must mirror the real PATCH's effect on
    `updated_at`, so a comment edit that keeps a claim-marker-shaped first line still
    trips the "was edited after publication" guard in tests, not only in production."""
    claimed = comment(1, claim_comment(request()))
    client = FakeComments({LEDGER_ISSUE: [claimed]})

    client.neutralize_claim_comment(1, claimed.body)

    edited = client.comments[LEDGER_ISSUE][0]
    assert edited.updated_at != edited.created_at
    with pytest.raises(InvalidClaimMarker, match="edited after publication"):
        parse_claim_event(edited)


@pytest.mark.parametrize(
    "attribution",
    [None, "Agent: Other (builder)", "Agent: Codex Sol (reviewer)"],
)
def test_protocol_event_requires_exact_final_agent_attribution(
    attribution: str | None,
) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "codex/issue-71-claims",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["AGENTS.md"],
    }
    body = marker(payload, attributed=False)
    if attribution is not None:
        body += f"\n\n{attribution}"

    with pytest.raises(InvalidClaimMarker, match="exact agent attribution"):
        parse_claim_event(comment(1, body))


@pytest.mark.parametrize(
    "invalid",
    ["Codex\nSol", "Codex\x1fSol", " ", "x" * 129],
)
def test_outbound_comment_constructors_reject_controlled_identity_fields(
    invalid: str,
) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        claim_comment(replace(request(), agent=invalid))
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        release_comment(claimed, invalid, "builder", "landed")
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        supersede_comment(claimed, 170, invalid, "coordinator", "rollover")

    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        claim_comment(replace(request(), role=invalid))
    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        release_comment(claimed, "Codex Sol", invalid, "landed")
    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        supersede_comment(claimed, 170, "Codex Sol", invalid, "rollover")


@pytest.mark.parametrize(
    "invalid",
    ["landed\nwith detail", "landed\x1fdetail", " ", "x" * 513],
)
def test_outbound_comment_constructors_reject_controlled_reasons(invalid: str) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    with pytest.raises(ClaimError, match="reason must be one bounded non-empty line"):
        release_comment(claimed, "Codex Sol", "builder", invalid)
    with pytest.raises(ClaimError, match="reason must be one bounded non-empty line"):
        supersede_comment(claimed, 170, "Codex Sol", "coordinator", invalid)


def test_legacy_bootstrap_claim_is_read_only_when_marker_is_first_line() -> None:
    legacy = marker(
        {
            "action": "claim",
            "agent": "Codex Sol",
            "base": BASE,
            "branch": "codex/issue-71-claims",
            "claim_id": "bootstrap",
            "role": "builder",
            "scope": ["AGENTS.md"],
        },
        legacy=True,
    )

    parsed = parse_claim_event(comment(1, legacy))

    assert parsed is not None
    assert parsed.identity == IssueIdentity(LEDGER_ISSUE)
    assert parsed.claim_id == "bootstrap"


def test_legacy_marker_fails_loud_with_a_clear_message_before_ledger_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy marker binds to `LEDGER_ISSUE`; parsing one before `configure_ledger`
    runs must report the real defect (caller/setup), not misreport it as if the
    marker itself carried an invalid issue number."""
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 0)
    legacy = marker(
        {
            "action": "claim",
            "agent": "Codex Sol",
            "base": BASE,
            "branch": "codex/issue-71-claims",
            "claim_id": "bootstrap",
            "role": "builder",
            "scope": ["AGENTS.md"],
        },
        legacy=True,
    )

    with pytest.raises(ClaimError, match="before configure_ledger"):
        parse_claim_event(comment(1, legacy))


@pytest.mark.parametrize(
    ("branch", "scope"),
    [
        ("../not-a-branch", ["src"]),
        ("topic//double", ["src"]),
        ("topic.lock", ["src"]),
        ("topic", ["/home/operator/repo"]),
        ("topic", ["C:\\Users\\operator\\secret.txt"]),
        ("topic", ["C:/Users/operator/secret.txt"]),
        ("topic", ["\\\\server\\share\\secret.txt"]),
        ("topic", ["../other-repo"]),
        ("topic", ["."]),
        ("topic", ["./src"]),
        ("topic", ["src//file.py"]),
        ("topic", [".git/config"]),
    ],
)
def test_invalid_branch_and_private_or_noncanonical_scope_fail_loud(
    branch: str, scope: list[str]
) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": branch,
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": scope,
    }

    with pytest.raises(InvalidClaimMarker):
        parse_claim_event(comment(1, marker(payload)))


def test_unknown_or_missing_marker_fields_fail_loud() -> None:
    unknown = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "topic",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["src"],
        "surprise": True,
    }

    with pytest.raises(InvalidClaimMarker, match="fields differ"):
        parse_claim_event(comment(1, marker(unknown)))
    with pytest.raises(InvalidClaimMarker):
        parse_claim_event(comment(2, marker({"action": "claim"})))


def test_release_must_come_from_original_claimant() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    foreign_release = release_event(claimed, agent="Other", role="builder")

    with pytest.raises(InvalidClaimMarker, match="only be released by its claimant"):
        active_claims((comment(1, claimed_body), comment(2, foreign_release)))


def test_coordinator_override_is_explicit_and_bound_to_claim_comment() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    override = release_comment(
        claimed,
        "Codex Commissioner",
        "coordinator",
        "verified abandoned",
        coordinator_override=True,
    )

    assert active_claims((comment(1, claimed_body), comment(2, override))) == ()

    first_line = override.partition("\n")[0]
    payload = json.loads(
        first_line.removeprefix("<!-- agent-claim:v2 ").removesuffix(" -->")
    )
    payload["claim_comment_id"] = 999
    with pytest.raises(InvalidClaimMarker, match="wrong claim comment"):
        active_claims((comment(1, claimed_body), comment(2, marker(payload))))


def test_active_claims_strict_reader_refuses_reused_claim_ids_and_orphan_releases() -> None:
    """`active_claims` (the strict reader behind status/claim/release) still refuses a
    poisoned ledger outright; only `acquire_claim`'s pre-post guard and `reconcile`'s
    tolerant repair pass are allowed to treat a duplicate claim id as recoverable."""
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    released = release_event(claimed)

    with pytest.raises(InvalidClaimMarker, match="was reused"):
        active_claims(
            (
                comment(1, claimed_body),
                comment(2, released),
                comment(3, claimed_body),
            )
        )
    with pytest.raises(InvalidClaimMarker, match="before it was acquired"):
        active_claims((comment(1, released),))


def test_duplicate_claimant_releases_are_idempotent() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    first_release = release_comment(claimed, "Codex Sol", "builder", "landed")
    second_release = release_comment(claimed, "Codex Sol", "builder", "landed retry")

    assert active_claims(
        (
            comment(1, claimed_body),
            comment(2, first_release),
            comment(3, second_release),
        )
    ) == ()


@pytest.mark.parametrize("override_first", [False, True])
def test_claimant_and_coordinator_release_race_is_idempotent(
    override_first: bool,
) -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    claimant = release_comment(claimed, "Codex Sol", "builder", "landed")
    coordinator = release_comment(
        claimed,
        "Fleet Coordinator",
        "coordinator",
        "verified handoff",
        coordinator_override=True,
    )
    releases = (coordinator, claimant) if override_first else (claimant, coordinator)

    assert active_claims(
        (
            comment(1, claimed_body),
            comment(2, releases[0]),
            comment(3, releases[1]),
        )
    ) == ()


def test_supersede_atomically_terminates_the_only_ledger_claim() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = supersede_comment(
        claimed,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed rollover ready to land",
    )
    parsed = parse_claim_event(comment(2, frozen))
    assert isinstance(parsed, LedgerSupersede)
    assert parsed.successor_issue == 170

    with pytest.raises(LedgerSuperseded, match="successor #170"):
        active_claims((comment(1, claimed_body), comment(2, frozen)))
    late_claim = comment(
        3,
        claim_comment(request("late", issue=72, scope=("frontend",))),
    )
    with pytest.raises(LedgerSuperseded, match="successor #170"):
        active_claims((comment(1, claimed_body), comment(2, frozen), late_claim))


def test_supersede_is_an_inert_rejected_event_while_another_lane_is_active() -> None:
    rollover_body = claim_comment(request(issue=LEDGER_ISSUE, scope=("docs",)))
    rollover = parse_claim_event(comment(1, rollover_body))
    assert isinstance(rollover, ActiveClaim)
    other = comment(
        2,
        claim_comment(request("other", issue=72, scope=("frontend",))),
    )
    frozen = comment(
        3,
        supersede_comment(
            rollover,
            170,
            "Fleet Coordinator",
            "coordinator",
            "not actually drained",
        ),
    )

    observed = active_claims((comment(1, rollover_body), other, frozen))

    assert [claim.claim_id for claim in observed] == [rollover.claim_id, "other"]


def test_supersede_command_posts_terminal_event_and_observes_freeze() -> None:
    client = FakeComments(valid_successors={170})
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    selected = supersede_ledger(
        client,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed successor ready",
        acquired.claim_id,
    )

    assert selected == acquired
    assert LEDGER_ISSUE not in client.labels
    with pytest.raises(LedgerSuperseded, match="successor #170"):
        active_claims(client.list_protocol_candidates(LEDGER_ISSUE))


def test_supersede_race_loses_cleanly_without_poisoning_the_ledger() -> None:
    client = FakeComments(valid_successors={170})
    acquired = acquire_claim(
        client,
        request(issue=LEDGER_ISSUE, scope=("docs",)),
    )
    competitor = comment(
        50,
        claim_comment(request("other", issue=72, scope=("frontend",))),
        created_at="2026-08-21T00:00:01Z",
    )
    client.inject_before_next_ledger_post = competitor

    with pytest.raises(ClaimError, match="not observed"):
        supersede_ledger(
            client,
            170,
            "Fleet Coordinator",
            "coordinator",
            "race should reject",
            acquired.claim_id,
        )

    observed = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in observed} == {acquired.claim_id, "other"}


def test_supersede_label_failure_can_be_retried_without_reposting_event() -> None:
    client = FakeComments(valid_successors={170}, fail_remove_label=True)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    with pytest.raises(ClaimError, match="label remove failed"):
        supersede_ledger(
            client,
            170,
            "Fleet Coordinator",
            "coordinator",
            "reviewed successor ready",
            acquired.claim_id,
        )
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))
    assert LEDGER_ISSUE in client.labels

    client.fail_remove_label = False
    client.valid_successors.clear()  # The successor may already have accepted new claims.
    supersede_ledger(
        client,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed successor ready",
        acquired.claim_id,
    )

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count
    assert LEDGER_ISSUE not in client.labels


def test_supersede_refuses_an_unverified_successor_before_posting() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    with pytest.raises(ClaimUnavailable, match="open, empty, collaborator-locked"):
        supersede_ledger(
            client,
            999999,
            "Fleet Coordinator",
            "coordinator",
            "invalid successor",
            acquired.claim_id,
        )

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_supersede_requires_a_higher_numbered_successor() -> None:
    client = FakeComments(valid_successors={70})
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    with pytest.raises(ClaimError, match="greater than the current ledger"):
        supersede_comment(
            acquired,
            70,
            "Fleet Coordinator",
            "coordinator",
            "invalid rollover",
        )
    with pytest.raises(ClaimUnavailable, match="greater than the current ledger"):
        supersede_ledger(
            client,
            70,
            "Fleet Coordinator",
            "coordinator",
            "invalid rollover",
            acquired.claim_id,
        )

    with pytest.raises(InvalidClaimMarker, match="greater than the current ledger"):
        parse_claim_event(
            comment(
                2,
                marker(
                    {
                        "action": "supersede",
                        "agent": "Fleet Coordinator",
                        "claim_comment_id": acquired.comment.identifier,
                        "claim_id": acquired.claim_id,
                        "issue": LEDGER_ISSUE,
                        "reason": "invalid rollover",
                        "role": "coordinator",
                        "successor_issue": 70,
                    }
                ),
            )
        )

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_scope_overlap_is_repository_wide_and_path_aware() -> None:
    left = request(issue=71, scope=("frontend/src",))
    nested = request("claim-b", issue=72, scope=("frontend/src/lib/player.ts",))
    sibling = request("claim-c", issue=73, scope=("frontend/tests",))

    assert not claims_conflict(left, nested)
    assert protocol.claims_overlap(left, nested)
    assert not claims_conflict(left, sibling)
    assert not protocol.claims_overlap(left, sibling)


def test_comma_joined_scope_marker_is_read_as_distinct_paths() -> None:
    parsed = parse_claim_event(
        comment(
            1,
            marker(
                {
                    "action": "claim",
                    "agent": "Codex Sol",
                    "base": BASE,
                    "branch": "codex/issue-71-claims",
                    "claim_id": "claim-a",
                    "issue": 71,
                    "role": "builder",
                    "scope": [
                        "docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py"
                    ],
                }
            ),
        )
    )

    assert isinstance(parsed, ActiveClaim)
    assert parsed.scope == (
        "docs/PRODUCT.md",
        "src/atelier2/adapters/dbos/run_transitions.py",
    )


def test_comma_joined_scope_on_another_issue_is_an_overlap_note_not_a_refusal() -> None:
    incumbent = comment(
        1,
        marker(
            {
                "action": "claim",
                "agent": "Codex Sol",
                "base": BASE,
                "branch": "codex/issue-72-claims",
                "claim_id": "joined",
                "issue": 72,
                "role": "builder",
                "scope": [
                    "docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py"
                ],
            }
        ),
    )
    client = FakeComments({LEDGER_ISSUE: [incumbent]}, {72})

    acquired = acquire_claim(
        client,
        request("challenger", "Grok 4.6", issue=73, scope=("docs/PRODUCT.md",)),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"joined", acquired.claim_id}
    assert [claim.claim_id for claim in protocol.overlapping_claims(standing, acquired)] == [
        "joined"
    ]


def test_comma_joined_scope_with_spaces_equals_repeated_entries() -> None:
    parsed = parse_claim_event(
        comment(
            1,
            marker(
                {
                    "action": "claim",
                    "agent": "Codex Sol",
                    "base": BASE,
                    "branch": "codex/issue-71-claims",
                    "claim_id": "claim-a",
                    "issue": 71,
                    "role": "builder",
                    "scope": ["docs/PRODUCT.md, src/widget.py"],
                }
            ),
        )
    )

    assert isinstance(parsed, ActiveClaim)
    assert parsed.scope == ("docs/PRODUCT.md", "src/widget.py")


@pytest.mark.parametrize(
    "scope",
    [
        ["docs/PRODUCT.md,"],
        [",src/widget.py"],
        ["docs/PRODUCT.md,,src/widget.py"],
        [" docs/PRODUCT.md"],
        ["docs/PRODUCT.md "],
    ],
)
def test_comma_joined_scope_refuses_empty_or_padded_entries(scope: list[str]) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "codex/issue-71-claims",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": scope,
    }

    with pytest.raises(InvalidClaimMarker, match="canonical bounded paths"):
        parse_claim_event(comment(1, marker(payload)))


@pytest.mark.parametrize(
    ("right", "expected"),
    [
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-a", scope=("other",)),
            True,
            id="same-lane-disjoint-scope-still-conflicts",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("shared/file.py",)),
            False,
            id="different-lanes-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("other",)),
            False,
            id="different-lanes-disjoint-scope-no-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("shared/file.py",)),
            False,
            id="lane-and-issue-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("other",)),
            False,
            id="lane-and-issue-disjoint-scope-no-conflict",
        ),
    ],
)
def test_lane_and_issue_conflict_matrix(right: ClaimRequest, expected: bool) -> None:
    left = request(lane=True, branch="docs/lane-a", scope=("shared",))
    assert claims_conflict(left, right) == expected


def test_status_scope_index_never_rescans_scope_pairs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claims: list[ActiveClaim] = []
    for claim_index in range(50):
        parsed = parse_claim_event(
            comment(
                claim_index + 1,
                claim_comment(
                    request(
                        f"claim-{claim_index}",
                        issue=claim_index + 100,
                        scope=tuple(
                            f"area-{claim_index}/path-{scope_index}"
                            for scope_index in range(32)
                        ),
                    )
                ),
                created_at="2026-08-21T00:00:00Z",
            )
        )
        assert isinstance(parsed, ActiveClaim)
        claims.append(parsed)

    def scope_pair_scan(*args, **kwargs):
        pytest.fail("status must use its single scope index")

    monkeypatch.setattr(protocol, "claims_conflict", scope_pair_scan)

    assert _status(tuple(claims), None) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 50
    assert _status(tuple(claims), 100) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 1


def test_existing_scope_on_another_issue_is_posted_as_an_overlap() -> None:
    incumbent = comment(1, claim_comment(request(issue=71, scope=("shared",))))
    client = FakeComments({LEDGER_ISSUE: [incumbent]}, {71})

    acquired = acquire_claim(
        client,
        request("challenger", "Grok 4.6", issue=72, scope=("shared/file.py",)),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"claim-a", acquired.claim_id}


def test_rescope_adds_a_path_without_changing_claim_id_or_base() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        ("src/new.py",),
        (),
        acquired.claim_id,
    )

    assert updated.claim_id == acquired.claim_id
    assert updated.base == acquired.base
    assert updated.branch == acquired.branch
    assert updated.agent == acquired.agent
    assert updated.scope == ("src/widget.py", "src/new.py")
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.scope for claim in standing] == [("src/widget.py", "src/new.py")]


def test_rescope_drop_and_add_replace_paths_atomically() -> None:
    client = FakeComments()
    acquired = acquire_claim(
        client, request(issue=72, scope=("src/old.py", "src/keep.py"))
    )

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        ("src/new.py",),
        ("src/old.py",),
        acquired.claim_id,
    )

    assert updated.claim_id == acquired.claim_id
    assert updated.scope == ("src/keep.py", "src/new.py")


def test_rescope_adds_a_path_held_by_another_issue() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    other = acquire_claim(
        client, request("claim-b", "Grok 4.6", issue=73, scope=("docs/PRODUCT.md",))
    )

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        ("docs/PRODUCT.md",),
        (),
        "claim-a",
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert updated.scope == ("src/widget.py", "docs/PRODUCT.md")
    assert scopes["claim-a"] == ("src/widget.py", "docs/PRODUCT.md")
    assert scopes[other.claim_id] == ("docs/PRODUCT.md",)


def test_rescope_drops_an_unrelated_path_when_the_remainder_already_overlaps() -> None:
    client = _claims_client(
        request(issue=72, scope=("docs/product", "tests/tooling")),
        request("claim-b", "Grok 4.6", issue=73, scope=("docs/product",)),
    )

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        (),
        ("tests/tooling",),
        "claim-a",
    )

    assert updated.claim_id == "claim-a"
    assert updated.scope == ("docs/product",)
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert scopes["claim-a"] == ("docs/product",)
    assert scopes["claim-b"] == ("docs/product",)


def test_rescope_adds_a_held_path_when_the_remainder_already_overlaps() -> None:
    client = _claims_client(
        request(issue=72, scope=("docs/product", "tests/tooling")),
        request(
            "claim-b",
            "Grok 4.6",
            issue=73,
            scope=("docs/product", "src/held.py"),
        ),
    )

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        ("src/held.py",),
        (),
        "claim-a",
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert updated.scope == ("docs/product", "tests/tooling", "src/held.py")
    assert scopes["claim-a"] == ("docs/product", "tests/tooling", "src/held.py")
    assert scopes["claim-b"] == ("docs/product", "src/held.py")


def test_rescope_refuses_dropping_a_path_it_does_not_hold() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    with pytest.raises(ClaimUnavailable, match="cannot drop 'docs/PRODUCT.md'"):
        rescope_claim(
            client,
            IssueIdentity(72),
            "Codex Sol",
            (),
            ("docs/PRODUCT.md",),
            "claim-a",
        )


def test_rescope_refuses_an_empty_or_unchanged_scope() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    with pytest.raises(ClaimUnavailable, match="non-empty scope"):
        rescope_claim(
            client,
            IssueIdentity(72),
            "Codex Sol",
            (),
            ("src/widget.py",),
            "claim-a",
        )
    with pytest.raises(ClaimUnavailable, match="does not change"):
        rescope_claim(
            client,
            IssueIdentity(72),
            "Codex Sol",
            ("src/widget.py",),
            (),
            "claim-a",
        )


def test_rescope_refuses_a_foreign_agent() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    with pytest.raises(ClaimUnavailable, match="only the original claimant"):
        rescope_claim(
            client,
            IssueIdentity(72),
            "Grok 4.6",
            ("src/new.py",),
            (),
            "claim-a",
        )


@pytest.mark.parametrize(
    ("competitor_id", "created_at"),
    [
        pytest.param(
            "earlier",
            "2026-08-20T23:59:59Z",
            id="older-competitor",
        ),
        pytest.param(
            "later",
            "2026-08-21T00:00:50Z",
            id="newer-competitor",
        ),
    ],
)
def test_rescope_keeps_an_added_path_that_another_claim_also_holds(
    competitor_id: str, created_at: str
) -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    competitor = comment(
        50,
        claim_comment(
            request(competitor_id, "Grok 4.6", issue=73, scope=("src/new.py",))
        ),
        created_at=created_at,
    )
    client.inject_before_next_ledger_post = competitor

    updated = rescope_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        ("src/new.py",),
        (),
        "claim-a",
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert set(scopes) == {"claim-a", competitor_id}
    assert updated.scope == ("src/widget.py", "src/new.py")
    assert scopes["claim-a"] == ("src/widget.py", "src/new.py")
    assert scopes[competitor_id] == ("src/new.py",)


def test_who_reports_the_claim_holding_a_path() -> None:
    first = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("docs/PRODUCT.md",))))
    )
    second = parse_claim_event(
        comment(2, claim_comment(request("claim-b", issue=73, scope=("src/widget.py",))))
    )
    assert first is not None and second is not None
    claims = (first, second)

    assert claims_holding_path(claims, "docs/PRODUCT.md") == (first,)
    assert claims_holding_path(claims, "src/widget.py") == (second,)
    assert claims_holding_path(claims, "README.md") == ()


def test_who_reports_a_directory_claim_for_a_descendant_path() -> None:
    parent = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("docs",))))
    )
    assert parent is not None

    assert claims_holding_path((parent,), "docs/decisions/one.md") == (parent,)


def test_who_refuses_a_comma_joined_path() -> None:
    with pytest.raises(ClaimError, match="single repository-relative path"):
        claims_holding_path((), "docs/PRODUCT.md,src/widget.py")


def test_disjoint_issues_can_be_claimed_and_are_projected() -> None:
    client = FakeComments()

    first = acquire_claim(client, request(issue=72, scope=("frontend",)))
    second = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src",)),
    )

    assert {first.identity.issue, second.identity.issue} == {72, 73}
    assert client.labels == {72, 73}
    assert "🔒 **Claimed**" in client.comments[72][0].body
    assert "🔒 **Claimed**" in client.comments[73][0].body


def test_owning_issue_projection_uses_the_configured_ledger_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request(issue=72))))
    assert isinstance(claimed, ActiveClaim)
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)

    projection = issue_claim._active_projection(claimed)

    assert "ledger=170" in projection.partition("\n")[0]
    assert "ledger=71" not in projection.partition("\n")[0]
    assert claim_label() == "agent-claim:active:170"


def test_same_issue_refuses_a_second_claim_even_with_disjoint_scope() -> None:
    incumbent = comment(1, claim_comment(request(issue=72, scope=("frontend",))))
    client = FakeComments({LEDGER_ISSUE: [incumbent]}, {72})

    with pytest.raises(ClaimUnavailable, match="issue #72"):
        acquire_claim(
            client,
            request("claim-b", "Grok 4.6", issue=72, scope=("src",)),
        )


def test_same_lane_refuses_a_second_claim_even_with_disjoint_scope() -> None:
    client = FakeComments()
    acquire_claim(client, request(lane=True, branch="docs/lane-a", scope=("frontend",)))

    with pytest.raises(ClaimUnavailable, match="lane 'docs/lane-a'"):
        acquire_claim(
            client,
            request("claim-b", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("src",)),
        )


def test_lane_and_issue_claim_with_overlapping_scope_both_stay_live() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("shared",)))

    lane = acquire_claim(
        client,
        request(
            "claim-b", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("shared/file.py",)
        ),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"claim-a", lane.claim_id}


def test_acquire_claim_refuses_reusing_an_active_claim_id_before_posting() -> None:
    incumbent = comment(1, claim_comment(request("claim-a", issue=72, scope=("old",))))
    client = FakeComments({LEDGER_ISSUE: [incumbent]}, {72})

    with pytest.raises(ClaimUnavailable, match="claim id 'claim-a' is already"):
        acquire_claim(
            client,
            request("claim-a", "Codex Sol", issue=72, scope=("old", "new")),
        )

    assert client.comments[LEDGER_ISSUE] == [incumbent]


def test_acquire_claim_refuses_reusing_a_released_claim_id_before_posting() -> None:
    claimed_body = claim_comment(request("claim-a", issue=72))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert claimed is not None
    entries = [comment(1, claimed_body), comment(2, release_event(claimed))]
    client = FakeComments({LEDGER_ISSUE: list(entries)})

    with pytest.raises(ClaimUnavailable, match="claim id 'claim-a' is already"):
        acquire_claim(
            client,
            request("claim-a", "Grok 4.6", issue=73, scope=("fresh",)),
        )

    assert client.comments[LEDGER_ISSUE] == entries


def test_acquire_claim_translates_a_same_claim_id_post_race_into_a_clear_error() -> None:
    client = FakeComments()
    client.inject_after_next_ledger_post = comment(
        2,
        claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("elsewhere",))),
    )

    with pytest.raises(ClaimUnavailable, match="claim race detected"):
        acquire_claim(client, request("claim-a", "Codex Sol", issue=72, scope=("mine",)))


def test_cross_issue_scope_race_keeps_both_overlapping_claims() -> None:
    client = FakeComments()
    earlier = comment(
        100,
        claim_comment(
            request("earlier", "Grok 4.6", issue=72, scope=("shared/file.py",))
        ),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    later = acquire_claim(
        client,
        request("later", "Codex Sol", issue=73, scope=("shared",)),
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert {claim.claim_id for claim in standing} == {"earlier", later.claim_id}
    assert client.labels == {73}


def test_release_removes_projection_only_after_claim_is_gone() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=72))
    projection_id = client.comments[72][0].identifier

    released = release_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        "builder",
        "landed",
        acquired.claim_id,
    )

    assert released.claim_id == "claim-a"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()
    assert client.labels == set()
    assert len(client.comments[72]) == 1
    assert client.comments[72][0].identifier == projection_id
    assert "🔓 **Unclaimed** · landed" in client.comments[72][0].body


def test_release_reconciliation_keeps_a_successor_claim_projection_active() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=72, scope=("old",)))
    successor = comment(
        4,
        claim_comment(request("successor", "Grok 4.6", issue=72, scope=("new",))),
    )
    client.inject_during_next_remove = successor

    release_claim(
        client,
        IssueIdentity(72),
        "Codex Sol",
        "builder",
        "landed",
        acquired.claim_id,
    )

    projection = client.comments[72][0]
    assert len(client.comments[72]) == 1
    assert "🔒 **Claimed**" in projection.body
    assert "Grok 4.6" in projection.body
    assert "codex/issue-72-claims" in projection.body
    assert "🔓 **Unclaimed**" not in projection.body
    assert client.labels == {72}


def test_projection_is_minimal_and_reuses_one_trusted_comment() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=72, scope=("private/path",)))
    first_projection = client.comments[72][0]
    duplicate = replace(first_projection, identifier=first_projection.identifier + 100)
    client.comments[72].append(duplicate)

    client.upsert_projection(72, issue_claim._active_projection(acquired))

    assert len(client.comments[72]) == 1
    projection = client.comments[72][0]
    assert projection.identifier == first_projection.identifier
    assert "private/path" not in projection.body
    assert acquired.base not in projection.body
    assert acquired.branch in projection.body


def test_reconcile_does_not_create_projection_for_never_claimed_issue() -> None:
    client = FakeComments()

    reconcile_issue_label(client, 999)

    assert client.comments.get(999, []) == []
    assert client.labels == set()


def test_claim_labels_are_isolated_by_ledger_generation() -> None:
    assert claim_label(71) == "agent-claim:active:71"
    assert claim_label(170) == "agent-claim:active:170"
    assert claim_label(71) != claim_label(170)


def test_successor_adopts_old_projection_but_old_helper_cannot_mutate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    old_projection = comment(1, issue_claim._unclaimed_projection())
    old_duplicate = replace(old_projection, identifier=2)
    client = FakeComments({72: [old_projection, old_duplicate]})

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    successor_body = issue_claim._active_projection(
        ActiveClaim(
            IssueIdentity(72),
            "successor",
            "Codex Sol",
            "builder",
            BASE,
            "codex/issue-72-claims",
            ("scripts/issue_claim.py",),
            comment(3, claim_comment(request("successor", issue=72))),
        )
    )
    assert client.upsert_projection(72, successor_body, adopt_stale=True)
    assert len(client.comments[72]) == 1
    assert "ledger=170" in client.comments[72][0].body.partition("\n")[0]

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    with pytest.raises(ClaimError, match="newer ledger generation"):
        client.upsert_projection(72, issue_claim._unclaimed_projection(), create=False)
    assert len(client.comments[72]) == 1
    assert "ledger=170" in client.comments[72][0].body.partition("\n")[0]


def test_release_refuses_foreign_actor_without_explicit_override() -> None:
    client = FakeComments()
    acquired = acquire_claim(client, request(issue=72))

    with pytest.raises(ClaimUnavailable, match="original claimant"):
        release_claim(
            client,
            IssueIdentity(72),
            "Other",
            "builder",
            "takeover",
            acquired.claim_id,
        )


@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_release_claim_omitted_id_posts_landed_using_selected_role(role: str) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role=role, branch="lane-72", scope=("src",))
    )

    released = release_claim(client, IssueIdentity(72), "Ada", None, None, None, branch="lane-72")
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == role
    assert posted.reason == "landed"
    assert posted.agent == "Ada"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_release_claim_omitted_id_releases_when_foreign_peer_exists_on_issue() -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
        request(
            "theirs",
            "Other",
            issue=72,
            role="builder",
            branch="other-lane",
            scope=("docs",),
        ),
    )

    released = release_claim(client, IssueIdentity(72), "Ada", None, None, None, branch="lane-72")
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert [claim.claim_id for claim in standing] == ["theirs"]
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "landed"


def test_release_claim_omitted_id_uniqueness_is_issue_scoped() -> None:
    client = _claims_client(
        request(
            "on-72", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)
        ),
        request(
            "on-73", "Ada", issue=73, role="reviewer", branch="lane-72", scope=("docs",)
        ),
    )

    released = release_claim(client, IssueIdentity(72), "Ada", None, None, None, branch="lane-72")
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))

    assert released.claim_id == "on-72"
    assert [claim.claim_id for claim in standing] == ["on-73"]


@pytest.mark.parametrize(
    ("agent", "branch", "standing"),
    [
        (
            "Other",
            "lane-72",
            (
                request(
                    "mine",
                    "Ada",
                    issue=72,
                    role="reviewer",
                    branch="lane-72",
                    scope=("src",),
                ),
            ),
        ),
        (
            "Ada",
            "other-lane",
            (
                request(
                    "mine",
                    "Ada",
                    issue=72,
                    role="reviewer",
                    branch="lane-72",
                    scope=("src",),
                ),
            ),
        ),
        (
            "Ada",
            "lane-72",
            (
                request(
                    "one", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)
                ),
                request(
                    "two", "Ada", issue=72, role="builder", branch="lane-72", scope=("docs",)
                ),
            ),
        ),
    ],
)
def test_release_claim_omitted_id_fails_closed_for_wrong_agent_branch_or_two_matches(
    agent: str, branch: str, standing: tuple[ClaimRequest, ...]
) -> None:
    client = _claims_client(*standing)
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    with pytest.raises(ClaimUnavailable, match="pass --claim-id") as raised:
        release_claim(client, IssueIdentity(72), agent, None, None, None, branch=branch)

    assert "conflicting claims" not in str(raised.value)
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_release_claim_explicit_id_ignores_branch() -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )

    released = release_claim(
        client, IssueIdentity(72), "Ada", None, None, "mine", branch="other-lane"
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "landed"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_release_claim_omitted_id_requires_branch_and_does_not_call_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unused(arguments: list[str]) -> str:
        pytest.fail("release_claim must not call git")

    monkeypatch.setattr(checkout, "_git_output", unused)
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )

    with pytest.raises(ClaimUnavailable, match="current branch"):
        release_claim(client, IssueIdentity(72), "Ada", None, None, None)
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == 1

    released = release_claim(client, IssueIdentity(72), "Ada", None, None, None, branch="lane-72")
    assert released.claim_id == "mine"


@pytest.mark.parametrize(
    ("role", "reason"),
    [
        ("builder", "takeover"),
        ("coordinator", None),
        (None, None),
        (None, "takeover"),
    ],
)
def test_release_claim_override_fails_before_ledger_without_role_and_reason(
    role: str | None, reason: str | None
) -> None:
    client = FakeComments()
    expected = "--role coordinator" if role != "coordinator" else "--reason"

    with pytest.raises(ClaimUnavailable, match=expected):
        release_claim(
            client,
            IssueIdentity(72),
            "Ada",
            role,
            reason,
            "mine",
            coordinator_override=True,
        )

    assert client.comments == {}


def test_label_reconciliation_heals_claim_posted_during_release_remove() -> None:
    old_claim_body = claim_comment(request("old", issue=72, scope=("old",)))
    old_claim = parse_claim_event(comment(1, old_claim_body))
    assert old_claim is not None
    release_body = release_event(old_claim)
    new_claim_comment = comment(
        3,
        claim_comment(request("new", issue=72, scope=("new",))),
    )
    client = FakeComments(
        {LEDGER_ISSUE: [comment(1, old_claim_body), comment(2, release_body)]},
        {72},
        inject_during_next_remove=new_claim_comment,
    )

    reconcile_issue_label(client, 72)

    assert [
        claim.claim_id
        for claim in active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    ] == ["new"]
    assert client.labels == {72}


def test_label_failure_is_loud_while_comment_truth_remains() -> None:
    client = FakeComments(fail_add_label=True)

    with pytest.raises(ClaimError, match="label add failed"):
        acquire_claim(client, request(issue=72))

    assert [
        claim.identity.issue
        for claim in active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    ] == [72]


def test_reconcile_all_repairs_active_and_stale_labels() -> None:
    active = comment(1, claim_comment(request(issue=72)))
    client = FakeComments({LEDGER_ISSUE: [active]}, {73})

    observed = reconcile_all_labels(client)

    assert observed == (72,)
    assert client.labels == {72}


def test_reconcile_all_labels_ignores_lane_claims_on_a_mixed_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane claim owns no GitHub issue, so reconcile must never label or project
    it — only the issue claim on the same mixed ledger keeps its usual behaviour."""
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("backend",)))
    acquire_claim(
        client,
        request("lane-claim", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("docs",)),
    )

    lane_calls: list[tuple[str, object]] = []
    original_add_label = client.add_label
    original_remove_label = client.remove_label
    original_upsert_projection = client.upsert_projection

    def add_label(issue: object, label: str) -> None:
        if issue != 72:
            lane_calls.append(("add_label", issue))
        return original_add_label(issue, label)

    def remove_label(issue: object, label: str) -> None:
        if issue != 72:
            lane_calls.append(("remove_label", issue))
        return original_remove_label(issue, label)

    def upsert_projection(
        issue: object, body: str, *, create: bool = True, adopt_stale: bool = False
    ) -> bool:
        if issue != 72:
            lane_calls.append(("upsert_projection", issue))
        return original_upsert_projection(issue, body, create=create, adopt_stale=adopt_stale)

    monkeypatch.setattr(client, "add_label", add_label)
    monkeypatch.setattr(client, "remove_label", remove_label)
    monkeypatch.setattr(client, "upsert_projection", upsert_projection)

    observed = reconcile_all_labels(client)

    assert observed == (72,)
    assert client.labels == {72}
    assert lane_calls == []


def test_reconcile_repairs_a_duplicate_claim_id_and_restores_strict_reads() -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    client = FakeComments({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})

    with pytest.raises(InvalidClaimMarker, match="was reused"):
        active_claims(client.list_protocol_candidates(LEDGER_ISSUE))

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1,), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.comment.identifier for claim in survivors] == [2]
    assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][0])
    assert "SUPERSEDED" in client.comments[LEDGER_ISSUE][0].body
    assert "claim-a" in client.comments[LEDGER_ISSUE][0].body

    assert reconcile_all_labels(client) == (72,)
    assert client.labels == {72}


@pytest.mark.parametrize(
    ("older_agent", "release_before_reuse", "newer_agent", "expect_repaired"),
    [
        pytest.param("Codex Sol", False, "Codex Sol", True, id="same_agent_unreleased"),
        pytest.param("Codex Sol", True, "Grok 4.6", True, id="released_id_reuse"),
        pytest.param("Codex Sol", False, "Grok 4.6", False, id="cross_agent_unreleased"),
    ],
)
def test_repair_duplicate_claims_only_auto_resolves_the_safe_cases(
    older_agent: str,
    release_before_reuse: bool,
    newer_agent: str,
    expect_repaired: bool,
) -> None:
    older_body = claim_comment(request("claim-a", older_agent, issue=72, scope=("old",)))
    older_claim = parse_claim_event(comment(1, older_body))
    assert older_claim is not None
    entries = [comment(1, older_body)]
    if release_before_reuse:
        entries.append(comment(2, release_event(older_claim)))
    entries.append(
        comment(
            len(entries) + 1,
            claim_comment(request("claim-a", newer_agent, issue=72, scope=("new",))),
        )
    )
    client = FakeComments({LEDGER_ISSUE: entries})

    if not expect_repaired:
        before = list(client.comments[LEDGER_ISSUE])
        with pytest.raises(DuplicateClaimConflict, match="claim id 'claim-a'"):
            repair_duplicate_claims(client)
        assert client.comments[LEDGER_ISSUE] == before
        return

    repaired = repair_duplicate_claims(client)

    expected_superseded_ids = (1, 2) if release_before_reuse else (1,)
    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a",
            superseded_comment_ids=expected_superseded_ids,
            survivor_comment_id=entries[-1].identifier,
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(claim.claim_id, claim.agent) for claim in survivors] == [("claim-a", newer_agent)]
    assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][0])
    if release_before_reuse:
        assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][1])


def test_repair_duplicate_claims_ignores_an_inert_ledger_supersede_as_a_release() -> None:
    """A `LedgerSupersede` event only really terminates a claim when
    `_apply_terminal_event` honors it (coordinator role, right ledger issue, right
    claim comment id, and it was the ledger's only active claim). One that misses
    any of those conditions is inert and must not be read as a release by repair,
    even though it parses cleanly and names the right claim id."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=LEDGER_ISSUE)))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    other_active_claim = comment(
        2, claim_comment(request("other", "Codex Sol", issue=72))
    )
    inert_supersede = comment(
        3,
        supersede_comment(
            original_claim, 170, "Fleet Coordinator", "coordinator", "rollover"
        ),
    )
    reused = comment(
        4, claim_comment(request("claim-a", "Grok 4.6", issue=LEDGER_ISSUE, scope=("new",)))
    )
    client = FakeComments(
        {LEDGER_ISSUE: [original, other_active_claim, inert_supersede, reused]}
    )
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflict, match="claim id 'claim-a'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_attributes_a_late_release_to_the_original_occurrence() -> None:
    """`claim x (A) -> claim x (B, duplicate) -> release x (A)`: the release names
    the original claimant and must close the FIRST occurrence, letting the safe
    already-released repair apply, regardless of which agent posted the duplicate."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    duplicate = comment(
        2, claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("new",)))
    )
    late_release = comment(3, release_event(original_claim))
    client = FakeComments({LEDGER_ISSUE: [original, duplicate, late_release]})

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1, 3), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(claim.identity.issue, claim.agent) for claim in survivors] == [(73, "Grok 4.6")]


@pytest.mark.parametrize(
    "second_release_body",
    [
        pytest.param(
            lambda claim: release_comment(claim, claim.agent, claim.role, "landed retry"),
            id="release_retry",
        ),
        pytest.param(
            lambda claim: release_comment(
                claim,
                "Fleet Coordinator",
                "coordinator",
                "verified handoff",
                coordinator_override=True,
            ),
            id="claimant_then_coordinator_override",
        ),
    ],
)
def test_repair_duplicate_claims_neutralizes_every_honored_terminal_comment(
    second_release_body,
) -> None:
    """A claim id can legitimately carry more than one honored terminal comment (an
    idempotent release retry, or a claimant release followed by a coordinator
    override). Repair must neutralize ALL of them, not only the one whose pop
    actually emptied `active` — otherwise the surviving terminal comment is left
    referencing a claim that repair just made invisible, and the ledger stays dead."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72)))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    first_release = comment(2, release_event(original_claim))
    second_release = comment(3, second_release_body(original_claim))
    reused = comment(
        4, claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("fresh",)))
    )
    client = FakeComments(
        {LEDGER_ISSUE: [original, first_release, second_release, reused]}
    )

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1, 2, 3), survivor_comment_id=4
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(claim.identity.issue, claim.agent) for claim in survivors] == [(73, "Grok 4.6")]
    # A truly clean repair: nothing left for a second reconcile pass to find or fix.
    assert repair_duplicate_claims(client) == ()


def test_repair_duplicate_claims_validates_every_lifecycle_before_writing_any() -> None:
    first = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    middle = comment(2, claim_comment(request("claim-a", "Grok 4.6", issue=72, scope=("mid",))))
    newest = comment(3, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",))))
    client = FakeComments({LEDGER_ISSUE: [first, middle, newest]})
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflict, match="claim id 'claim-a'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_leaves_other_duplicate_ids_untouched_when_one_conflicts() -> None:
    safe_older = comment(
        1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    )
    safe_newer = comment(
        2, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    )
    conflict_older = comment(
        3, claim_comment(request("claim-b", "Codex Sol", issue=73, scope=("x",)))
    )
    conflict_newer = comment(
        4, claim_comment(request("claim-b", "Grok 4.6", issue=73, scope=("y",)))
    )
    client = FakeComments(
        {LEDGER_ISSUE: [safe_older, safe_newer, conflict_older, conflict_newer]}
    )
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflict, match="claim id 'claim-b'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_same_agent_cross_issue_keeps_only_the_newer_lane() -> None:
    """Documented tradeoff: same-agent keep-newest is not scoped to one issue. A
    same-agent duplicate spanning two issues still only keeps the newer issue's
    lane; the older issue's still-active claim is silently ended, not preserved."""
    older = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    newer = comment(2, claim_comment(request("claim-a", "Codex Sol", issue=73, scope=("new",))))
    client = FakeComments({LEDGER_ISSUE: [older, newer]}, {72, 73})

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1,), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(claim.identity.issue, claim.claim_id) for claim in survivors] == [(73, "claim-a")]

    assert reconcile_all_labels(client) == (73,)
    assert client.labels == {73}


def test_stale_reconcile_removes_label_when_supersede_wins_midflight() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = comment(
        2,
        supersede_comment(
            claimed,
            170,
            "Fleet Coordinator",
            "coordinator",
            "reviewed rollover ready",
        ),
    )
    client = FakeComments(
        {LEDGER_ISSUE: [comment(1, claimed_body)]},
        inject_during_next_add=frozen,
    )

    with pytest.raises(LedgerSuperseded):
        reconcile_issue_label(client, LEDGER_ISSUE)

    assert LEDGER_ISSUE not in client.labels
    with pytest.raises(LedgerSuperseded):
        active_claims(client.list_protocol_candidates(LEDGER_ISSUE))


def test_old_reconcile_clears_only_its_generation_label_after_freeze() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = supersede_comment(
        claimed,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed rollover ready",
    )
    client = FakeComments(
        {LEDGER_ISSUE: [comment(1, claimed_body), comment(2, frozen)]},
        {LEDGER_ISSUE, 72},
        {claim_label(170): {170}},
    )

    with pytest.raises(LedgerSuperseded):
        reconcile_all_labels(client)
    assert client.labels == set()
    assert client.other_labels == {claim_label(170): {170}}

    client.labels.update({LEDGER_ISSUE, 170})
    with pytest.raises(LedgerSuperseded):
        reconcile_issue_label(client, 170)
    assert client.labels == {LEDGER_ISSUE}
    assert client.other_labels == {claim_label(170): {170}}


def test_paused_old_release_fails_frozen_without_mutating_successor_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    client = FakeComments(valid_successors={170})
    old_claim = acquire_claim(client, request("old", issue=72, scope=("old",)))
    client.post_comment(
        71,
        release_comment(old_claim, "Codex Sol", "builder", "landed"),
    )
    rollover = acquire_claim(
        client,
        request("rollover", issue=71, scope=("docs/COORDINATION.md",)),
    )
    supersede_ledger(
        client,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed successor ready",
        rollover.claim_id,
    )

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    acquire_claim(
        client,
        request("successor", "Grok 4.6", issue=72, scope=("new",)),
    )
    successor_projection = client.comments[72][0].body
    client.other_labels[claim_label(170)] = set(client.labels)
    client.labels.clear()

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    with pytest.raises(LedgerSuperseded, match="successor #170"):
        reconcile_issue_label(client, 72)

    assert client.comments[72][0].body == successor_projection
    assert client.other_labels == {claim_label(170): {72}}


def test_status_reports_repository_scope_overlaps_as_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("shared",))))
    )
    second = parse_claim_event(
        comment(
            2,
            claim_comment(request("claim-b", issue=73, scope=("shared/file.py",))),
        )
    )
    assert first is not None and second is not None

    exit_code = _status((first, second), None)

    assert exit_code == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered
    assert "overlaps issue #73 (claim-b)" in rendered
    assert "overlaps issue #72 (claim-a)" in rendered
    assert _status((first, second), 72) == 0
    issue_rendered = capsys.readouterr().out
    assert issue_rendered.count("CLAIMED") == 2
    assert "overlaps issue #73 (claim-b)" in issue_rendered


def test_status_notes_a_scope_that_is_claimed_after_its_descendant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    descendant = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("shared/file.py",))))
    )
    parent = parse_claim_event(
        comment(2, claim_comment(request("claim-b", issue=73, scope=("shared",))))
    )
    assert descendant is not None and parent is not None

    assert _status((descendant, parent), None) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered


def test_github_comment_reader_accepts_paginated_json_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_rows = [
        {
            "id": 10,
            "created_at": "2026-08-21T01:00:00Z",
            "updated_at": "2026-08-21T01:00:00Z",
            "body": "ordinary prose",
            "author_association": "OWNER",
            "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-10",
        },
        {
            "id": 11,
            "created_at": "2026-08-21T02:00:00Z",
            "updated_at": "2026-08-21T02:00:00Z",
            "body": "more ordinary prose",
            "author_association": "MEMBER",
            "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-11",
        },
    ]
    protocol_row = {
        "id": 12,
        "created_at": "2026-08-21T03:00:00Z",
        "updated_at": "2026-08-21T03:00:00Z",
        "body": claim_comment(request()),
        "author_association": "OWNER",
        "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-12",
    }
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(github, "COMMENTS_PER_PAGE", 2)

    def page(arguments: list[str]) -> str:
        endpoint = arguments[1]
        rows = ordinary_rows if "page=1" in endpoint else [protocol_row]
        return "\n".join(map(json.dumps, rows))

    monkeypatch.setattr(client, "_run", page)

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [12]
    assert observed[0].body == protocol_row["body"]


def _comment_row(identifier: int, body: str = "ordinary prose") -> dict[str, object]:
    stamp = f"2026-08-21T{identifier:02d}:00:00Z"
    return {
        "id": identifier,
        "created_at": stamp,
        "updated_at": stamp,
        "body": body,
        "author_association": "OWNER",
        "html_url": (
            f"https://github.com/example/agent-claim/issues/71#issuecomment-{identifier}"
        ),
    }


def test_github_comment_reader_accepts_pretty_and_ansi_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _comment_row(10, claim_comment(request()))
    second = _comment_row(11, "ordinary prose")
    pretty = json.dumps(first, indent=2) + "\n" + json.dumps(second, indent=2)
    colored = f"\x1b[32m{pretty}\x1b[0m"
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(client, "_run", lambda arguments: colored)

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [10]
    assert observed[0].body == first["body"]


def test_github_comment_reader_accepts_concatenated_pretty_json_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _comment_row(10, claim_comment(request()))
    second = _comment_row(11, claim_comment(request("claim-b", issue=72)))
    raw = json.dumps(first, indent=2) + json.dumps(second, indent=2)
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(client, "_run", lambda arguments: raw)

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [10, 11]


def test_bounded_command_sets_github_quiet_environment() -> None:
    observed = issue_claim._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['NO_COLOR']); "
            "print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="env probe",
    )

    assert observed.splitlines() == ["1", "1"]


def test_repository_resolution_uses_github_quiet_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*arguments, **kwargs):
        command = arguments[0]
        observed["command"] = command
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            command, 0, "\x1b[32mowner/repository\x1b[0m\n", ""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _repository(None) == "owner/repository"
    assert observed["command"][0] == "gh"
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["NO_COLOR"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_fake_and_github_adapters_expose_only_common_protocol_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = comment(1, claim_comment(request()))
    prose = comment(2, "ordinary prose")
    untrusted = comment(3, claim_comment(request("untrusted")), association="NONE")
    fake = FakeComments({LEDGER_ISSUE: [trusted, prose, untrusted]})
    assert fake.list_protocol_candidates(LEDGER_ISSUE) == (trusted,)

    rows = [
        {
            "id": entry.identifier,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "body": entry.body,
            "author_association": entry.author_association,
            "html_url": entry.url,
        }
        for entry in (trusted, prose, untrusted)
    ]
    github = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(github, "_run", lambda arguments: "\n".join(map(json.dumps, rows)))

    assert github.list_protocol_candidates(LEDGER_ISSUE) == (trusted,)


def test_comment_size_is_bounded_before_any_adapter_post() -> None:
    widest_scope = tuple(f"p{index:03d}-" + "x" * 507 for index in range(256))

    with pytest.raises(ClaimError, match=str(MAX_COMMENT_BYTES)):
        claim_comment(request(scope=widest_scope))


def test_github_comment_body_uses_stdin_instead_of_process_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    observed: dict[str, object] = {}

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed["arguments"] = arguments
        observed["input"] = input_data
        return "https://github.com/example/agent-claim/issues/71#issuecomment-1"

    monkeypatch.setattr(client, "_run", run)
    body = claim_comment(request())

    client.post_comment(LEDGER_ISSUE, body)

    arguments = observed["arguments"]
    assert isinstance(arguments, list)
    assert body not in arguments
    assert arguments[-2:] == ["--body-file", "-"]
    assert observed["input"] == body.encode()


def test_github_projection_update_patches_one_comment_and_deletes_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    first = comment(10, issue_claim._unclaimed_projection())
    duplicate = replace(first, identifier=11)
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (first, duplicate))
    observed: list[tuple[list[str], bytes | None]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    monkeypatch.setattr(client, "_run", run)
    body = issue_claim._active_projection(
        ActiveClaim(
            IssueIdentity(72),
            "claim-a",
            "Codex Sol",
            "builder",
            BASE,
            "codex/issue-72-claims",
            ("scripts/issue_claim.py",),
            comment(9, claim_comment(request(issue=72))),
        )
    )

    assert client.upsert_projection(72, body)
    assert observed[0][0] == [
        "api",
        "--method",
        "PATCH",
        "repos/example/agent-claim/issues/comments/10",
        "--input",
        "-",
    ]
    assert observed[0][1] == json.dumps({"body": body}).encode("utf-8")
    assert observed[1] == (
        [
            "api",
            "--method",
            "DELETE",
            "repos/example/agent-claim/issues/comments/11",
        ],
        None,
    )


def test_github_projection_update_does_not_create_on_a_never_claimed_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(client, "_projection_comments", lambda issue: ())
    monkeypatch.setattr(
        client,
        "post_comment",
        lambda issue, body: pytest.fail("reconcile must not create a projection"),
    )

    assert not client.upsert_projection(999, issue_claim._unclaimed_projection(), create=False)


def test_github_successor_adopts_stale_projection_but_old_generation_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    stale = comment(10, issue_claim._unclaimed_projection())
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 171)
    future = comment(11, issue_claim._unclaimed_projection())
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (stale, future))
    observed: list[tuple[list[str], bytes | None]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    monkeypatch.setattr(client, "_run", run)
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    successor_body = issue_claim._unclaimed_projection()

    assert client.upsert_projection(72, successor_body, adopt_stale=True)
    assert observed == [
        (
            [
                "api",
                "--method",
                "PATCH",
                "repos/example/agent-claim/issues/comments/10",
                "--input",
                "-",
            ],
            json.dumps({"body": successor_body}).encode("utf-8"),
        )
    ]

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    successor = replace(stale, body=successor_body)
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (successor,))
    observed.clear()
    with pytest.raises(ClaimError, match="newer ledger generation"):
        client.upsert_projection(72, issue_claim._unclaimed_projection(), create=False)
    assert observed == []


def test_github_claimed_issue_query_is_scoped_to_this_ledger_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    observed: list[str] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        assert input_data is None
        observed.extend(arguments)
        return "72\n73"

    monkeypatch.setattr(client, "_run", run)

    assert client.list_claimed_issues() == (72, 73)
    assert (
        f"repos/example/agent-claim/issues?state=all&labels={claim_label()}&per_page=100"
        in observed
    )
    assert "--paginate" in observed


def test_github_successor_must_exist_open_empty_locked_and_not_be_a_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    valid = {
        "number": 170,
        "state": "open",
        "locked": True,
        "comments": 0,
        "is_pull_request": False,
    }
    monkeypatch.setattr(client, "_run", lambda arguments: json.dumps(valid))

    client.validate_successor(170)

    for key, value in (
        ("number", 999999),
        ("state", "closed"),
        ("locked", False),
        ("comments", 1),
        ("is_pull_request", True),
    ):
        invalid = {**valid, key: value}
        monkeypatch.setattr(client, "_run", lambda arguments, row=invalid: json.dumps(row))
        with pytest.raises(ClaimUnavailable, match="open, empty, collaborator-locked"):
            client.validate_successor(170)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps([]),
        json.dumps({"id": "wrong"}),
        json.dumps(
            {
                "id": 1,
                "created_at": "not-time",
                "updated_at": "not-time",
                "body": "body",
                "author_association": "OWNER",
                "html_url": "https://github.com/example",
            }
        ),
    ],
)
def test_github_comment_reader_wraps_invalid_json_and_schema(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    client = GitHubIssueComments("example/agent-claim")
    monkeypatch.setattr(client, "_run", lambda arguments: raw)

    with pytest.raises(ClaimError):
        client.list_protocol_candidates(71)


def test_missing_gh_repository_resolution_is_a_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(ClaimError, match="gh is required"):
        _repository(None)


def test_cli_version_exits_before_requiring_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == "agent-claim 0.5.0\n"


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repository.git",
        "git@github.com:owner/repository.git",
    ],
)
def test_repository_falls_back_to_standard_github_remote(
    monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    calls: list[list[str]] = []

    def failed_gh(*arguments, **kwargs):
        command = arguments[0]
        calls.append(command)
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 1, "", "not a gh repo")
        return subprocess.CompletedProcess(command, 0, remote + "\n", "")

    monkeypatch.setattr(subprocess, "run", failed_gh)

    assert _repository(None) == "owner/repository"
    assert calls == [
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        ["git", "config", "--get", "remote.origin.url"],
    ]


def test_bounded_command_stops_before_unbounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "MAX_COMMAND_OUTPUT_BYTES", 32)

    with pytest.raises(ClaimError, match="output limit"):
        issue_claim._bounded_command(
            [sys.executable, "-c", "print('x' * 1000)"],
            purpose="test command",
        )


def test_bounded_command_disables_github_update_notifications() -> None:
    observed = issue_claim._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="update notifier probe",
    )

    assert observed == "1"


def test_bounded_command_streams_stdin_without_putting_it_in_argv() -> None:
    observed = issue_claim._bounded_command(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().decode())"],
        purpose="stdin probe",
        input_data=b"bounded body",
    )

    assert observed == "bounded body"


def test_bounded_command_wraps_process_argument_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_start(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(subprocess, "Popen", cannot_start)

    with pytest.raises(ClaimError, match="cannot start test command"):
        issue_claim._bounded_command(["gh", "issue"], purpose="test command")


def test_bounded_command_wraps_stdin_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_write(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(github.os, "write", cannot_write)

    with pytest.raises(ClaimError, match="failed while sending bounded input"):
        issue_claim._bounded_command(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            purpose="stdin write probe",
            input_data=b"body",
        )


def test_bounded_command_reaps_child_when_selector_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(*arguments, **kwargs):
        process = original_popen(*arguments, **kwargs)
        observed["process"] = process
        return process

    def cannot_select():
        raise OSError(5, "selector failed")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.selectors, "DefaultSelector", cannot_select)

    with pytest.raises(ClaimError, match="failed while coordinating I/O"):
        issue_claim._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="selector setup probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_when_select_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(*arguments, **kwargs):
        process = original_popen(*arguments, **kwargs)
        observed["process"] = process
        return process

    class FailingSelector:
        instance: FailingSelector | None = None

        def __init__(self) -> None:
            self.closed = False
            FailingSelector.instance = self

        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise OSError(5, "select failed")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.selectors, "DefaultSelector", FailingSelector)

    with pytest.raises(ClaimError, match="failed while waiting for I/O"):
        issue_claim._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="select probe",
        )

    process = observed["process"]
    assert process.poll() is not None
    assert process.stdout is not None and process.stdout.closed
    assert FailingSelector.instance is not None and FailingSelector.instance.closed


def test_bounded_command_reaps_child_when_output_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen
    original_read = github.os.read

    def start(*arguments, **kwargs):
        process = original_popen(*arguments, **kwargs)
        observed["process"] = process
        return process

    def cannot_read(file_descriptor: int, count: int) -> bytes:
        process = observed.get("process")
        if (
            process is not None
            and process.stdout is not None
            and file_descriptor == process.stdout.fileno()
        ):
            raise OSError(5, "read failed")
        return original_read(file_descriptor, count)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.os, "read", cannot_read)

    with pytest.raises(ClaimError, match="failed while reading output"):
        issue_claim._bounded_command(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready'); time.sleep(30)",
            ],
            purpose="read probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(*arguments, **kwargs):
        process = original_popen(*arguments, **kwargs)
        observed["process"] = process
        return process

    class CancellationSentinel(BaseException):
        pass

    class CancellingSelector:
        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise CancellationSentinel

        def close(self) -> None:
            pass

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.selectors, "DefaultSelector", CancellingSelector)

    with pytest.raises(CancellationSentinel):
        issue_claim._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="cancellation probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_scope_directories_detects_a_git_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    def git(arguments: list[str]) -> str:
        if arguments == ["cat-file", "-t", "HEAD:docs"]:
            return "tree"
        if arguments == ["cat-file", "-t", "HEAD:README.md"]:
            return "blob"
        raise ClaimError("not a git object")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("docs", "README.md")) == ("docs",)


def test_scope_directories_detects_an_untracked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scratch").mkdir()
    (tmp_path / "file.py").write_text("x\n")

    def git(arguments: list[str]) -> str:
        if arguments[:2] == ["cat-file", "-t"]:
            raise ClaimError("not in HEAD")
        if arguments == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        raise ClaimError("unexpected git")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("scratch", "file.py")) == ("scratch",)


def test_paths_under_scope_matches_prefix_or_exact_entry() -> None:
    paths = ("LICENSE", "src/a.py", "src/b.py", "docs/a.md")

    assert checkout.paths_under_scope(paths, ("src",)) == ("src/a.py", "src/b.py")
    assert checkout.paths_under_scope(paths, ("LICENSE",)) == ("LICENSE",)
    assert checkout.paths_under_scope(paths, ("src/a.py", "docs")) == ("src/a.py", "docs/a.md")
    assert checkout.paths_under_scope(paths, ("missing",)) == ()



def test_checkout_validation_binds_clean_head_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    issue_claim._validate_checkout(request())


@pytest.mark.parametrize(
    ("candidate", "values", "message"),
    [
        (
            request(),
            {
                ("rev-parse", "HEAD"): "b" * 40,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout HEAD",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "other",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout branch",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "linked isolated worktree",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): " M file",
            },
            "before the first worktree edit",
        ),
    ],
)
def test_checkout_validation_rejects_false_or_late_claims(
    monkeypatch: pytest.MonkeyPatch,
    candidate: ClaimRequest,
    values: dict[tuple[str, str], str],
    message: str,
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    with pytest.raises(ClaimError, match=message):
        issue_claim._validate_checkout(candidate)


def _git_checkout(
    *,
    head: str = BASE,
    branch: str = "codex/issue-72",
    git_directory: str = "/repo/.git/worktrees/issue-72",
    common_directory: str = "/repo/.git",
    dirty: str = "",
) -> dict[tuple[str, str], str]:
    return {
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--show-toplevel"): "/repo",
        ("branch", "--show-current"): branch,
        ("rev-parse", "--git-dir"): git_directory,
        ("rev-parse", "--git-common-dir"): common_directory,
        ("status", "--porcelain"): dirty,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("log", "--first-parent", "--reverse", "--format=%cI", "refs/remotes/origin/main"): "",
    }


def _set_agent_identity_env(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str] | None = None
) -> None:
    for name in (
        issue_claim.AGENT_CLAIM_AGENT_ENV,
        issue_claim.GROK_SESSION_ID_ENV,
        issue_claim.CLAUDE_SESSION_ID_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in (environ or {}).items():
        monkeypatch.setenv(name, value)


def _forbid_github_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("agent identity must be resolved before GitHub")

    monkeypatch.setattr(github, "GitHubIssueComments", unused)
    monkeypatch.setattr(checkout, "_repository", unused)
    monkeypatch.setattr(discovery, "discover_ledger", unused)


def _forbid_git_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(arguments: list[str]) -> str:
        pytest.fail("agent identity must be resolved before git fill")

    monkeypatch.setattr(checkout, "_git_output", unused)


def _patch_release_session(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeComments,
    *,
    agent: str = "Ada",
    branch: str | None = "lane-72",
    forbid_git: bool = False,
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: agent})
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    if forbid_git:
        def unused(arguments: list[str]) -> str:
            pytest.fail("explicit --claim-id must not inspect checkout branch")

        monkeypatch.setattr(checkout, "_git_output", unused)
        return
    git_values = {("branch", "--show-current"): branch or ""}
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )


def _assert_missing_identity_message(message: str) -> None:
    assert "--agent" in message
    assert issue_claim.AGENT_CLAIM_AGENT_ENV in message
    assert issue_claim.GROK_SESSION_ID_ENV in message
    assert issue_claim.CLAUDE_SESSION_ID_ENV in message
    assert "GROK_AGENT" not in message


def _claim_without_agent_args(*flags: str) -> list[str]:
    return [
        "claim",
        "72",
        "--role",
        "builder",
        "--scope",
        "src",
        "--claim-id",
        "cli-claim",
        *flags,
    ]


def _parse_claim_command(*flags: str):
    return issue_claim._parser().parse_args(
        [
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
            *flags,
        ]
    )


@pytest.mark.parametrize(
    ("flags", "git_values", "error"),
    [
        ((), _git_checkout(), None),
        (("--branch", "codex/issue-72"), _git_checkout(), None),
        (("--base", BASE), _git_checkout(), None),
        (("--branch", "other"), _git_checkout(), "does not match checkout branch"),
        (("--base", "b" * 40), _git_checkout(), "does not match checkout HEAD"),
        (
            ("--base", "b" * 40, "--branch", "other"),
            _git_checkout(),
            "does not match checkout HEAD",
        ),
        ((), _git_checkout(branch="main"), "isolated non-main worktree branch"),
        ((), _git_checkout(branch="master"), "isolated non-main worktree branch"),
        (
            (),
            _git_checkout(git_directory="/repo/.git", common_directory="/repo/.git"),
            "linked isolated worktree",
        ),
        ((), _git_checkout(dirty=" M file"), "before the first worktree edit"),
    ],
)
def test_claim_request_binds_omitted_base_and_branch_to_checkout(
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    git_values: dict[tuple[str, str], str],
    error: str | None,
) -> None:
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    parsed = _parse_claim_command(*flags)
    if "--base" not in flags:
        assert parsed.base is None
    if "--branch" not in flags:
        assert parsed.branch is None

    if error is not None:
        with pytest.raises(ClaimError, match=error):
            issue_claim._request(parsed)
        return

    claimed = issue_claim._request(parsed)
    assert claimed.base == git_values[("rev-parse", "HEAD")]
    assert claimed.branch == git_values[("branch", "--show-current")]


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim", "42", "--agent", "Ada", "--role", "builder"],
        [
            "supersede",
            "170",
            "--agent",
            "Ada",
            "--reason",
            "landed",
            "--claim-id",
            "cli-claim",
        ],
    ],
)
def test_claim_still_requires_scope_and_supersede_still_requires_role(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim._parser().parse_args(arguments)

    assert exited.value.code == 2


def test_cli_claim_role_argparse_default_unchanged_and_release_omits_role_reason() -> None:
    claimed = issue_claim._parser().parse_args(["claim", "42", "--scope", "src/widget.py"])
    released = issue_claim._parser().parse_args(["release", "42"])

    assert claimed.role == issue_claim.DEFAULT_CLAIM_ROLE
    assert released.role is None
    assert released.reason is None
    assert released.claim_id is None
    assert released.coordinator_override is False


@pytest.mark.parametrize(
    ("role_flags", "role"),
    [
        ((), issue_claim.DEFAULT_CLAIM_ROLE),
        (("--role", "builder"), "builder"),
        (("--role", "coordinator"), "coordinator"),
    ],
)
def test_cli_claim_omitted_role_posts_default_and_explicit_wins(
    monkeypatch: pytest.MonkeyPatch,
    role_flags: tuple[str, ...],
    role: str,
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            *role_flags,
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert claimed == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.role == role


def test_cli_claim_empty_role_fails_closed_without_posting_builder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    argv = [
        "--repo",
        "example/agent-claim",
        "claim",
        "72",
        "--agent",
        "Codex Sol",
        "--role",
        "",
        "--base",
        BASE,
        "--branch",
        "codex/issue-72",
        "--scope",
        "src",
        "--claim-id",
        "cli-claim",
    ]

    parsed = issue_claim._parser().parse_args(argv)
    with pytest.raises(ClaimError, match=r"role.+must be one bounded non-empty line"):
        issue_claim._request(parsed)

    claimed = issue_claim.main(argv)
    captured = capsys.readouterr()

    assert claimed == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "role" in captured.err
    assert "must be one bounded non-empty line" in captured.err
    assert client.list_protocol_candidates(LEDGER_ISSUE) == ()
    assert active_claims(tuple(client.comments.get(LEDGER_ISSUE, ()))) == ()


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim", "42", "--role", "builder", "--scope", "src/widget.py"],
        ["release", "42", "--role", "builder", "--reason", "landed"],
    ],
)
def test_claim_and_release_parse_omitted_agent(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    _set_agent_identity_env(monkeypatch)
    parsed = issue_claim._parser().parse_args(arguments)
    assert parsed.agent is None


def test_supersede_still_requires_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_agent_identity_env(monkeypatch)
    with pytest.raises(SystemExit) as exited:
        issue_claim._parser().parse_args(
            [
                "supersede",
                "170",
                "--role",
                "coordinator",
                "--reason",
                "reviewed successor ready",
                "--claim-id",
                "cli-claim",
            ]
        )

    assert exited.value.code == 2


@pytest.mark.parametrize(
    ("explicit", "environ", "agent"),
    [
        (
            "Ada",
            {
                "AGENT_CLAIM_AGENT": "Other",
                "GROK_SESSION_ID": "grok-session",
                "CLAUDE_SESSION_ID": "claude-session",
            },
            "Ada",
        ),
        (None, {"AGENT_CLAIM_AGENT": "Ada"}, "Ada"),
        (None, {"AGENT_CLAIM_AGENT": "", "GROK_SESSION_ID": "sess-1"}, "Grok sess-1"),
        (
            None,
            {"GROK_SESSION_ID": "sess-1", "CLAUDE_SESSION_ID": "sess-2"},
            "Grok sess-1",
        ),
        (None, {"CLAUDE_SESSION_ID": "sess-2"}, "Claude sess-2"),
        (
            None,
            {
                "AGENT_CLAIM_AGENT": "",
                "GROK_SESSION_ID": "",
                "CLAUDE_SESSION_ID": "sess-2",
            },
            "Claude sess-2",
        ),
    ],
)
def test_request_and_cli_claim_fill_agent_from_documented_else_chain(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    environ: dict[str, str],
    agent: str,
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    command = _claim_without_agent_args()
    if explicit is not None:
        command.extend(["--agent", explicit])
    parsed = issue_claim._parser().parse_args(command)
    assert issue_claim._request(parsed).agent == agent

    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    assert issue_claim.main(["--repo", "example/agent-claim", *command]) == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.agent == agent


@pytest.mark.parametrize(
    ("explicit", "environ"),
    [
        ("", {"AGENT_CLAIM_AGENT": "Ada"}),
        (None, {"AGENT_CLAIM_AGENT": " ", "GROK_SESSION_ID": "sess-1"}),
        (None, {"GROK_SESSION_ID": "bad\nid", "CLAUDE_SESSION_ID": "sess-2"}),
        (None, {"GROK_SESSION_ID": "x" * 200, "CLAUDE_SESSION_ID": "sess-2"}),
    ],
)
def test_invalid_agent_identity_fails_before_git_and_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    explicit: str | None,
    environ: dict[str, str],
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    _forbid_git_fill(monkeypatch)
    command = _claim_without_agent_args()
    if explicit is not None:
        command.extend(["--agent", explicit])
    parsed = issue_claim._parser().parse_args(command)
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        issue_claim._request(parsed)

    _forbid_github_construction(monkeypatch)
    releases = [
        ["release", "72"],
        ["release", "72", "--role", "builder", "--reason", "landed"],
    ]
    if explicit is not None:
        for argv in releases:
            argv.extend(["--agent", explicit])
    for argv in (command, *releases):
        assert issue_claim.main(["--repo", "example/agent-claim", *argv]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ERROR:" in captured.err
        assert "agent must be one bounded non-empty line" in captured.err


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {
            "AGENT_CLAIM_AGENT": "",
            "GROK_SESSION_ID": "",
            "CLAUDE_SESSION_ID": "",
        },
        {"GROK_AGENT": "should-not-fill"},
    ],
)
def test_missing_agent_identity_fails_closed_without_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    _forbid_git_fill(monkeypatch)
    command = _claim_without_agent_args()
    parsed = issue_claim._parser().parse_args(command)
    with pytest.raises(ClaimError) as raised:
        issue_claim._request(parsed)
    _assert_missing_identity_message(str(raised.value))

    _forbid_github_construction(monkeypatch)
    for argv in (
        command,
        ["release", "72"],
        ["release", "72", "--role", "builder", "--reason", "landed"],
    ):
        assert issue_claim.main(["--repo", "example/agent-claim", *argv]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("ERROR:")
        _assert_missing_identity_message(captured.err)


def test_cli_same_filled_agent_can_claim_and_release_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_identity_env(monkeypatch, {"GROK_SESSION_ID": "session-1"})
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )
    released = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "release",
            "72",
            "--role",
            "builder",
            "--reason",
            "landed",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert (claimed, released) == (0, 0)
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.agent == "Grok session-1"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_two_session_claimants_cannot_release_without_extra_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"GROK_SESSION_ID": "session-1"})
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "claim",
                "72",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-72",
                "--scope",
                "src",
                "--claim-id",
                "cli-claim",
            ]
        )
        == 0
    )
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))
    capsys.readouterr()

    _set_agent_identity_env(monkeypatch, {"CLAUDE_SESSION_ID": "session-2"})
    released = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "release",
            "72",
            "--role",
            "builder",
            "--reason",
            "landed",
            "--claim-id",
            "cli-claim",
        ]
    )
    captured = capsys.readouterr()

    assert released == 2
    assert "original claimant" in captured.err
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert [claim.agent for claim in standing] == ["Grok session-1"]


@pytest.mark.parametrize(
    ("role", "flags", "reason"),
    [
        ("builder", (), "landed"),
        ("reviewer", (), "landed"),
        ("reviewer", ("--reason", "abandoned"), "abandoned"),
    ],
)
def test_cli_release_omitted_flags_posts_landed_using_selected_claim_role(
    monkeypatch: pytest.MonkeyPatch, role: str, flags: tuple[str, ...], reason: str
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role=role, branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", *flags]
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert isinstance(posted, ClaimantRelease)
    assert posted.claim_id == "mine"
    assert posted.role == role
    assert posted.reason == reason
    assert posted.agent == "Ada"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_release_omitted_claim_id_releases_when_foreign_peer_exists_on_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
        request(
            "theirs",
            "Other",
            issue=72,
            role="builder",
            branch="other-lane",
            scope=("docs",),
        ),
    )
    _patch_release_session(monkeypatch, client)

    released = issue_claim.main(["--repo", "example/agent-claim", "release", "72"])
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert [claim.claim_id for claim in standing] == ["theirs"]
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "landed"


@pytest.mark.parametrize(
    ("agent", "branch", "standing"),
    [
        (
            "Other",
            "lane-72",
            (
                request(
                    "mine",
                    "Ada",
                    issue=72,
                    role="reviewer",
                    branch="lane-72",
                    scope=("src",),
                ),
            ),
        ),
        (
            "Ada",
            "other-lane",
            (
                request(
                    "mine",
                    "Ada",
                    issue=72,
                    role="reviewer",
                    branch="lane-72",
                    scope=("src",),
                ),
            ),
        ),
        (
            "Ada",
            "lane-72",
            (
                request(
                    "one", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)
                ),
                request(
                    "two", "Ada", issue=72, role="builder", branch="lane-72", scope=("docs",)
                ),
            ),
        ),
    ],
)
def test_cli_release_wrong_agent_or_branch_or_two_matches_fails_without_post(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    branch: str,
    standing: tuple[ClaimRequest, ...],
) -> None:
    client = _claims_client(*standing)
    _patch_release_session(monkeypatch, client, agent=agent, branch=branch)
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    released = issue_claim.main(["--repo", "example/agent-claim", "release", "72"])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "pass --claim-id" in captured.err
    assert "conflicting claims" not in captured.err
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_cli_release_explicit_claim_id_ignores_checkout_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client, forbid_git=True)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--claim-id", "mine"]
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "landed"


@pytest.mark.parametrize(
    "flags",
    [
        ("--coordinator-override",),
        ("--coordinator-override", "--role", "builder", "--reason", "takeover"),
        ("--coordinator-override", "--role", "coordinator"),
        ("--coordinator-override", "--reason", "takeover"),
    ],
)
def test_cli_release_override_fails_before_git_and_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)

    def unused(arguments: list[str]) -> str:
        pytest.fail("coordinator override must fail before git")

    monkeypatch.setattr(checkout, "_git_output", unused)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", *flags]
    )
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "coordinator override" in captured.err


def test_cli_release_omitted_claim_id_fails_closed_on_detached_head(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: "")

    released = issue_claim.main(["--repo", "example/agent-claim", "release", "72"])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "pass --claim-id" in captured.err


def test_cli_claim_omitted_base_and_branch_posts_filled_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    git_values = _git_checkout()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert claimed == 0
    assert "CLAIMED issue #72" in capsys.readouterr().out
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.base == BASE
    assert posted.branch == "codex/issue-72"
    assert posted.scope == ("src",)


def test_cli_status_claim_release_and_adapter_error_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )
    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
    released = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "release",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--reason",
            "landed",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert (claimed, status, released) == (0, 0, 0)
    assert "CLAIMED issue #72" in capsys.readouterr().out

    monkeypatch.setattr(
        github,
        "GitHubIssueComments",
        lambda repository: (_ for _ in ()).throw(ClaimError("adapter failed")),
    )
    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 2
    assert "ERROR: adapter failed" in capsys.readouterr().err



class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 21, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _stub_versioned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_claim/__init__.py",
        ),
    )


# A PR checkout has no origin/main, so the live function would fail loud in CI;
# tests of trunk_landing_times itself call _LIVE_TRUNK_LANDING_TIMES.
@pytest.fixture(autouse=True)
def _stub_trunk_landing_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())


def test_versioned_paths_reads_nul_terminated_ls_files_without_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments, **kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, stdout=b" foo.py\0bar.py\0 foo.py\0", stderr=b""
        )

    monkeypatch.setattr(checkout.subprocess, "run", run)

    assert _LIVE_VERSIONED_PATHS() == (" foo.py", "bar.py")
    assert observed == [["git", "ls-files", "-z", "--full-name"]]


def test_versioned_paths_fails_loud_like_git_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_arguments, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(checkout.subprocess, "run", missing)
    with pytest.raises(ClaimError, match="git is required for issue claims"):
        _LIVE_VERSIONED_PATHS()

    def timed_out(*_arguments, **_kwargs):
        raise subprocess.TimeoutExpired(["git"], checkout.GH_TIMEOUT_SECONDS)

    monkeypatch.setattr(checkout.subprocess, "run", timed_out)
    with pytest.raises(ClaimError, match="git timed out while validating the build checkout"):
        _LIVE_VERSIONED_PATHS()

    def failed(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 128, stdout=b"", stderr=b"fatal: not a git repository\n"
        )

    monkeypatch.setattr(checkout.subprocess, "run", failed)
    with pytest.raises(ClaimError, match="fatal: not a git repository"):
        _LIVE_VERSIONED_PATHS()


@pytest.fixture(autouse=True)
def _freeze_cli_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def _patch_status_cli(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeComments,
    *,
    ledger: int | None = LEDGER_ISSUE,
) -> None:
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: ledger)
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_claim/__init__.py",
        ),
    )
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def test_cli_reconcile_repairs_a_poisoned_ledger_and_status_reads_it_afterwards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    client = FakeComments({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 0
    reconcile_out = capsys.readouterr().out
    assert "REPAIRED claim 'claim-a': superseded #1 -> survivor #2" in reconcile_out
    assert "RECONCILED #72" in reconcile_out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert "CLAIMED issue #72" in capsys.readouterr().out


def test_cli_reconcile_targeted_issue_succeeds_on_a_mixed_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A targeted `reconcile <issue>` filters its own summary line by identity kind
    (cli.py's `isinstance(claim.identity, IssueIdentity)` guard); a lane claim
    coexisting on the same ledger must not make that filter raise."""
    issue_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("backend",)))
    lane_body = claim_comment(
        request("lane-claim", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("docs",))
    )
    client = FakeComments({LEDGER_ISSUE: [comment(1, issue_body), comment(2, lane_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile", "72"]) == 0
    assert "RECONCILED #72" in capsys.readouterr().out
    assert client.labels == {72}


def test_cli_reconcile_refuses_a_cross_agent_duplicate_with_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Grok 4.6", issue=72, scope=("new",)))
    client = FakeComments({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 2
    assert "claim id 'claim-a'" in capsys.readouterr().err


def test_cli_reconcile_still_clears_stale_labels_when_ledger_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen_body = supersede_comment(
        claimed, 170, "Fleet Coordinator", "coordinator", "reviewed rollover ready"
    )
    client = FakeComments(
        {LEDGER_ISSUE: [comment(1, claimed_body), comment(2, frozen_body)]},
        {LEDGER_ISSUE, 72},
    )
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 2
    assert "frozen" in capsys.readouterr().err
    assert client.labels == set()


def test_cli_status_empty_ledger_prints_ledger_then_unclaimed_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\nUNCLAIMED repository\n"
    )


def test_cli_status_issue_with_no_claim_prints_ledger_then_unclaimed_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\nUNCLAIMED issue #72\n"
    )


def test_cli_status_after_claim_prints_ledger_then_claimed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "claim",
                "72",
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-72",
                "--scope",
                "src",
                "--claim-id",
                "cli-claim",
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72 claim=cli-claim 0h 0m\n"
        "  src\n"
    )


def test_cli_lane_claim_status_and_release_round_trip_without_issue_number(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full `Done when` #1 story: a docs/ checkout claims, is visible in
    `status`, and releases again — all without ever passing an issue number."""
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Codex Sol"})
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "claim",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "docs/lane-cleanup",
                "--scope",
                "docs",
                "--claim-id",
                "cli-lane-claim",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED lane docs/lane-cleanup: Codex Sol (builder) base={BASE} "
        "branch=docs/lane-cleanup claim=cli-lane-claim 0h 0m\n"
        "  docs\n"
    )

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "--reason", "landed"]
    )
    assert released == 0
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


@pytest.mark.parametrize("command", ["claim", "release"])
def test_cli_lane_mode_refuses_a_non_conventional_branch(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Codex Sol"})
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: "codex/issue-38-issueless-claims"
    )

    arguments = ["--repo", "example/agent-claim", command]
    if command == "claim":
        arguments += [
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-38-issueless-claims",
            "--scope",
            "src",
            "--claim-id",
            "cli-lane-claim",
        ]
    else:
        arguments += ["--reason", "landed"]

    assert issue_claim.main(arguments) == 2
    captured = capsys.readouterr()
    assert "codex/issue-38-issueless-claims" in captured.err
    assert "issue number" in captured.err
    assert "'docs/'" in captured.err and "'fix/'" in captured.err
    assert client.comments == {}


def test_cli_status_overlapping_protocol_comments_print_ledger_then_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72-claims claim=claim-a 0h 0m\n"
        "  shared\n"
        "  overlaps issue #73 (claim-b)\n"
        f"CLAIMED issue #73: Grok 4.6 (builder) base={BASE} "
        "branch=codex/issue-73-claims claim=claim-b 0h 0m\n"
        "  shared/file.py\n"
        "  overlaps issue #72 (claim-a)\n"
    )


def test_cli_status_without_ledger_errors_and_prints_no_ledger_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "no agent-claim ledger exists" in captured.err
    assert "LEDGER" not in captured.out


def test_status_direct_empty_claims_prints_unclaimed_repository_without_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _status((), None) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_json_empty_ledger_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(
        {
            "ledger": LEDGER_ISSUE,
            "issue": None,
            "state": "UNCLAIMED",
            "claims": [],
        }
    ) + "\n"


def test_cli_status_json_issue_with_no_claim_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())

    assert (
        issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    )
    assert capsys.readouterr().out == json.dumps(
        {
            "ledger": LEDGER_ISSUE,
            "issue": 72,
            "state": "UNCLAIMED",
            "claims": [],
        }
    ) + "\n"


def test_cli_status_json_after_claim_prints_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments())
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "claim",
                "72",
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-72",
                "--scope",
                "src",
                "--claim-id",
                "cli-claim",
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "72", "--json"]
    )
    assert status == 0
    assert capsys.readouterr().out == json.dumps(
        {
            "ledger": LEDGER_ISSUE,
            "issue": 72,
            "state": "CLAIMED",
            "claims": [
                {
                    "issue": 72,
                    "lane": None,
                    "agent": "Codex Sol",
                    "role": "builder",
                    "base": BASE,
                    "branch": "codex/issue-72",
                    "claim_id": "cli-claim",
                    "scope": ["src"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [],
                    "state": "CLAIMED",
                    "age": "0h 0m",
                    "old": False,
                }
            ],
        }
    ) + "\n"


def test_cli_status_json_overlapping_protocol_comments_print_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "--json"])
    assert status == 0
    assert capsys.readouterr().out == json.dumps(
        {
            "ledger": LEDGER_ISSUE,
            "issue": None,
            "state": "CLAIMED",
            "claims": [
                {
                    "issue": 72,
                    "lane": None,
                    "agent": "Codex Sol",
                    "role": "builder",
                    "base": BASE,
                    "branch": "codex/issue-72-claims",
                    "claim_id": "claim-a",
                    "scope": ["shared"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [
                        {
                            "issue": 73,
                            "lane": None,
                            "claim_id": "claim-b",
                            "agent": "Grok 4.6",
                        }
                    ],
                    "state": "CLAIMED",
                    "age": "0h 0m",
                    "old": False,
                },
                {
                    "issue": 73,
                    "lane": None,
                    "agent": "Grok 4.6",
                    "role": "builder",
                    "base": BASE,
                    "branch": "codex/issue-73-claims",
                    "claim_id": "claim-b",
                    "scope": ["shared/file.py"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [
                        {
                            "issue": 72,
                            "lane": None,
                            "claim_id": "claim-a",
                            "agent": "Codex Sol",
                        }
                    ],
                    "state": "CLAIMED",
                    "age": "0h 0m",
                    "old": False,
                },
            ],
        }
    ) + "\n"


def test_cli_status_json_issue_on_overlap_prints_related_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "72", "--json"]
    )
    assert status == 0
    assert capsys.readouterr().out == json.dumps(
        {
            "ledger": LEDGER_ISSUE,
            "issue": 72,
            "state": "CLAIMED",
            "claims": [
                {
                    "issue": 72,
                    "lane": None,
                    "agent": "Codex Sol",
                    "role": "builder",
                    "base": BASE,
                    "branch": "codex/issue-72-claims",
                    "claim_id": "claim-a",
                    "scope": ["shared"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [
                        {
                            "issue": 73,
                            "lane": None,
                            "claim_id": "claim-b",
                            "agent": "Grok 4.6",
                        }
                    ],
                    "state": "CLAIMED",
                    "age": "0h 0m",
                    "old": False,
                },
                {
                    "issue": 73,
                    "lane": None,
                    "agent": "Grok 4.6",
                    "role": "builder",
                    "base": BASE,
                    "branch": "codex/issue-73-claims",
                    "claim_id": "claim-b",
                    "scope": ["shared/file.py"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [
                        {
                            "issue": 72,
                            "lane": None,
                            "claim_id": "claim-a",
                            "agent": "Codex Sol",
                        }
                    ],
                    "state": "CLAIMED",
                    "age": "0h 0m",
                    "old": False,
                },
            ],
        }
    ) + "\n"


def test_cli_status_json_without_ledger_errors_and_prints_no_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "no agent-claim ledger exists" in captured.err


def test_cli_claim_and_release_accept_json_while_parent_and_bootstrap_reject_it() -> None:
    claimed = issue_claim._parser().parse_args(
        ["claim", "42", "--scope", "src/widget.py", "--json"]
    )
    released = issue_claim._parser().parse_args(["release", "42", "--json"])
    omitted_claim = issue_claim._parser().parse_args(["claim", "42", "--scope", "src"])
    omitted_release = issue_claim._parser().parse_args(["release", "42"])

    assert claimed.json is True
    assert released.json is True
    assert omitted_claim.json is False
    assert omitted_release.json is False
    for arguments in (["--json", "status"], ["bootstrap", "--json"]):
        with pytest.raises(SystemExit) as exited:
            issue_claim._parser().parse_args(arguments)
        assert exited.value.code == 2


def test_cli_claim_without_json_prints_the_claimed_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert claimed == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: cli-claim "
        "https://github.com/example/agent-claim/issues/71#issuecomment-1\n"
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_comma_joined_scope_is_stored_as_distinct_paths_and_overlaps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "ReproAgentA",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py",
            "--claim-id",
            "joined",
        ]
    )

    assert claimed == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == (
        "docs/PRODUCT.md",
        "src/atelier2/adapters/dbos/run_transitions.py",
    )

    second = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "ReproAgentB",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "docs/PRODUCT.md",
            "--claim-id",
            "single",
        ]
    )
    captured = capsys.readouterr()

    assert second == 0
    assert "CLAIMED issue #73: single " in captured.out
    assert "overlaps issue #72 (joined)" in captured.out
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == 2


def test_cli_comma_joined_scope_flag_equals_repeated_scope_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    joined = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs/PRODUCT.md,src/widget.py",
            "--claim-id",
            "joined",
        ]
    )
    repeated_client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: repeated_client)
    repeated = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "docs/PRODUCT.md",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "repeated",
        ]
    )

    assert (joined, repeated) == (0, 0)
    first = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    second = parse_claim_event(repeated_client.comments[LEDGER_ISSUE][0])
    assert isinstance(first, ActiveClaim) and isinstance(second, ActiveClaim)
    assert first.scope == second.scope == ("docs/PRODUCT.md", "src/widget.py")


def test_cli_rescope_adds_a_path_without_matching_head_or_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquired = acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "src/new.py",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].claim_id == acquired.claim_id
    assert standing[0].base == BASE
    assert standing[0].scope == ("src/widget.py", "src/new.py")


def test_cli_rescope_json_prints_updated_scope_and_same_claim_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request("cli-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "docs/PRODUCT.md,src/new.py",
            "--drop",
            "src/widget.py",
            "--json",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == json.dumps(
        {
            "issue": 72,
            "lane": None,
            "claim_id": "cli-claim",
            "agent": "Ada",
            "role": "builder",
            "base": BASE,
            "branch": "codex/issue-72",
            "scope": ["docs/PRODUCT.md", "src/new.py"],
        }
    ) + "\n"


def test_cli_rescope_without_add_or_drop_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "rescope", "72"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "--add" in captured.err or "rescope requires" in captured.err


def test_cli_rescope_refuses_primary_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout(git_directory="/repo/.git", common_directory="/repo/.git")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "linked isolated worktree" in captured.err


def test_cli_claim_refuses_a_directory_scope_without_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "tree",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "directory scope 'docs'" in captured.err
    assert "erst schneiden" in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_allows_a_directory_scope_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--allow-directory",
            "rewrite the docs tree",
            "--claim-id",
            "tree",
        ]
    )

    assert status == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == ("docs",)
    assert (
        "- Allow-directory reason: rewrite the docs tree"
        in client.comments[LEDGER_ISSUE][0].body
    )


def test_cli_who_prints_the_claim_holding_a_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, scope=("docs/PRODUCT.md", "src/widget.py"))
    )
    _patch_status_cli(monkeypatch, client)

    claimed = issue_claim.main(
        ["--repo", "example/agent-claim", "who", "docs/PRODUCT.md"]
    )
    free = issue_claim.main(["--repo", "example/agent-claim", "who", "README.md"])
    claimed_out = capsys.readouterr().out

    assert claimed == 0
    assert free == 0
    assert "CLAIMED docs/PRODUCT.md issue #72: Ada (builder) claim=mine" in claimed_out
    assert "UNCLAIMED README.md" in claimed_out


def test_cli_who_json_prints_holder_or_unclaimed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request("mine", "Ada", issue=72, scope=("docs",)))
    _patch_status_cli(monkeypatch, client)

    descendant = issue_claim.main(
        ["--repo", "example/agent-claim", "who", "docs/decisions/one.md", "--json"]
    )
    claimed = json.loads(capsys.readouterr().out)
    free = issue_claim.main(
        ["--repo", "example/agent-claim", "who", "src/widget.py", "--json"]
    )
    unclaimed = json.loads(capsys.readouterr().out)

    assert descendant == 0
    assert claimed["state"] == "CLAIMED"
    assert claimed["path"] == "docs/decisions/one.md"
    assert claimed["claims"][0]["claim_id"] == "mine"
    assert free == 0
    assert unclaimed == {
        "ledger": LEDGER_ISSUE,
        "path": "src/widget.py",
        "state": "UNCLAIMED",
        "claims": [],
    }


def test_cli_rescope_refuses_adding_a_directory_without_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "docs"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "directory scope 'docs'" in captured.err
    assert "erst schneiden" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py",)



def test_cli_claim_share_above_a_quarter_requires_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--claim-id",
            "wide",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "2 of 4" in captured.err
    assert "--allow-directory" in captured.err
    assert "erst schneiden" not in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_share_above_a_quarter_succeeds_with_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--allow-directory",
            "cover two files",
            "--claim-id",
            "wide",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["versioned_files"] == 2
    assert payload["versioned_files_total"] == 4
    assert payload["share"] == 0.5
    assert payload["touches"] == []
    assert "- Allow-directory reason: cover two files" in client.comments[LEDGER_ISSUE][0].body


def test_cli_claim_share_at_a_quarter_does_not_need_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "quarter",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out.endswith(
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_claim_touches_stay_empty_beside_a_disjoint_standing_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request("claim-a", "Ada", issue=73, scope=("LICENSE",)))
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "disjoint",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["touches"] == []


def test_claim_cost_lists_an_overlapping_standing_claim_as_a_touch() -> None:
    standing = parse_claim_event(
        comment(1, claim_comment(request("claim-a", issue=55, scope=("src",))))
    )
    lane = parse_claim_event(
        comment(
            2,
            claim_comment(
                request("claim-b", "Grok 4.6", lane=True, branch="docs/foo", scope=("docs",))
            ),
        )
    )
    assert isinstance(standing, ActiveClaim) and isinstance(lane, ActiveClaim)
    overlapping = protocol.conflicting_claims(
        (standing, lane), request("challenger", issue=56, scope=("src/widget.py",))
    )
    both = protocol.conflicting_claims(
        (standing, lane), request("wide", issue=56, scope=("src", "docs"))
    )

    assert [claim.claim_id for claim in overlapping] == ["claim-a"]
    assert issue_claim._touch_summary(overlapping) == "overlaps issue #55 (claim-a)"
    assert issue_claim._touch_summary(both) == (
        "overlaps issue #55 (claim-a), lane docs/foo (claim-b)"
    )
    assert issue_claim._touch_summary(()) == "overlaps no other open claims"


def test_claim_age_old_compares_real_age_against_the_threshold() -> None:
    just_over_an_hour = timedelta(seconds=3601)
    exactly_one_hour = timedelta(hours=1)
    sixty_one_minutes = timedelta(seconds=3660)

    assert board.format_claim_age(just_over_an_hour) == "1h 0m"
    assert board.claim_is_old(just_over_an_hour) is True
    assert board.format_claim_age(sixty_one_minutes) == "1h 1m"
    assert board.claim_is_old(sixty_one_minutes) is True
    assert board.claim_is_old(exactly_one_hour) is False


def test_has_cut_requires_a_non_empty_slice_title() -> None:
    assert board.has_cut("## Schnitt\n\n**Scheibe 1: Title**\n") is True
    assert board.has_cut("## Schnitt\n\n**Scheibe 1:    **\n") is False
    assert board.has_cut("## Schnitt\n\n**Scheibe 1:**\n") is False
    assert board.has_cut(
        "## Schnitt\n\n**Scheibe 1:    **\n**Scheibe 2: Real title**\n"
    ) is True


def test_status_and_board_show_claim_age_from_the_claim_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    fresh = comment(1, claim_comment(claimed), created_at="2026-08-20T23:30:00Z")
    client = FakeComments({LEDGER_ISSUE: [fresh]})
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert " 0h 30m\n" in status_out
    assert " old" not in status_out.split("CLAIMED", 1)[1]

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "0h 30m"
    assert payload["claims"][0]["old"] is False

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 0h 30m" in capsys.readouterr().out
    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    item = next(row for row in json.loads(capsys.readouterr().out)["items"] if row["number"] == 72)
    assert item["claim_age"] == "0h 30m"
    assert item["claim_old"] is False


def test_status_and_board_mark_a_claim_old_after_sixty_one_minutes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    old = comment(1, claim_comment(claimed), created_at="2026-08-20T22:59:00Z")
    client = FakeComments({LEDGER_ISSUE: [old]})
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert " 1h 1m old\n" in capsys.readouterr().out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "1h 1m"
    assert payload["claims"][0]["old"] is True

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 1h 1m old" in capsys.readouterr().out


def test_claim_age_uses_the_claim_comment_not_a_later_rescope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    claim_event = comment(1, claim_comment(claimed), created_at="2026-08-20T23:30:00Z")
    parsed = parse_claim_event(claim_event)
    assert isinstance(parsed, ActiveClaim)
    rescope_event = comment(
        2,
        protocol.rescope_comment(parsed, ("src", "LICENSE"), "Ada", "builder"),
        created_at="2026-08-20T23:59:00Z",
    )
    client = FakeComments({LEDGER_ISSUE: [claim_event, rescope_event]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    out = capsys.readouterr().out
    assert " 0h 30m\n" in out
    assert " old" not in out.split("CLAIMED", 1)[1]


def test_cli_claim_allows_a_cut_directory_without_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeComments()
    client.board_issues = (
        board_issue(
            72,
            "Cut work",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "cut",
        ]
    )

    assert status == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == ("docs",)


def test_cli_claim_refuses_a_schnitt_heading_without_a_scheibe_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    client.board_issues = (
        board_issue(
            72,
            "Uncut",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\nNo slices yet.\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "heading",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "erst schneiden" in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_lane_directory_without_allow_directory_is_erst_schneiden(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Ada"})
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "--base",
            BASE,
            "--branch",
            "docs/lane-cleanup",
            "--scope",
            "docs",
            "--claim-id",
            "lane-docs",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "erst schneiden" in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_cut_directory_still_needs_allow_directory_when_share_is_high(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    client.board_issues = (
        board_issue(
            72,
            "Cut work",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: ("LICENSE", "README.md", "docs/a.md", "docs/b.md"),
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "wide-cut",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "2 of 4" in captured.err
    assert "--allow-directory" in captured.err
    assert "erst schneiden" not in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_rescope_add_that_raises_combined_share_requires_allow_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "LICENSE",
            "--add",
            "README.md",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "3 of 4" in captured.err
    assert "--allow-directory" in captured.err
    assert "erst schneiden" not in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src",)


def test_cli_rescope_persists_allow_directory_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "docs",
            "--allow-directory",
            "widen to the docs tree",
        ]
    )

    assert status == 0
    bodies = [entry.body for entry in client.comments[LEDGER_ISSUE]]
    assert any("- Allow-directory reason: widen to the docs tree" in body for body in bodies)
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py", "docs")


def test_cli_release_without_json_prints_the_released_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client)

    released = issue_claim.main(["--repo", "example/agent-claim", "release", "72"])

    assert released == 0
    assert capsys.readouterr().out == "RELEASED issue #72: mine\n"


@pytest.mark.parametrize(
    ("issue_argument", "branch", "identity_fields"),
    [
        (["72"], "codex/issue-72", {"issue": 72, "lane": None}),
        ([], "docs/lane-cleanup", {"issue": None, "lane": True}),
    ],
    ids=["issue", "lane"],
)
def test_cli_claim_json_prints_acquired_claim_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issue_argument: list[str],
    branch: str,
    identity_fields: dict[str, object],
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            *issue_argument,
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            branch,
            "--scope",
            "src",
            "--scope",
            "docs",
            "--claim-id",
            "cli-claim",
            "--json",
        ]
    )

    assert claimed == 0
    assert capsys.readouterr().out == json.dumps(
        {
            **identity_fields,
            "claim_id": "cli-claim",
            "url": "https://github.com/example/agent-claim/issues/71#issuecomment-1",
            "agent": "Codex Sol",
            "role": "builder",
            "base": BASE,
            "branch": branch,
            "scope": ["src", "docs"],
            "resource": None,
            "resource_value": None,
            "versioned_files": 1,
            "versioned_files_total": 4,
            "share": 0.25,
            "touches": [],
        }
    ) + "\n"
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == ("src", "docs")


@pytest.mark.parametrize(
    (
        "issue_argument",
        "branch",
        "identity_fields",
        "standing_role",
        "flags",
        "agent",
        "role",
        "reason",
    ),
    [
        (
            ["72"], "lane-72", {"issue": 72, "lane": None},
            "reviewer", ("--json",), "Ada", "reviewer", "landed",
        ),
        (
            ["72"], "lane-72", {"issue": 72, "lane": None},
            "reviewer",
            ("--reason", "abandoned", "--json"),
            "Ada", "reviewer", "abandoned",
        ),
        (
            ["72"], "lane-72", {"issue": 72, "lane": None},
            "reviewer",
            (
                "--claim-id",
                "mine",
                "--coordinator-override",
                "--role",
                "coordinator",
                "--reason",
                "verified abandoned",
                "--json",
            ),
            "Fleet Coordinator", "coordinator", "verified abandoned",
        ),
        (
            [], "docs/lane-cleanup", {"issue": None, "lane": True},
            "reviewer", ("--json",), "Ada", "reviewer", "landed",
        ),
        (
            [], "docs/lane-cleanup", {"issue": None, "lane": True},
            "reviewer",
            (
                "--claim-id",
                "mine",
                "--coordinator-override",
                "--role",
                "coordinator",
                "--reason",
                "verified abandoned",
                "--json",
            ),
            "Fleet Coordinator", "coordinator", "verified abandoned",
        ),
    ],
    ids=["issue-json", "issue-reason", "issue-override", "lane-json", "lane-override"],
)
def test_cli_release_json_prints_effective_posted_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issue_argument: list[str],
    branch: str,
    identity_fields: dict[str, object],
    standing_role: str,
    flags: tuple[str, ...],
    agent: str,
    role: str,
    reason: str,
) -> None:
    lane = not issue_argument
    client = _claims_client(
        request(
            "mine", "Ada", issue=72, lane=lane, role=standing_role, branch=branch, scope=("src",)
        )
    )
    # Lane mode always derives its branch from the checkout, even with an explicit
    # --claim-id (Entschieden #2: LaneIdentity carries no branch of its own), so git
    # is only forbidden for the issue-mode explicit-claim-id case.
    forbid_git = bool(issue_argument) and "--claim-id" in flags
    _patch_release_session(
        monkeypatch, client, agent=agent, branch=branch, forbid_git=forbid_git
    )

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", *issue_argument, *flags]
    )

    assert released == 0
    assert capsys.readouterr().out == json.dumps(
        {
            **identity_fields,
            "branch": branch,
            "claim_id": "mine",
            "agent": agent,
            "role": role,
            "reason": reason,
        }
    ) + "\n"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "claim",
            "72",
            "--agent",
            "Ada",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
            "--json",
        ],
        ["release", "72", "--agent", "Ada", "--claim-id", "mine", "--json"],
    ],
)
def test_cli_claim_and_release_json_errors_print_no_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeComments(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR:")
    assert "no agent-claim ledger exists" in captured.err


def test_cli_claim_json_conflict_errors_without_success_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request(issue=72, scope=("src",)))
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Grok 4.6",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "challenger",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert claimed == 2
    assert captured.out == ""
    assert captured.err.startswith("ERROR:")
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_cli_supersede_freezes_the_drained_ledger_and_prints_the_contract_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeComments(valid_successors={170})
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    frozen = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "supersede",
            "170",
            "--agent",
            "Fleet Coordinator",
            "--role",
            "coordinator",
            "--reason",
            "reviewed successor ready",
            "--claim-id",
            acquired.claim_id,
        ]
    )

    captured = capsys.readouterr()
    assert frozen == 0
    assert captured.out == (
        f"SUPERSEDED ledger #{LEDGER_ISSUE} successor #170: {acquired.claim_id}\n"
    )
    assert LEDGER_ISSUE not in client.labels
    assert "not available in v0.1" not in captured.out
    assert "not available in v0.1" not in captured.err
    with pytest.raises(LedgerSuperseded, match="successor #170"):
        active_claims(client.list_protocol_candidates(LEDGER_ISSUE))


@pytest.mark.parametrize("failure", ["builder", "drain"])
def test_cli_supersede_fails_closed_without_mutating_protocol_candidates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    client = FakeComments(valid_successors={170})
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    if failure == "drain":
        acquire_claim(client, request("other", issue=72, scope=("frontend",)))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    frozen = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "supersede",
            "170",
            "--agent",
            "Fleet Coordinator",
            "--role",
            "builder" if failure == "builder" else "coordinator",
            "--reason",
            "reviewed successor ready",
            "--claim-id",
            acquired.claim_id,
        ]
    )

    captured = capsys.readouterr()
    assert frozen == 2
    assert captured.err.startswith("ERROR:")
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def forbid_github_for_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("policy must not use GitHub")

    monkeypatch.setattr(github, "GitHubIssueComments", unused)
    monkeypatch.setattr(checkout, "_repository", unused)
    monkeypatch.setattr(discovery, "discover_ledger", unused)


@pytest.mark.parametrize(
    "arguments",
    [
        ["policy", "--print"],
        ["--repo", "OWNER/REPO", "policy", "--print"],
    ],
)
def test_cli_policy_print_emits_the_locked_loader_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    forbid_github_for_policy(monkeypatch)

    assert issue_claim.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "<!-- agent-claim-policy:v1 -->\n"
        "Before the first edit in a Git repository, use live `agent-claim`: "
        "`status`, then `claim` the issue and write scope. `bootstrap` only when "
        "neither a coordination/claim contract nor a ledger exists. `release` after "
        "landing or abandoning the lane. Missing `gh` or network is a failure, "
        "never coordinated success. Read-only review stays free. Do not invent a "
        "second board.\n"
    )
    assert captured.err == ""
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


def test_cli_policy_without_print_is_an_argparse_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    forbid_github_for_policy(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["policy"])

    assert exited.value.code == 2
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


def _isolate_protect_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return home, work


def _protect_git_values(
    work: Path, overrides: dict[tuple[str, str], str] | None = None
) -> dict[tuple[str, str], str]:
    values = {
        ("branch", "--show-current"): "codex/issue-72-claims",
        ("rev-parse", "--git-dir"): str(work / ".git" / "worktrees" / "issue-72"),
        ("rev-parse", "--git-common-dir"): str(work / ".git"),
        ("rev-parse", "--show-toplevel"): str(work.resolve()),
    }
    if overrides:
        values.update(overrides)
    return values


def _patch_protect_git(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    overrides: dict[tuple[str, str], str] | None = None,
) -> None:
    values = _protect_git_values(work, overrides)

    def git(arguments: list[str]) -> str:
        if arguments == ["status", "--porcelain"]:
            pytest.fail("dirty tree is irrelevant to protect")
        if arguments == ["rev-parse", "HEAD"]:
            pytest.fail("protect must not bind HEAD to claim.base")
        return values[tuple(arguments)]

    monkeypatch.setattr(checkout, "_git_output", git)


def _patch_protect_claim(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str = "Grok sess-1",
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
) -> FakeComments:
    claimed = comment(
        1,
        claim_comment(
            replace(
                request("cli-claim", agent, issue=72, lane=lane, scope=scope),
                branch=branch,
            )
        ),
    )
    client = FakeComments({LEDGER_ISSUE: [claimed]}, {72})
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    return client


def _forbid_protect_git_github_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("this protect path must not use identity, git, or GitHub")

    monkeypatch.setattr(checkout, "_resolved_agent", unused)
    monkeypatch.setattr(checkout, "_git_output", unused)
    monkeypatch.setattr(github, "GitHubIssueComments", unused)
    monkeypatch.setattr(checkout, "_repository", unused)
    monkeypatch.setattr(discovery, "discover_ledger", unused)
    monkeypatch.setattr(protocol, "configure_ledger", unused)


def _protect_main(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    return issue_claim.main(["--repo", "example/agent-claim", "protect"])


def _assert_protect_decision(
    capsys: pytest.CaptureFixture[str],
    *,
    decision: str,
    reason: str | None = None,
) -> None:
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    if decision == "allow":
        assert payload == {"decision": "allow"}
        return
    assert payload == {"decision": "deny", "reason": reason}


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Bash", "toolInput": {"path": "src/cli.py", "command": "rm -rf /"}},
        {"tool_name": "run_terminal_command", "tool_input": {"command": "git status"}},
        {"toolName": "read_file", "toolInput": {"path": "src/secret.py"}},
        {"tool_name": "grep", "tool_input": {"pattern": "secret"}},
        {"toolName": "list_dir", "toolInput": {"path": "src"}},
        {"tool_name": "spawn_subagent", "tool_input": {"prompt": "edit src"}},
        {"toolName": "unknown"},
    ],
)
def test_protect_non_mutating_tools_allow_without_identity_git_or_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 0
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [("write", "path"), ("search_replace", "filePath")],
)
def test_protect_grok_camelcase_allows_when_session_claim_covers_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool_name: str,
    path_key: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": tool_name, "toolInput": {path_key: "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_allows_a_lane_claim_covering_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guardrail (Entschieden #6): `_protect_write` already authorizes purely via
    agent/branch/scope, so a lane claim (no GitHub issue at all) passes through it
    unchanged, with no code path change required."""
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, {("branch", "--show-current"): "docs/lane-cleanup"})
    _patch_protect_claim(monkeypatch, branch="docs/lane-cleanup", lane=True)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_grok_camelcase_denies_write_without_this_session_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
    assert list(home.iterdir()) == []


def test_protect_absolute_file_path_allows_when_claim_scope_covers_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    target = work / "src" / "agent_claim" / "cli.py"

    assert (
        _protect_main(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(target.resolve())},
            },
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_dirty_worktree_still_allows_covered_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    (work / "dirty.txt").write_text("edited\n", encoding="utf-8")
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_missing_ledger_denies_claim_first_without_configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(
        github, "GitHubIssueComments", lambda repository: FakeComments()
    )
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: None)

    def unused_configure(issue: int) -> None:
        pytest.fail("missing ledger must not configure_ledger")

    monkeypatch.setattr(protocol, "configure_ledger", unused_configure)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


@pytest.mark.parametrize(
    "payload",
    ["not-json", "[]", "null", "1", '{"toolName": 1}', "{}"],
)
def test_protect_invalid_hook_payload_denies_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="invalid hook payload")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Write"},
        {"tool_name": "Edit", "tool_input": "src/widget.py"},
        {"toolName": "MultiEdit", "toolInput": {"contents": "x"}},
        {"toolName": "write", "toolInput": {"path": "", "file_path": ""}},
    ],
)
def test_protect_mutating_tool_without_path_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="path required")


def test_protect_missing_identity_denies_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_github_construction(monkeypatch)
    _forbid_git_fill(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    _assert_missing_identity_message(payload["reason"])


@pytest.mark.parametrize("branch", ["main", "master"])
def test_protect_main_branch_denies_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(
        monkeypatch, work, {("branch", "--show-current"): branch}
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not main")


def test_protect_primary_checkout_denies_worktree_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = str(work / ".git")
    _patch_protect_git(
        monkeypatch,
        work,
        {
            ("rev-parse", "--git-dir"): git_directory,
            ("rev-parse", "--git-common-dir"): git_directory,
        },
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="worktree")


def test_protect_path_outside_repository_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "write",
                "toolInput": {"path": str(tmp_path / "outside.py")},
            },
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="path required")


def test_protect_wrong_branch_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, branch="other/issue-72")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_non_overlapping_scope_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("docs",))

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_ledger_error_denies_json_without_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(
        github, "GitHubIssueComments", lambda repository: FakeComments()
    )

    def failed(_client):
        raise ClaimError("adapter failed")

    monkeypatch.setattr(discovery, "discover_ledger", failed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "ERROR:" not in captured.out
    assert json.loads(captured.out) == {"decision": "deny", "reason": "adapter failed"}


def test_protect_non_claim_error_from_write_path_denies_json_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(
        github, "GitHubIssueComments", lambda repository: FakeComments()
    )

    def crashed(_client):
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(discovery, "discover_ledger", crashed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "ERROR:" not in captured.out
    assert json.loads(captured.out) == {
        "decision": "deny",
        "reason": "write path crashed",
    }


def test_two_lanes_may_claim_the_same_file() -> None:
    client = FakeComments()
    first = acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    second = acquire_claim(
        client, request("claim-b", "Grok 4.6", issue=73, scope=("src/widget.py",))
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {first.claim_id, second.claim_id}
    holders = claims_holding_path(standing, "src/widget.py")
    assert {claim.claim_id for claim in holders} == {first.claim_id, second.claim_id}


def test_many_lanes_may_claim_the_same_directory() -> None:
    client = FakeComments()
    acquired = [
        acquire_claim(
            client,
            request(f"claim-{index}", f"Agent {index}", issue=100 + index, scope=("src",)),
        )
        for index in range(8)
    ]

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert len(standing) == 8
    assert {claim.claim_id for claim in standing} == {claim.claim_id for claim in acquired}


def test_same_issue_still_refuses_a_second_live_claim() -> None:
    client = FakeComments()
    acquire_claim(client, request(issue=72, scope=("src/a.py",)))

    with pytest.raises(ClaimUnavailable, match="issue #72 is claimed"):
        acquire_claim(
            client, request("claim-b", "Grok 4.6", issue=72, scope=("src/b.py",))
        )


def test_resource_allocates_unique_values_in_sequence() -> None:
    client = FakeComments()
    first = acquire_claim(
        client, request(issue=72, scope=("src/a.py",), resource="schema-hop")
    )
    second = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    assert first.resource == protocol.ResourceHold("schema-hop", 1)
    assert second.resource == protocol.ResourceHold("schema-hop", 2)
    first_body = client.comments[LEDGER_ISSUE][0].body
    assert "- Resource: `schema-hop`" in first_body
    assert "`schema-hop` =" not in first_body
    assert "resource_value" not in _marker_payload_keys(first_body)


def test_auto_resource_after_live_explicit_two_holds_one_not_none() -> None:
    client = FakeComments()
    explicit = acquire_claim(
        client,
        request(issue=72, scope=("src/a.py",), resource="schema-hop", resource_value=2),
    )
    auto = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = {claim.resource for claim in standing}
    assert explicit.resource == protocol.ResourceHold("schema-hop", 2)
    assert auto.resource == protocol.ResourceHold("schema-hop", 1)
    assert None not in holds
    assert holds == {
        protocol.ResourceHold("schema-hop", 1),
        protocol.ResourceHold("schema-hop", 2),
    }


def test_resource_refuses_a_second_live_hold_of_the_same_value() -> None:
    client = FakeComments()
    acquire_claim(
        client,
        request(issue=72, scope=("src/a.py",), resource="schema-hop", resource_value=4),
    )

    with pytest.raises(ClaimUnavailable, match="schema-hop 4 is held by Codex Sol"):
        acquire_claim(
            client,
            request(
                "claim-b",
                "Grok 4.6",
                issue=73,
                scope=("src/b.py",),
                resource="schema-hop",
                resource_value=4,
            ),
        )


def test_releasing_a_resource_drops_the_hold_and_keeps_later_values_unique() -> None:
    client = FakeComments()
    first = acquire_claim(
        client, request(issue=72, scope=("src/a.py",), resource="schema-hop")
    )
    release_claim(client, IssueIdentity(72), "Codex Sol", "builder", "abandoned", first.claim_id)
    acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.resource for claim in standing] == [protocol.ResourceHold("schema-hop", 2)]


def test_resource_race_later_auto_succeeds_with_the_next_value() -> None:
    client = FakeComments()
    earlier = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
                resource_value=1,
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    later = acquire_claim(
        client,
        request("later", "Codex Sol", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    by_id = {claim.claim_id: claim for claim in standing}
    assert later.resource == protocol.ResourceHold("schema-hop", 2)
    assert by_id["earlier"].resource == protocol.ResourceHold("schema-hop", 1)
    assert by_id["later"].resource == protocol.ResourceHold("schema-hop", 2)


def test_resource_race_explicit_value_still_fails_closed() -> None:
    client = FakeComments()
    earlier = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
                resource_value=1,
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    with pytest.raises(ClaimUnavailable, match="schema-hop 1 is held by Grok 4.6"):
        acquire_claim(
            client,
            request(
                "later",
                "Codex Sol",
                issue=73,
                scope=("src/b.py",),
                resource="schema-hop",
                resource_value=1,
            ),
        )


def test_two_intents_for_the_same_value_leave_exactly_one_holder() -> None:
    client = FakeComments(
        {
            LEDGER_ISSUE: [
                comment(
                    1,
                    claim_comment(
                        request(
                            issue=72,
                            scope=("src/a.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
                comment(
                    2,
                    claim_comment(
                        request(
                            "claim-b",
                            "Grok 4.6",
                            issue=73,
                            scope=("src/b.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
            ]
        }
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = [claim.resource for claim in standing if claim.resource is not None]
    assert holds == [protocol.ResourceHold("schema-hop", 1)]
    assert {claim.claim_id for claim in standing} == {"claim-a", "claim-b"}


def test_resource_loser_that_dies_before_retry_is_not_a_holder() -> None:
    client = FakeComments(
        {
            LEDGER_ISSUE: [
                comment(
                    1,
                    claim_comment(
                        request(
                            "first",
                            issue=72,
                            scope=("src/a.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
                comment(
                    2,
                    claim_comment(
                        request(
                            "loser",
                            "Grok 4.6",
                            issue=73,
                            scope=("src/b.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
            ]
        }
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    holders = [
        claim
        for claim in standing
        if claim.resource == protocol.ResourceHold("schema-hop", 1)
    ]
    assert [claim.claim_id for claim in holders] == ["first"]
    assert {claim.claim_id for claim in standing} == {"first", "loser"}
    assert all("## RELEASE" not in entry.body for entry in client.comments[LEDGER_ISSUE])


def test_cli_claim_resource_prints_the_allocated_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeComments()
    monkeypatch.setattr(github, "GitHubIssueComments", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["resource"] == "schema-hop"
    assert payload["resource_value"] == 1
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.resource is None
    assert posted.requested_resource == "schema-hop"
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.resource for claim in standing] == [protocol.ResourceHold("schema-hop", 1)]



def test_cli_two_claims_of_the_same_directory_are_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths: tuple(path for path in paths if path == "src"),
    )

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "dir-a",
            "--allow-directory",
            "shared directory",
        ]
    )
    capsys.readouterr()
    second = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src",
            "--claim-id",
            "dir-b",
            "--allow-directory",
            "shared directory",
        ]
    )
    claimed = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "CONFLICT" not in claimed
    assert "overlaps issue #72 (dir-a)" in claimed

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered
    assert "overlaps issue #73 (dir-b)" in rendered
    assert "overlaps issue #72 (dir-a)" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "who", "src"])
    holders = capsys.readouterr().out
    assert who == 0
    assert "CONFLICT" not in holders
    assert "CLAIMED src issue #72" in holders
    assert "CLAIMED src issue #73" in holders
    assert "overlap: issue #72 (dir-a), issue #73 (dir-b)" in holders


def test_cli_two_claims_of_the_same_file_are_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "file-a",
        ]
    )
    capsys.readouterr()
    second = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "file-b",
        ]
    )
    claimed = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "CONFLICT" not in claimed
    assert "overlaps issue #72 (file-a)" in claimed

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "who", "src/widget.py"])
    holders = capsys.readouterr().out
    assert who == 0
    assert "CONFLICT" not in holders
    assert "CLAIMED src/widget.py issue #72" in holders
    assert "CLAIMED src/widget.py issue #73" in holders
    assert "overlap: issue #72 (file-a), issue #73 (file-b)" in holders


def test_cli_two_resource_claims_allocate_one_then_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/a.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-1",
            "--json",
        ]
    )
    first_payload = json.loads(capsys.readouterr().out)
    second = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/b.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-2",
            "--json",
        ]
    )
    second_payload = json.loads(capsys.readouterr().out)

    assert first == 0
    assert second == 0
    assert first_payload["resource_value"] == 1
    assert second_payload["resource_value"] == 2


def test_cli_resource_race_still_yields_unique_live_holds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeComments()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    client.inject_after_next_ledger_post = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/b.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "later",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = sorted(
        claim.resource.value
        for claim in standing
        if claim.resource is not None and claim.resource.name == "schema-hop"
    )

    assert status == 0
    assert payload["resource_value"] == 2
    assert holds == [1, 2]


def test_who_lists_every_holder_without_calling_overlap_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, scope=("src/widget.py",)),
        request("theirs", "Grok 4.6", issue=73, scope=("src/widget.py",)),
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "who", "src/widget.py"])
    rendered = capsys.readouterr().out

    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED src/widget.py issue #72" in rendered
    assert "CLAIMED src/widget.py issue #73" in rendered
    assert "overlap: issue #72 (mine), issue #73 (theirs)" in rendered


def test_ruled_expectations_without_a_date_fail_loud() -> None:
    issue = board_issue(
        10,
        "Undated",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*", heading="Erwartung"),
    )

    with pytest.raises(ClaimError, match="no readable date"):
        board.build_board(
            (issue,),
            (),
            (),
            (),
            board.BoardConfig(),
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )


def test_proposed_expectations_have_neither_fresh_nor_old() -> None:
    issue = board_issue(
        10,
        "Proposed",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(Default: yes)*"),
    )
    projected = board.build_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=timezone.utc)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED
    assert projected.items[0].ruling_landings is None
    assert projected.items[0].ruling_old is None


def test_a_ruling_is_old_after_ten_trunk_landings() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    landings = tuple(
        datetime(2026, 8, 29, hour, tzinfo=timezone.utc) for hour in range(10)
    )
    projected = board.build_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        trunk_landings=landings,
    )
    item = projected.items[0]

    assert item.ruling_landings == 10
    assert item.ruling_old is True
    assert "ruled 10 old" in board.render(projected)


def test_one_trunk_landing_does_not_make_a_ruling_old() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    projected = board.build_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        trunk_landings=(datetime(2026, 8, 29, tzinfo=timezone.utc),),
    )

    assert projected.items[0].ruling_landings == 1
    assert projected.items[0].ruling_old is False


def test_same_day_trunk_landings_do_not_age_a_date_only_ruling() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    projected = board.build_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        trunk_landings=(datetime(2026, 8, 28, 23, tzinfo=timezone.utc),),
    )

    assert projected.items[0].ruling_landings == 0
    assert projected.items[0].ruling_old is False


def test_operator_ruling_date_wins_over_another_heading_date() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen (refine-Lauf 01.08.2026 - GEREGELT: Operator 28.08.2026)",
        ),
    )
    landings = (
        datetime(2026, 8, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    projected = board.build_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        trunk_landings=landings,
    )

    assert projected.items[0].ruling_landings == 1


def test_distinct_heading_dates_without_an_operator_date_fail_loud() -> None:
    issue = board_issue(
        10,
        "Ambiguous",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen (01.08.2026 and 28.08.2026)",
        ),
    )

    with pytest.raises(ClaimError, match="more than one date"):
        board.build_board(
            (issue,),
            (),
            (),
            (),
            board.BoardConfig(),
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )


def test_next_names_an_old_ruling_when_the_item_is_pulled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubIssueComments", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(
        checkout,
        "trunk_landing_times",
        lambda: tuple(datetime(2026, 8, 29, hour, tzinfo=timezone.utc) for hour in range(10)),
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Work\n"
        "Next: Claim #10.\n"
        "vor 10 Landungen geregelt, beim Ziehen neu refinen\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ruling_landings"] == 10
    assert payload["ruling_old"] is True
    assert payload["ruling_hint"] == "vor 10 Landungen geregelt, beim Ziehen neu refinen"


def test_each_item_carries_its_own_ruling_age() -> None:
    fresh = board_issue(
        10,
        "Fresh",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartung (refine-Lauf 28.08.2026)",
        ),
    )
    old = board_issue(
        11,
        "Old",
        complete_contract("Claim #11.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartung (refine-Lauf 01.08.2026)",
        ),
    )
    landings = tuple(datetime(2026, 8, 10 + index, tzinfo=timezone.utc) for index in range(12))
    projected = board.build_board(
        (fresh, old),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        trunk_landings=landings,
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[10].ruling_old is False
    assert by_number[11].ruling_old is True
    assert by_number[11].ruling_landings == 12


def test_identity_conflict_still_marks_status_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("src/a.py",))))
    )
    second = parse_claim_event(
        comment(
            2,
            claim_comment(request("claim-b", "Grok 4.6", issue=72, scope=("src/b.py",))),
        )
    )
    assert first is not None and second is not None

    assert _status((first, second), None) == 2
    rendered = capsys.readouterr().out
    assert rendered.count("CONFLICT") == 2


def test_trunk_landing_times_read_the_default_branch_not_the_work_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def git_output(arguments: list[str]) -> str:
        observed.append(arguments)
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[:4] == ["log", "--first-parent", "--reverse", "--format=%cI"]:
            assert arguments[4] == "refs/remotes/origin/main"
            return "2026-08-29T00:00:00+00:00\n2026-08-30T00:00:00Z"
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    times = _LIVE_TRUNK_LANDING_TIMES()

    assert times == (
        datetime(2026, 8, 29, tzinfo=timezone.utc),
        datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    assert [
        "log",
        "--first-parent",
        "--reverse",
        "--format=%cI",
        "refs/remotes/origin/main",
    ] in observed



def test_trunk_landing_times_count_a_five_commit_merge_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    (repo / "file.txt").write_text("0\n")
    git("add", "file.txt")
    git("commit", "-m", "initial")
    git("checkout", "-b", "feature")
    for index in range(1, 6):
        (repo / "file.txt").write_text(f"{index}\n")
        git("add", "file.txt")
        git("commit", "-m", f"commit-{index}")
    git("checkout", "main")
    git("merge", "--no-ff", "-m", "merge feature", "feature")
    git("checkout", "-b", "work")
    monkeypatch.chdir(repo)

    unrestricted = git("log", "--reverse", "--format=%cI").stdout.splitlines()
    assert len(unrestricted) == 7
    assert len(_LIVE_TRUNK_LANDING_TIMES()) == 2


def test_no_path_class_list_is_read_or_written() -> None:
    assert not Path("src/agent_claim").joinpath("single_writer.py").exists()
    text = Path("src/agent_claim/protocol.py").read_text()
    assert "single-writer" not in text
    assert "single_writer" not in text
