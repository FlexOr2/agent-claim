"""Vulture whitelist.

`NoItemKind.DOCS` and `NoItemKind.FIX` are never referenced by a literal
attribute access; the code reaches them only by dynamic construction
(`NoItemKind(value)`) and iteration (`for kind in NoItemKind`), both invisible
to vulture's static analysis. Naming them here is the whole fix -- this file
is never imported by the package itself.

`parse_slice_table` and `SliceTableRow.item_issue` are kept as the migration
input for the typed body block (decision record 0001 step 4). Claim no longer
calls the parser; the body migration deletes it.

`ForgeUnsupportedError` is the port's typed capability-refusal failure
(decision record 0001 §2, §4 criterion D3): it has no caller until the first
adapter that can refuse an operation (the GitLab adapter, per #112) lands, so
vulture sees it as unreferenced. It is part of the port's declared failure
surface today (issue #131), not speculative.
"""

from agent_claim.board import NoItemKind, SliceTableRow, parse_slice_table
from agent_claim.forge import ForgeUnsupportedError

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    parse_slice_table,
    SliceTableRow.item_issue,
    ForgeUnsupportedError,
)
