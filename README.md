# Flex Bulk

Bulk order matching for Webb staff. A customer sends a list (a takeoff or BOM in Excel or CSV, an email with
attachments, a Word list, a PDF, a photo of a handwritten note, or lines typed at the counter); Flex Bulk matches
every line to an item on **your item list**, shows branch stock against the quantity asked, prices it at the
customer's price level, and lets staff confirm, swap or find each item. The order is saved under the customer and
job, and exported to Excel, CSV, or an item/quantity file for order entry.

It is the Bulk order tool from FlexSearch v2.46, taken out on its own with its own copy of the code. Everything that
makes it match well comes with it: the line reader (email, Word, OCR, column mapping), line fit (size, material,
joint), trade shorthand, the search engine underneath, and the settings dashboard: crosses, brand preferences,
learned crosses, ranking weights, the search dictionary, rewrites and rules. What's gone is everything about
customer accounts: there are no customer logins, order history or invoices. Staff enter the customer and job on each
order, and the customer's own earlier picks count for more the next time.

## Start

On a Mac, double-click **Start Flex Bulk.command** (or run `bash start.sh`); on Windows, run `start-windows.bat`.
The first run installs Python and the packages into this folder, loads the item files, builds the index (about
2 minutes for 40,000 items, 20 to 30 for 750,000) and opens http://127.0.0.1:8080. The settings dashboard is at
`/admin`.

With no files of your own yet, it loads a demo list: 36,092 items with stock at Webb's branches and two price levels
(`samples/demo-data/`).

## Your item, stock and price files

Put them in `data/incoming/`:

| File | What's in it | Required |
|---|---|---|
| `items.*` | One row per item: item #, description; manufacturer, mfr part #, UPC, categories, unit, status if you have them | yes |
| `stock.*` | On hand per item and branch: one row per item and branch, or one column per branch (`QOH_100`, `QOH_131`…) | no (lines then say "ask the branch") |
| `prices.*` | List price per item, plus any price levels (Contractor, Net…) staff can choose per order | no if the item file has a list price |
| `branches.*` | Branch #, name, city, state, lat/lon | no (Webb's branch list is used) |

CSV (also `.csv.gz`), Excel (`.xlsx`) and Parquet all work. Column names are matched loosely ("Mfr Part #",
"MFR_PART_NO", "Manufacturer Part Number" all work); `config/bulk_sources.yaml` lists the names it looks for and is
the only file to edit when your export calls a column something else.

Then start again: new files are loaded and indexed automatically. To load by hand:

```
python -m flexbulk.loader --check     # read and check the files, change nothing
python -m flexbulk.build              # load and index
```

The loader says what it found before anything is indexed: duplicate item numbers, items with no description or no
price, stock or price rows for item numbers not on the list, branches not on the branch list. The same notes show at
the bottom of the app.

Without sales history, how often an item sells is read from its stock (how many branches carry it, and how many
they hold). Without attributes in the file, size, material and joint are read from the description.

## Using it

- **New order**: customer #, name, job, PO, contact, notes. Facts a document states (an email's job name or PO) fill
  the empty boxes.
- **Lines**: upload or paste, multi-search, or quick entry. Exact item #, mfr part # and UPC matches arrive
  confirmed; everything else is a recommendation to confirm, with the reasons shown. **Find another** searches the
  item list for any line. Short at the pickup branch: the transfer plan and same-spec items on the shelf.
- **Pickup branch** and **price level** (header) apply to the whole order; changing them re-matches the lines.
- **Save** (Ctrl/Cmd+S) keeps the order with every line as left; **Orders** lists them. **Export**: an Excel workbook
  (header, every line, an order-entry sheet), a CSV, or the order-entry file (confirmed lines: item # and quantity).

## The dashboard (`/admin`)

| Page | What it changes |
|---|---|
| Bulk order analytics | What's uploaded, how lines match, brands asked for that aren't carried, items asked for that aren't on the list |
| Crosses | Brand crosses (Kohler → American Standard) and item crosses (a part # or line → one item), company-wide or per branch |
| Brand preferences | Webb's brand order per category, for lines that name no brand |
| Learned crosses | What staff pick for a line; make a pick permanent, or switch it off |
| Ranking weights | Bulk order weights (Webb's brand order, learned picks, on the shelf) and search weights |
| Relevance by field, Search dictionary, Rewrites, Rules | How the search underneath reads trade words |
| Change log, Data & access, Claude usage, Sign-ins | Admin |

Settings are drafted, tried against sample lines, then published; every published version can be reverted. Crosses
set up for FlexSearch's catalog point at its item numbers; on a new item list, check them on the Crosses page (a
cross to an item that isn't on the list is ignored).

## Sharing it

On a server, set `WEBB_SITE_PASSWORD` (a login for everyone), `WEBB_ADMIN_PASSWORD` (the dashboard) and
`WEBB_SESSION_SECRET`. `Dockerfile` and `fly.toml` run it on Fly.io with the data folder on a volume: copy the item
files to `/data/incoming/` and restart. Set `ANTHROPIC_API_KEY` to read handwriting on a server (a Mac reads it
with Apple's handwriting recognition).

| Setting | Default | |
|---|---|---|
| `WEBB_DATA_DIR` | `data/` | where everything built and saved lives |
| `FLEXBULK_INCOMING` | `data/incoming/` | where the item files are read from |
| `FLEXBULK_SOURCES` | `config/bulk_sources.yaml` | the column mapping |
| `FLEXBULK_ORDERS_DB` | `data/bulk/orders.sqlite` | saved orders |
| `FLEXBULK_XREF` | `0` | `1` turns on FlexSearch's Webb cross-reference lake and real Webb items, if your item numbers are Webb's |

## Code

```
flexbulk/        loader (files -> tables), build, orders (save, export), prices, launch
webbsearch/      FlexSearch's engine, copied: retrieval/bom.py (the matcher), resolve, intake, linefit, ocr,
                 availability (stock from the stock file), merch/ (settings, crosses, learned choices), api/ (the app
                 and the dashboard)
config/          bulk_sources.yaml (your columns), the search dictionary, Webb's branch list
samples/         demo item files and three sample orders (an email, a Word list, a handwritten note)
tests/           python -m pytest tests
```
