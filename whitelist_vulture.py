"""Vulture whitelist.

`NoItemKind.DOCS` and `NoItemKind.FIX` are never referenced by a literal
attribute access; the code reaches them only by dynamic construction
(`NoItemKind(value)`) and iteration (`for kind in NoItemKind`), both invisible
to vulture's static analysis. Naming them here is the whole fix -- this file
is never imported by the package itself.

`parse_slice_table` and `SliceTableRow.item_issue` are kept as the migration
input for the typed body block (decision record 0001 step 4). Claim no longer
calls the parser; the body migration deletes it.
"""

from agent_claim.board import NoItemKind, SliceTableRow, parse_slice_table

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    parse_slice_table,
    SliceTableRow.item_issue,
)
