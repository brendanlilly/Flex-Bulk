# Changelog

## 1.1 (2026-10-27)

Matching on real vendor and ERP lists (worked out on a 500-line customer file against the 719,441-item Webb list).

- **Lists with no header row** (a system export: manufacturer | part # | description | category | unit) are read by
  their columns: each column's role from what's in it. Before, such a sheet was read as free text: lines were set
  aside and "3M" read as a quantity of 3. A category column is kept but not searched; "See description" and a size in
  the part # column aren't part #s.
- **Part # hits are checked against the line's manufacturer.** A part # that finds the same manufacturer's item is
  confirmed as before; one that only finds another manufacturer's item (Hydrolevel 45-026 is also a weathervane) is
  offered with a note, not confirmed. A close part # of the line's manufacturer (one with a suffix the line left off)
  is offered as an option.
- **Brand names**: the manufacturer a list names ("Mueller Industries", "Resideo (Honeywell Home)", "A. O. Smith /
  State") is resolved to the item list's manufacturers by spelling, by part and by prefix, plus a table edited on the
  new dashboard page **Brand names & shorthand**. Match reports suggest names from part #s that found another
  manufacturer's item (accept with "Same company", or "Not the same").
- **Brand first**: when a line names a manufacturer, its items rank ahead of other brands' with the same fit, and a
  search on that manufacturer is added when none of its items came back.
- **Shorthand**: ERP abbreviations ("MXG VLV ADJ 3W THM 3/4 BRS") are searched as words, from a starting list and
  from what match reports learn (lines whose part # found their item pair the abbreviation with the item's word).
  Learned on 3+ lines: used; fewer: waits for review on the same page. Also 11/4 → 1-1/4, 1/2inch → 1/2".
- **Match reports**:
  - test cases (orders saved with **Test case** ticked keep the customer's documents and are read again),
  - customers' documents given to the report,
  - **Hide part #s**: matches every line by its description and manufacturer alone and scores it against the item its
    part # found; a measure of how lines with no part # will match,
  - the matcher's steps per line, every candidate's points (Trace), the reading check, and a **Bundle** with the code,
    settings and inputs.
- **Why?** on every line in the staff app: how it was read, the brand check, crosses, exact lookup, searches, and
  every candidate's points.
- Product links to fwwebb.com (`product_url` in `config/bulk_sources.yaml`).

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
