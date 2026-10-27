# Changelog

## 1.0 (2026-10-27)

Flex Bulk starts as its own project: FlexSearch v2.46's Bulk order tool, with its own copy of the code, for Webb staff
matching customers' lists against a new item list.

- **Item, stock and price files**: `data/incoming/` (CSV, Excel, Parquet), columns mapped in `config/bulk_sources.yaml`,
  checked and reported before indexing (`flexbulk.loader`, `flexbulk.build`). Stock per item and branch (long or wide
  layout) replaces FlexSearch's demo stock; price levels per order; size, material and joint read from descriptions
  when the file has no attributes; popularity read from stock until there's sales history.
- **Orders**: customer #, name, job, PO, contact and notes per order; saved with every line as staff left it; reopened
  from the Orders page; exported to Excel, CSV or an order-entry file (item # and quantity).
- **Find another**: search the item list for any line and use the item picked.
- **Learning per customer**: staff picks for a line count company-wide once enough agree, and at once for the same
  customer.
- **Dashboard**: the bulk order pages (analytics, crosses, brand preferences, learned crosses) and the matching
  settings (weights, relevance by field, dictionary, rewrites, rules); FlexSearch's storefront pages removed.
- Removed: customer accounts and sign-in switching, order history and invoices, the storefront search, Ask, job kits,
  the Knowledge Center, photo search, the Extended Catalog. FlexSearch's Webb cross-reference lake is off unless
  `FLEXBULK_XREF=1`.
- Fixed (also in FlexSearch): an item cross pointing at an item not on the list stopped the line from matching.
