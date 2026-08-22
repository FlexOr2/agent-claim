# agent-claim

`agent-claim` is a small installable CLI that gives coding agents one
append-only, locked GitHub issue ledger per repository. It is provider-neutral:
Codex, Claude, Grok, people, and future agents use the same contract.

## Install and maintain

```bash
uv tool install git+https://github.com/FlexOr2/agent-claim.git@v0.4.0
# or: pipx install git+https://github.com/FlexOr2/agent-claim.git@v0.4.0
uv tool upgrade agent-claim
uv tool uninstall agent-claim
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/FlexOr2/agent-claim.git@v0.3.0`.

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
on `release` selects the unique active claim on that issue whose agent is this
session and whose branch is the current checkout; otherwise it fails closed.
Omitted `--role` on `release` uses that selected claim's role; an explicit
`--role` must still match unless `--coordinator-override`. Omitted `--reason` on
`release` is `landed`. `--coordinator-override` still requires `--role coordinator` and
`--reason`. `supersede` still requires `--agent` and `--role`.

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. Claims collide on the
same issue or overlapping paths; disjoint scopes can proceed concurrently.
Agents should read `--json` from `status`, `claim`, and `release`.

`bootstrap` adopts the exact `<!-- agent-claim-ledger:v1 -->` issue marker,
ensures it is locked and labelled, and safely converges concurrent first starts
to the earliest ledger, visibly closing later duplicates. It refuses to compete
when another machine-readable claim/ledger contract exists. A claimed issue gets
one reusable minimal projection comment and a generation-scoped label.
Use `release --coordinator-override` only for an explicit coordinator action.
Ledger rollover (`supersede`) requires a coordinator whose named claim is the
only active claim and owns the ledger issue; the successor is a higher-numbered
open empty collaborator-locked issue, and the freeze is atomic.

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

## v0.4 boundary

GitHub via the `gh` CLI is supported today. The tool does not automatically
allocate work, merge code, or operate a lease server. Omitted `--agent` follows
the documented else-chain; it does not invent an identity. It intentionally
leaves policy-file generation and non-GitHub adapters for a later release.
