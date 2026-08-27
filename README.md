# agent-claim

`agent-claim` is a small installable CLI that gives coding agents one
append-only, locked GitHub issue ledger per repository. It is provider-neutral:
Codex, Claude, Grok, people, and future agents use the same contract.

## Install and maintain

```bash
uv tool install git+https://github.com/FlexOr2/agent-claim.git@v0.5.0
# or: pipx install git+https://github.com/FlexOr2/agent-claim.git@v0.5.0
uv tool upgrade agent-claim
uv tool uninstall agent-claim
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/FlexOr2/agent-claim.git@v0.4.0`.

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
`--role` on `claim` is `builder`; an explicit `--role` wins. Omitted `--claim-id`
on `release` selects the unique active claim on that issue or lane whose agent
is this session and whose branch is the current checkout; otherwise it fails
closed.
Omitted `--role` on `release` uses that selected claim's role; an explicit
`--role` must still match unless `--coordinator-override`. Omitted `--reason` on
`release` is `landed`. `--coordinator-override` still requires `--role coordinator` and
`--reason`. `supersede` still requires `--agent` and `--role`. A `--claim-id` already
present on the ledger, active or released, is refused before anything is posted;
release the old claim and pass a fresh `--claim-id` instead.
`rescope <issue> --add <path> [--drop <path>]` changes a live claim's scope
without releasing it: the claim id and base stay, added paths are exclusive
like `claim`, dropping is not refused for an existing remainder overlap, and
there is no release window. It does not require HEAD to match
base or a clean tree. A `rescope` ledger event is a new v2 action; older
helpers fail loud on the whole ledger until they upgrade.

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. `--scope a,b` is
the same as `--scope a --scope b`; each path is stored and compared
separately, including when an older ledger comment still has one comma-joined
string. Directory scopes (`docs`, `frontend/src`) lock the whole tree and are
refused unless `--allow-directory REASON` is set. Claims collide on the same
issue or overlapping paths; disjoint scopes can proceed concurrently.
`who <path>` prints the live claim that holds a path.
Agents should read `--json` from `status`, `claim`, `release`, `rescope`, and `who`.

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

`agent-claim board` reads the open issues, open PRs, PRs merged in the last 14
days, and the claim ledger, then prints a ranked projection with `READY NOW` and
`STALE` sections. The table exposes which exact contract headings were found
and a concise `Next`; JSON includes the complete derived contract state. It
never writes GitHub. The target defaults to the repository of the current checkout;
for another GitHub repository run `agent-claim --repo FlexOr2/atelier-2 board`.
The current checkout may set `priority_labels` as an ordered non-empty list in
`.agent-claim/board.toml`; absent configuration uses `security`, `data`, `ci`,
`product`, `ux`, then `cleanup`. The first three configured labels are primary
buckets, followed by items that unblock other work, then the remaining labels;
stage and Next bonuses sort only within a bucket.

Use `agent-claim next` (or `agent-claim next --json`) to name the highest-scored
actionable item: it is open, free, unblocked, and has a complete
Now/Next/Blocked by/Done when contract; it exits 3 when none qualifies. `claim`
still allows work out of order, but warns when a higher-scored actionable item is
free; pass `--out-of-order REASON` to preserve why in the claim comment.

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

A lane claim shares the same exclusivity, scope-conflict rules, and release
path as an issue claim: two lane claims collide on the same branch or an
overlapping scope, and a lane collides with an issue claim over scope overlap.
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
