# agent-claim

`agent-claim` is a small installable CLI that gives coding agents one
append-only, locked GitHub issue ledger per repository. It is provider-neutral:
Codex, Claude, Grok, people, and future agents use the same contract.

## Install and maintain

```bash
uv tool install git+https://github.com/FlexOr2/agent-claim.git@v0.1.0
# or: pipx install git+https://github.com/FlexOr2/agent-claim.git@v0.1.0
uv tool upgrade agent-claim
uv tool uninstall agent-claim
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/FlexOr2/agent-claim.git@v0.1.0`.

## Five-command quick start

```bash
agent-claim bootstrap
agent-claim status
agent-claim claim 42 --agent "Ada" --role builder --base "$(git rev-parse HEAD)" --branch "$(git branch --show-current)" --scope src/widget.py
agent-claim release 42 --claim-id <id> --agent "Ada" --role builder --reason landed
agent-claim reconcile
```

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. Claims collide on the
same issue or overlapping paths; disjoint scopes can proceed concurrently.

`bootstrap` adopts the exact `<!-- agent-claim-ledger:v1 -->` issue marker,
ensures it is locked and labelled, and safely converges concurrent first starts
to the earliest ledger, visibly closing later duplicates. It refuses to compete
when another machine-readable claim/ledger contract exists. A claimed issue gets
one reusable minimal projection comment and a generation-scoped label.
Use `release --coordinator-override` only for an explicit coordinator action.
Ledger rollover (`supersede`) is intentionally unavailable in v0.1 and deferred
to a follow-up issue; the command fails before making a GitHub mutation.

## v0.1 boundary

GitHub via the `gh` CLI is supported today. The tool does not automatically
allocate work, merge code, operate a lease server, or infer an agent identity.
It intentionally leaves policy-file generation and non-GitHub adapters for a
later release.
