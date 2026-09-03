# agent-claim

`agent-claim` is a small installable CLI that gives coding agents one
append-only, locked GitHub issue ledger per repository. It is provider-neutral:
Codex, Claude, Grok, people, and future agents use the same contract.

## Install and maintain

```bash
uv tool install git+https://github.com/FlexOr2/agent-claim.git@v0.7.0
# or: pipx install git+https://github.com/FlexOr2/agent-claim.git@v0.7.0
uv tool upgrade agent-claim
uv tool uninstall agent-claim
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/FlexOr2/agent-claim.git@v0.6.0`.

## Five-command quick start

```bash
agent-claim bootstrap
agent-claim status
agent-claim claim 42 --agent "Ada" --scope src/widget.py
agent-claim release 42
agent-claim reconcile
```

Omitted `--base`/`--branch` bind the current checkout; explicit values must match it.
Omitted `--agent` on `claim` and `release` is filled from non-empty
`AGENT_CLAIM_AGENT`, else non-empty `GROK_SESSION_ID` as `Grok {session}`, else
non-empty `CLAUDE_SESSION_ID` as `Claude {session}`. `GROK_AGENT` is not a name.
Missing or present-invalid identity fails closed before GitHub work. Omitted
`--role` on `claim` is `builder`; an explicit `--role` wins. Repeating an
interrupted `claim` for the same active item, agent, role, branch, and scope
returns that active claim's existing ID without posting a second claim. A
different live claim still fails; a released claim ID remains terminal.

Omitted `--claim-id` on `release` selects the unique active claim on that issue
or lane whose agent is this session and whose branch is the current checkout;
otherwise it fails closed.
Omitted `--role` on `release` uses that selected claim's role; an explicit
`--role` must still match unless `--coordinator-override`. Omitted `--reason` on
`release` is `landed`. `--coordinator-override` still requires `--role coordinator` and
`--reason`. `supersede` still requires `--agent` and `--role`. A `--claim-id` already
present on the ledger, active or released, is refused before anything is posted;
release the old claim and pass a fresh `--claim-id` instead.
`rescope <issue> --add <path> [--drop <path>]` changes a live claim's scope
without releasing it: the claim id and base stay, added paths are advisory
like `claim`, `--add` of a directory or a combined share above a quarter uses
the same cut / `--allow-directory` rules as `claim`, and there is no release
window. It does not require HEAD to match
base or a clean tree. A `rescope` ledger event is a new v2 action; older
helpers fail loud on the whole ledger until they upgrade.

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. `--scope a,b` is
the same as `--scope a --scope b`; each path is stored and compared
separately, including when an older ledger comment still has one comma-joined
string. Directory scopes (`docs`, `frontend/src`) lock the whole tree: claim one only
when the issue body has a `## Schnitt` cut with at least one `**Scheibe n: ...**`
slice, or pass `--allow-directory REASON`. A scope covering more than a quarter
of versioned files also needs that flag. Live claims are advisory: they say who
works where and do not refuse path overlap. Two lanes may claim the same
directory or the same file; `claim` and `status` print the overlap as a note.
The same issue or the same `docs/`/`fix/` lane branch still holds at most one
live claim. `claim --resource <name>` posts a name-only intent; the live integer is the next
positive value not occupied by an earlier first-occurrence request for that name. An explicit
posted value occupies that integer even after release; a released auto still occupies the
integer it would have been assigned. A second live hold of the same name and value is
refused: only the earliest live claim of that pair is the holder. Sequential allocations
stay unique even after a release. `claim` prints
how many versioned files the scope covers and which open claims it overlaps.
`who <path>` prints every live claim that holds a path.
Agents should read `--json` from `status`, `claim`, `release`, `rescope`, and `who`.
`status` prints each live claim's age from its claim comment as `Xh Ym`, and
marks it `old` after more than one hour.

