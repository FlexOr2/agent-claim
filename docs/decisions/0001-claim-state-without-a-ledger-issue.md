# 0001 — Claim state without a ledger issue

Status: accepted as direction, not yet implementable. Each §4 criterion is gated by the §5
step that resolves it: the state-ref cut (step 5) resolves criteria 1–6 and 10 (criterion 8
is already tied there to fencing v0.9 clients); port extraction (step 3) resolves criterion 9,
the complete Landing candidate (already tied there); criterion 7, the GitLab
state-transport preflight, must be re-run before the first GitLab adapter; criterion 11, the
live GitHub custom-ref probe, is satisfied (re-run by the head 05.09.2026, results on #131).
No slice may be dispatched before its gating step's criteria are satisfied.

Date: 05.09.2026. Baseline: `origin/main` = `2bcf3f3` (v0.9.0 plus the #109/#110 Sonar
cleanup, which changed no contract), tag `v0.9.0`.

Source: a two-round architecture concept and two independent counter-checks, both
returning REBUILD. This record harvests them. Where a counter-check contradicts the
concept, the counter-check's version stands, and it is written here as such.

## 1. Context: what the ledger issue costs today

`agent-claim` keeps every claim in one GitHub issue whose comments are the ledger.
Five costs are measured or read from the v0.9.0 source, not assumed.

| Problem | Evidence |
|---|---|
| A read of the whole ledger on every guarded write | `protect` re-reads the live ledger before each write tool call. Reported cost ~2.4 s per call (unverified, §6) |
| The same claim written in three places | The ledger comment owns it; an item projection comment (`upsert_projection` in `github.py`) and a generation label repeat it, and only `reconcile` repairs the drift |
| Acquisition is not atomic, only late-compensating | `_acquire_claim_with_observed` posts, then re-runs `blocking_claims`; the loser posts a compensating release. Three writes on the losing path. (The concept claimed both commands simply succeed; the round-1 counter-check corrected that — it is compensating, not unguarded) |
| Item contracts parsed by regex over prose in two languages | `board.py` compiles 28 patterns over `nichts`, `geregelt: ja\|NEIN`, `Eingefroren bis:`, `## Schnitt`, `**Scheibe n:**`, plus a markdown slice table with two malformed-input defect classes |
| No provider seam | `cli.py` shells out to `gh api` directly and both `cli.py` and `github.py` branch on the substring `"HTTP 404"`. A GitLab adapter has nowhere to attach |

## 2. Decision

**One state ref.** `refs/agent-claim/state` on one configured canonical remote points at a
commit whose tree *is* the current state: `claims/<key>.toml`, `resources/<name>.toml`, a
schema `version`. The tree owns current state; the commit message owns the transition. A
released claim is simply absent from the new tree and readable at the parent commit — there
is no second history ref.

**Advanced by ordinary fast-forward pushes, not `--force-with-lease`.** Git documents that
an explicit matching lease *overrides* the normal fast-forward restriction, so a correct
lease can still replace history. A plain push to a custom ref already accepts fast-forwards
and rejects non-fast-forwards, which is exactly the serialization needed, without opening
the history-loss path. This corrects the concept, which specified a lease.

**Every transition is a commit that applies a typed intent to the observed state.** The
client fetches the ref, parses the tree, and calls a pure `apply(observed_state, intent)`;
the result is committed on the observed tip and pushed. A rejected push means the state
moved: re-read, re-apply *the original intent* to the new state, retry. The transitions are
claim, rescope, release (the landed `MergedRelease | AbandonedRelease` union, reused
verbatim), coordinator override, and stale takeover.

**Resource allocation happens inside the same commit as the claim that requests it.** This
is what makes repository-global integer uniqueness a consequence of the CAS rather than a
separate invariant: two lanes requesting the same resource serialize on the one ref, and the
loser re-applies its intent and gets the next integer. Both counter-checks agree this is the
only shape whose cross-key invariant does not rest on an unverified server capability.

**A typed fenced TOML block replaces regex prose in item bodies.** One `agent-claim` fenced
region parsed by `tomllib`. **Keys are protocol and are always English; values are the
operator's prose and are never parsed.** This retires the German markers, the slice table,
and both malformed-input defect classes.

**Blockers and parentage live on the forge's structured relations where the pinned storage
configuration says so.** Capabilities *validate* the pin; they never *choose* the owner. The
round-2 counter-check named runtime capability selection as an unresolved defect in the
concept: a forge plan change must never silently move a fact's owner without a migration.

**A provider port with a capability enum `UNSUPPORTED | READ_ONLY | READ_WRITE`,** replacing
the concept's `Support(read, write)` pair, which could represent the illegal
`read=False, write=True`. The adapter owns every fallback so no caller branches on a
capability. Forge failures are typed (`ForgeUnsupported`, `ForgePermissionDenied`,
`ForgeNotFound`, `ForgeTransient`, `ForgeMalformedResponse`) instead of substring-matched.

**Configuration stays in `.agent-claim/board.toml`,** extended only with settings that have a
caller today: the pinned storage strategy, the pinned body contract, and a canonical-remote
override. Renaming the file is rejected — it would cost a migration to buy a better name.

## 3. Rejected alternatives

| Alternative | The sentence that killed it |
|---|---|
| Per-item claim comments | The comment API exposes no conditional write anywhere the design would use, so two agents can both post and neither loses — and issueless lanes have no issue to hold a comment |
| Assignee as the lock | It is additive on GitHub and last-writer-replacement on GitLab Free, has no CAS on either, and names a forge account rather than the agent and session that holds the claim |
| Projects v2 fields | No demonstrated CAS, an extra item-synchronization step, GraphQL coupling, and no GitLab Free equivalent (the concept's original reason — a missing token scope — is not architectural, since a scope can be granted) |
| Per-key refs plus a resources ref | A resource-holding claim would have its hold in `claims/<key>` and its integer in `resources`, two owners that can disagree, and making them one transition needs `git push --atomic` across refs, whose support on both SaaS hosts is unverified |

## 4. Unresolved before the first slice may be dispatched

Every remaining defect from the round-2 counter-check, as acceptance criteria the slice's
plan review must satisfy. The direction in §2 is ruled; these are not.

1. **Lost lineage.** An ancestry check against a cached OID does not detect a forced replace
   that branched before the cached point, and a fresh clone has no comparison point at all.
   The stamp does not keep the old OID reachable, so a lost suffix may be unrecoverable.
   Required: a detection rule that states what it cannot see, and a written recovery
   procedure (quiesce writers, collect surviving tips, validate trees and ancestry, restore
   with an explicit expected old OID).
2. **ABA and claim generation.** v0.9.0 owns a public `claim_id` and uses it for interrupted
   replay. The concept removed it, so a retry cannot distinguish its own replay from a
   successor claim on the same key. Required: a typed `ClaimId` generation carried by every
   transition and revalidated on every retry.
3. **Ambiguous push completion.** "Push accepted, response lost" is unspecified. Required: an
   operation id written into the commit so a client can re-read and recognize its own
   transition before deciding to retry.
4. **Clock policy for stale takeover.** `opened_at` moves from GitHub's server timestamp to
   the claimant's clock; a slow clock makes a fresh claim instantly takeable and a fast clock
   blocks takeover indefinitely. `apply` also has no `now` input. Required: either a
   provider-neutral authoritative-time contract or stale takeover as an explicit coordinator
   action only.
5. **Retry exhaustion under ten writers.** The two-racer experiment does not prove the
   ten-lane estimate; the collision window opens when state is *observed*, not when the push
   starts. Required: a ten-process contention test and explicit exhaustion semantics —
   including that a different-key loser must not fail by "naming the current holder", because
   no conflicting holder exists.
6. **Bootstrap only from a proven-empty remote.** `fetch` exit 128 also covers auth and
   transport failure. Only a successful `ls-remote --exit-code` with its documented
   no-matching-ref result may become `EmptyState`; a ref absent after a client once observed
   it is deletion or corruption, never fresh bootstrap.
7. **GitLab state-transport preflight.** Custom-ref pushability on GitLab is unverified, and
   `ForgeCapabilities` has no state-transport capability at all. Required, before the first
   GitLab adapter: a preflight covering custom-ref create, fetch, fast-forward update,
   concurrent rejection, non-fast-forward rejection, absence, permission failure, and nested
   namespace URLs.
8. **Migration that loses nothing and fences old clients.** The slice-table parser must
   survive until the body migration has consumed it, not be deleted a slice earlier. The body
   migration must be lossless and resumable, and must refuse — by name — any legacy slice or
   expectation whose required fields cannot be derived rather than invent them. And v0.9.0
   silently ignores unknown `board.toml` keys, so a pinned config alone does not fence it:
   the state cut needs a tombstone the *old* client reads and refuses on.
9. **A complete `Landing` candidate.** The proposed
   `Landing(number, merged_at, work_item, head_branch, merge_commit)` cannot carry #108's
   `pr-check` and merged-release verification contract, which also reads an author, PR body
   and classification, target and default branch, source repository, closing references, and
   the no-item case. The port must preserve that contract before anything is rewired to it.
10. **The typed-model gaps.** A collision-free reversible codec for `LaneKey.branch` as a
    file name; a claim transition carrying a resource *intent* rather than a preallocated
    allocation, with one allocation owner instead of the hold appearing in both the claim and
    the ledger; an acting identity carrying agent *and* validated role, since `apply` must
    preserve today's `(agent, role)` authorization; a runtime-validating OID type (`NewType`
    validates nothing); immutable collections inside the frozen state; item created/updated
    timestamps the board needs; a body-update operation the migration needs; a
    provider-neutral kind mapping — `idea_label` is still a competing owner of kind;
    `RuledExpectation.default` with a typed `yes | no | later`; and a typed failure for a
    malformed or unsupported state-tree schema, distinct from a stale rejection and from
    ambiguous push completion.
11. **GitHub custom-ref probe re-run — satisfied for GitHub.** The live re-run of the
    custom-ref push/fetch/CAS probe against GitHub — create-if-absent, fast-forward update,
    non-fast-forward rejection, concurrent rejection, and delete — landed 05.09.2026 (results
    on #131); §6 now marks the GitHub custom-ref facts verified. The GitLab equivalent
    (criterion 7) stays open.

Removed until a caller exists: chain-length reporting, a public `capabilities()` if only
adapters consume it, nonzero gate freshness, and the receipt unless phone visibility is
explicitly selected.

## 5. Migration order

As ruled by the round-2 counter-check. Strictly sequential.

1. **#113 revalidated** — one wide-scope predicate on `claim` and `rescope`. It keeps
   `parse_slice_table` alive until step 4 has converted the existing slice data.
2. **#112 re-cut around item kind** — D3 is ruled; dispatchable once the cross-forge kind
   mapping exists.
3. **Port extraction** — `ForgeReader` / `ForgeWriter`, the GitHub adapter, typed errors. A
   pure extraction against the current call set, with no behaviour change, and it must
   already satisfy criterion 9. Criterion 11's live GitHub custom-ref probe is satisfied
   (re-run 05.09.2026, results on #131).
4. **One body and relation migration** — the typed block, blockers and parentage from
   structured relations, and the conversion command, in one slice. Blockers never pass
   through the body on GitHub.
5. **One state-ref cut** — claim, rescope, both release outcomes, coordinator override, stale
   takeover, resource allocation and `protect`, together. It must import active claims, claim
   ids and every consumed released allocation value, and fence v0.9 clients (criterion 8).
6. **The receipt, last** — optional and additive; its absence costs visibility, never
   correctness.

## 6. Measured facts

Judged as the counter-checks judged them. "Reported live" means the concept author ran it on
05.09.2026 against `overnightworks/agent-claim` and neither counter-check could reproduce it
(both lost network access to `api.github.com`); a fact still marked "Reported live,
unverified" below is therefore not independently verified and no slice may depend on it
without re-running the probe. The GitHub custom-ref probe was re-run live by the head on
05.09.2026 (results on #131) and is marked verified below; the org-plan and issue-type
probes remain unverified.

| Fact | Standing | Source |
|---|---|---|
| GitHub accepts pushes to `refs/agent-claim/*`; create-if-absent, fast-forward update, non-fast-forward rejection, concurrent rejection (two parallel pushes of different children from two clones → exactly one winner, the other "cannot lock ref"), a stale-lease create rejected, and delete all behave as required; the REST endpoint `git/refs/agent-claim` returns 200 while the ref is present and 404 after deletion; the ref is invisible in the normal GitHub UI | Verified 05.09.2026 (re-run by the head, results on #131) | Head's live re-run, 05.09.2026, `overnightworks/agent-claim`, namespace `refs/agent-claim/probe-1788623638` (deleted afterwards) |
| Push to a custom ref: plain push is fast-forward-only; a matching lease still permits a non-fast-forward replacement | Verified (documentation) | `git push` documentation; the reason §2 rejects the lease |
| Two racers on one expected OID: at most one lands; the loser re-reads and retries | Verified in a throwaway bare repo; "exactly one" corrected to "at most one" (permissions, hooks or transport can fail every racer) | Concept local experiment; round-1 counter-check |
| Custom-ref create-if-absent 1.9 s, fast-forward update 1.9 s, `ls-remote` 1.4 s, single-ref fetch 1.4 s, delete 1.9 s; today's gate ~2.4 s per write | Verified 05.09.2026 (re-run by the head, results on #131); environment-specific and not to be projected onto other repositories | Head's live re-run, 05.09.2026 |
| `overnightworks` plan free, repository public; org issue types are `Task, Bug, Feature` with no `Container` | Reported live, unverified; token scopes never evidenced | Concept probes, 05.09.2026 |
| GitHub issue dependencies are available on Free; sub-issues allow 100 direct children and 8 nesting levels | Verified (documentation) | GitHub docs |
| GitHub rulesets and branch protection target branches and tags only, never `refs/agent-claim/*` | Verified (documentation) | GitHub docs. Accepted: the threat model is cooperating clients |
| GitHub supports organization-owned custom issue types | Verified (documentation) | GitHub issue-type API |
| `refs/agent-claim/*` is pushable on GitLab | **Unverified** — the Branches API proves only `refs/heads` creation, and Gitaly's note about other namespaces certifies neither GitLab.com nor every installation | Round-2 counter-check |
| GitLab Free: one assignee (replacing, not union); `blocks` is paid; child items exist; configurable work-item types are paid | Verified (documentation) | GitLab docs |
| The landed #108 release union `MergedRelease \| AbandonedRelease` | Verified from `eb384b1` | `protocol.py` |
| v0.9.0 reads only `priority_labels` and `idea_label` from `board.toml` and silently ignores every other key | Verified from `eb384b1` | `board.py` — the reason criterion 8 needs a tombstone, not a pin |

## 7. Operator rulings, 05.09.2026

Recorded verbatim.

- The ledger issue goes away; the 03.09.2026 ruling on #64 is overturned.
- GitLab must be possible later without a rewrite.
- Typed contracts.
- Per-repository configuration only for settings with a caller.
- The decision criterion for every open point is the cleanest long-term solution with one
  owner per fact.
- Rescope and resource allocation are preserved.
- Roles stay open.
- Read-only commands never write.
- No metrics without a caller.
- The threat model is cooperating clients.
- Upstream push rights are mandatory for claimants.

### Decided by that criterion

| # | Decision | Reasoning (counter-check) |
|---|---|---|
| D1 | A receipt comment, written after a successful state change and never read back as truth. No assignee projection | The assignee is a racing copy: an old release removes its claim, a successor claims and assigns, then the old release clears the assignee — and only `status`/`board` could repair it, which read-only commands may not do. It also shows a forge account, not the agent. Exactly-once is not available; the contract is "attempt after a successful transition; a receipt failure never changes the successful state result" |
| D2 | The write gate authorizes only against live state. No positive caching | A cached commit authorizes writes that live state denies after a release, takeover or scope reduction, and after a clock rollback or a future-dated stamp the window is not even bounded by its nominal length. Only negative decisions may be cached |
| D3 | Item kind is the native issue type, including an organization-level `Container` type on GitHub. Ruled 05.09.2026: the organization type `Container` (id 859945696) now exists next to Task, Bug, Feature; containers #103, #114, #122 carry it | It is the clean one-owner mechanism. GitLab Free cannot create a custom `Container` type, so its adapter maps item kind read-only there |
| D4 | Wide-scope thresholds are protocol constants, not configuration | No caller for repository-specific thresholds, and configuration would let two fleets in one repository disagree on what "too wide" means |
| D5 | The migration machinery is deleted in the first release after every supported repository is proven migrated and old clients are fenced | Release distance does not decide safety; the proof does. A fixed "next release" is right only if that proof already exists |
