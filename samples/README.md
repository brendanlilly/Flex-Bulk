# Sample orders for Bulk order → Upload

Made-up documents in the shapes contractors send, for trying the intake:

- `order-email.eml`: a forwarded order email with a CSV takeoff attached. The item lines are found in the body; the greeting, signature, phone and address are set aside; the PO, job and delivery note become the order header.
- `material-list.docx`: a Word material list with Qty / Size / Description / Notes columns.
- `handwritten-note.jpg`: a photo of a handwritten list. On a Mac it is read with Apple's handwriting recognition; each line shows the picture of what was written next to the text, and every line needs your confirmation.

Drag any of them onto the upload box (several at once is fine).

## demo-data/

A stand-in item list until your own files arrive, shaped like an ERP export: `items.csv.gz` (36,092 items: FlexSearch's
most-ordered items, the items its sample orders match, and a random 6,000 more), `stock.csv.gz` (on hand per item and
branch at Webb's counters; made-up quantities) and `prices.csv.gz` (list price and two made-up levels, Contractor and
Net). `python -m flexbulk.launch` copies them to `data/incoming/` when that folder is empty. Not real Webb data.