`bootstrap` adopts the exact `<!-- agent-claim-ledger:v1 -->` issue marker,
ensures it is locked and labelled, and safely converges concurrent first starts
to the earliest ledger, visibly closing later duplicates. It refuses to compete
when another machine-readable claim/ledger contract exists. A claimed issue gets
one reusable minimal projection comment and a generation-scoped label.
Use `release --coordinator-override` only for an explicit coordinator action.
Ledger rollover (`supersede`) requires a coordinator whose named claim is the
only active claim and owns the ledger issue; the successor is a higher-numbered
open empty collaborator-locked issue, and the freeze is atomic.
`reconcile` also repairs a duplicated claim id it finds on the ledger, keeping the
newest occurrence and printing one `REPAIRED claim '<id>': superseded <comments> ->
survivor #<comment>` line per id it fixes, where `<comments>` lists every superseded
comment it neutralized (the older CLAIM plus each terminal comment that honored its
release — there can be more than one, e.g. a release retry) as `#id, #id, ...`.
An older occurrence only auto-repairs when it is already released, or when it
shares the survivor's agent and role (a same-agent re-claim, kept newest because
that reflects the agent's latest intent — this is not scoped to one identity, so
a same-agent duplicate spanning two issues, two lanes, or an issue and a lane
still only keeps the newer identity's workstream and silently ends the older
one).
A duplicate still active under two different agents is a real ownership
conflict; `reconcile` reports it and leaves the whole ledger untouched — for
every duplicated id, not just the conflicting one — instead of picking a winner.

## Read-only board projection

`agent-claim board` reads the open issues, open PRs, PRs merged since the
oldest open issue was filed, and the claim ledger, then prints a ranked
projection with `READY NOW` and `STALE` sections. A pull request that
advances an issue without closing it — an epic's dispatched slice, typically
— credits that issue when the pull request names it a second time outside a
dedicated `Refs #N`/`Part of #N` line; that is a syntactic marker, not a
verified relation, so an unrelated pull request naming the same issue twice
by coincidence would still credit it.

The table exposes which exact contract headings were found, an
`EXPECT` state (`-`, `proposed`, or `ruled N` / `ruled N old`), a concise `Next`, and a CLAIM
cell with `-` or the agent, role, claim age, and `old` when the claim comment
is older than one hour; JSON includes the complete derived contract state. An `Erwartung`, `Erwartungen`, or
`Erwartungsliste` heading makes the following block an expectation list: a line
with `*(Default: yes|no|later)*` is proposed. A block is ruled only when every
expectation line carries a `*(geregelt: ja)*` or `*(geregelt: NEIN ...)*`
marker; absent or malformed markers remain proposed. A ruled block also shows
how many default-branch first-parent landings (`git log --first-parent`
committer times) happened after its heading date (`DD.MM.YYYY`, preferring
`GEREGELT: Operator …`); ten or more mark it `old`. Missing or proposed
expectations have neither fresh nor old. If a ruled block has no readable date
or git cannot name the default branch, that is an error, never silently fresh.
It never writes GitHub.
The target defaults to the repository of the current checkout;
for another GitHub repository run `agent-claim --repo FlexOr2/atelier-2 board`.
The current checkout may set `priority_labels` as an ordered non-empty list in
`.agent-claim/board.toml`; absent configuration uses `security`, `data`, `ci`,
`product`, `ux`, then `cleanup`. The first three configured labels are primary
buckets, followed by items that unblock other work, then the remaining labels;
stage and Next bonuses sort only within a bucket.

Use `agent-claim next` (or `agent-claim next --json`) to name the board's
top-ranked actionable item — the same bucket-then-score-then-number order
`board` shows, read from its first row: it is open, free, unblocked, not
frozen, and has a complete Now/Next/Blocked by/Done when contract. Pulling is
not dispatching, so unruled expectations never withhold an item; the pulled
item carries `Erwartungen
ungeregelt, beim Ziehen zuerst refinen` instead, and an item ruled long ago
carries `vor N Landungen geregelt, beim Ziehen neu refinen` (both as the JSON
`ruling_hint`). Items that genuinely cannot be worked — claimed, blocked by an
open issue, frozen, or without a complete contract — are named with that
reason under `SKIPPED` (also in the JSON `skipped` list), and `next` exits 3
when none qualifies.
`claim` refuses work out of order when a higher-priority actionable item — the
same order `board` and `next` use — is free. Pass `--out-of-order REASON` to
proceed deliberately; it remains visible as a warning and preserves the reason
in the claim comment.

