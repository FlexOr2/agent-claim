"""Vulture whitelist.

`NoItemKind.DOCS` and `NoItemKind.FIX` are never referenced by a literal
attribute access; the code reaches them only by dynamic construction
(`NoItemKind(value)`) and iteration (`for kind in NoItemKind`), both invisible
to vulture's static analysis. Naming them here is the whole fix -- this file
is never imported by the package itself.

`parse_slice_table` and `SliceTableRow.item_issue` are kept as the migration
input for the typed body block (decision record 0001 step 4). Claim no longer
calls the parser; the body migration deletes it.

`ForgeUnsupportedError` and `Capability.UNSUPPORTED` are the port's typed
capability-refusal surface (decision record 0001 §2, §4 criterion D3): the
GitHub adapter never refuses an operation, so both stay uncalled/unconstructed
until the first adapter that can refuse one (the GitLab adapter, per #112)
lands. `RepositoryId.host` and `ForgeReader.capability`/`GitHubForge.capability`
are likewise part of the port's declared shape with no production reader yet
-- `capability()` is exercised only by tests (the exhaustiveness test and
`ReaderOnlyForge`), and `host` only matters once a second forge host exists.
None of this is speculative: it is the port surface issue #131 declares today,
each with a named future caller.
"""

from agent_claim.board import NoItemKind, SliceTableRow, parse_slice_table
from agent_claim.forge import Capability, ForgeReader, ForgeUnsupportedError, RepositoryId
from agent_claim.github import GitHubForge

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    parse_slice_table,
    SliceTableRow.item_issue,
    ForgeUnsupportedError,
    Capability.UNSUPPORTED,
    RepositoryId("host", (), "name").host,
    ForgeReader.capability,
    GitHubForge.capability,
)