A body line `Eingefroren bis: <trigger in one sentence> (Operator, DD.MM.YYYY)`
freezes an issue: it drops out of `next` and the higher-priority refusal check
even though its score keeps showing on `board`, and deleting the line thaws it
again. The tool only checks the line's form, never who wrote it — that
authority is the coordination contract's. It reads the body the way GitHub
renders it: a marker inside a fenced code block (` ``` ` or `~~~`, including
one left unclosed to the end of the body) is documentation, never a live
marker — examples belong in a fence. A blockquoted `> Eingefroren bis: …`
still freezes; this repo already quotes operator rulings, so a quoted freeze
line reads as the freeze itself.

## Issueless lane claims

`docs/`- and `fix/`-prefixed branches land within one session without a GitHub
issue. Omit the positional issue number on `claim`/`release` for this lane mode,
derived from the current checkout branch — no separate `--lane` flag. Lane mode
is refused with the offending branch name and both remedies (pass an issue
number, or check out a `docs/`/`fix/` branch) when the branch does not follow
that convention, so a builder who simply forgot the issue number never gets a
silent, unlabeled claim:

```bash
git worktree add ../repo-worktrees/docs-tidy-readme -b docs/tidy-readme
cd ../repo-worktrees/docs-tidy-readme
agent-claim claim --agent "Ada" --scope README.md
agent-claim release
```

Like an issue claim, a lane claim must begin from a clean linked worktree
checked out on that branch — `claim` fails outside one.

A lane claim shares the same identity exclusivity, advisory overlap notes, and
release path as an issue claim: two lane claims collide on the same branch;
overlapping scope with another lane or issue is a visible note, not a refusal.
`status` and `protect` show and authorize it the same way. A lane owns no
GitHub issue, so it gets no projection comment or label, and `reconcile` never
touches it.

There is no flag to name a lane explicitly on `release`: a lane's only name is
the checkout branch it was claimed from, so releasing it — including a
coordinator override — always runs from a checkout of that same lane branch.
If the original worktree is gone or held by another session, re-create a
worktree on that branch (`git worktree add <path> <lane-branch>`) and run
`agent-claim release --claim-id <id> --coordinator-override --role coordinator
--reason "..."` from inside it, where `<id>` comes from `agent-claim status`
(omitting `--claim-id` still filters by the releasing agent, coordinator
override or not, so a foreign stuck claim needs the id).

The lane-claim marker extends the same `agent-claim:v2` event, but with a
different key set than an issue claim. A pre-issue-38 `agent-claim` cannot
parse it: it fails loud on the whole ledger, not just the lane claim, until it
upgrades — deliberate, since an agent that cannot read the live locks must not
build blindly. Upgrade every `agent-claim` installation together with (or
before) the first lane claim posted to a shared ledger.

## Global loader

Run `agent-claim policy --print` and append the block once into the file the
provider actually loads. Skip the append when `<!-- agent-claim-policy:v1 -->`
is already present. Never overwrite an existing loader. The CLI does not write
`~/.claude`, `~/.codex`, or `~/.grok`.

## PreToolUse write gate

Copy this hook once into the file the provider actually loads. Skip when
`Write|Edit|MultiEdit|write|search_replace` is already present. Never overwrite
an existing hook file. The CLI does not write `~/.grok`.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|write|search_replace",
        "hooks": [
          {
            "type": "command",
            "command": "agent-claim protect",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

## v0.5 boundary

GitHub via the `gh` CLI is supported today. Invocations set `NO_COLOR=1`
and `GH_NO_UPDATE_NOTIFIER=1`, strip ANSI from output, and parse pretty or
compact JSON, so a wrapping `gh` shim is not required. The tool does not
automatically allocate work, merge code, or operate a lease server. Omitted `--agent` follows
the documented else-chain; it does not invent an identity. It intentionally
leaves policy-file generation and non-GitHub adapters for a later release.
