# Invoice Capture

Reads PDFs from a folder (invoices, receipts, statements). Pulls out the vendor, invoice number, total, due date, and line items. Uses AI to read what OCR cannot. Routes by rules, flags duplicates and math errors, fills a worklist. A human checks the results instead of reading every page cold.

**Demo only. Fake data only.** Never put real financial documents through this without security review.

## Run

```
python make_fake_invoices.py
```
Makes 20 fake documents in `invoices_in/`.

```
python capture.py
```
Reads `invoices_in/` and extracts vendor, invoice number, date, total, line items, and payment details. Writes renamed copies, `results.json`, and `review.csv` into `invoices_out/`. Reruns skip files already done. Delete `invoices_out/` to start over.

Open `invoices_out/review.csv`, check the `reviewed` column, and mark each row as "ok" (import it) or "skip" (do not import). Clean rows with no flags are pre-filled "ok".

```
python capture.py --export
```
Reads `invoices_out/review.csv` and `results.json`. Rows marked "ok" are written to `quickbooks_bills.csv` (invoices) and `quickbooks_expenses.csv` (receipts). Appends exported rows to `ledger.csv` (skips duplicates). Prints a summary.

```
python test_capture.py
```
Checks the extraction logic and flag rules without calling the model.

```
python check_results.py
```
Scores `invoices_out/review.csv` against `expected.json` (the known answer for every fake invoice). Prints each wrong field and a total.

## What comes out

**review.csv** — one row per document:
| Column | Example |
|---|---|
| file | scan_0001.pdf |
| type | invoice |
| read_by | text (text layer) or vision (picture) |
| vendor | Northwind Supply |
| invoice_no | INV-2024-0142 |
| date | 09/15/2026 |
| due | 10/15/2026 |
| total | 1234.56 |
| category | Office Supplies |
| flags | MATH_ERROR;DUE_SOON |
| notes | lines sum 412.00, subtotal 421.00 |
| needs_review | True |
| new_file | northwind_supply_inv20240142_20260915.pdf |
| reviewed | ok |

**quickbooks_bills.csv** — ready to import to QuickBooks Online. One row per line item (bill details repeated). Only invoices marked "ok" in review.csv.

**quickbooks_expenses.csv** — ready to import to QuickBooks Online. One row per line item. Only receipts marked "ok" in review.csv. Columns: Ref No, Payee, Account, Payment Date, Payment Method, Memo, Category Account, Category Description, Category Line Amount, Currency Code. Account is the bank or card account the bookkeeper fills in; Category Account is the expense category. SaasAnt maps columns by name, so the extra Payment Method column is harmless.

**Renamed PDFs** — moved to `invoices_out/` with safe names for filing.

**results.json** — full extraction for all documents: vendor info, line items, confidence score, flags, notes.

## Flags (12 total)

- `NOT_INVOICE` — statement or other document type (skip QuickBooks)
- `CREDIT_MEMO` — credit note (skip QuickBooks)
- `DUPLICATE` — same vendor + invoice number as an earlier file or ledger
- `MARKED_PAID` — has a paid stamp visible (skip QuickBooks)
- `MATH_ERROR` — line items, shipping, and discount do not add up to total (tolerance ±0.02)
- `TOTAL_CONFLICT` — printed totals do not match model extraction (tolerance ±0.01)
- `OVERDUE` — due date is in the past
- `DUE_SOON` — due within 7 days
- `UNKNOWN_VENDOR` — vendor name not in vendors.csv
- `NO_TOTAL` — total is blank (except statements/others)
- `MISSING_INFO` — fields visible but unreadable
- `LOW_CONFIDENCE` — model confidence is low

## Options

```
python capture.py invoices_in invoices_out --model sonnet
```

`--model sonnet` uses Claude Sonnet (default: Haiku). Use Sonnet for harder images or handwriting.

```
python capture.py invoices_in invoices_out --model opencode-go/kimi-k3
```

OpenCode (your OpenCode login, no API key needed): `opencode-go/kimi-k3`, `opencode-go/qwen3-vl`, etc.

```
python capture.py invoices_in invoices_out --base-url http://localhost:11434/v1 --model qwen3-vl:32b
```

Ollama on your machine (fully local): pull a vision model, then pass `--base-url` and `--model`.

```
python capture.py invoices_in invoices_out --today 2026-09-20
```

Override today's date (for testing due-date flags). Format: YYYY-MM-DD.

## Data files

**vendors.csv** — vendor reference list. The bookkeeper maintains this per client. Columns: `vendor_name`, `qbo_vendor_name`, `default_category`, `default_terms`, `notes`. 

- `vendor_name`: the name as it appears on invoices (canonical).
- `qbo_vendor_name`: the vendor display name in the client's QuickBooks. Leave blank if it matches `vendor_name`.
- `default_category`: must be the exact account name from the client's QuickBooks chart of accounts.
- `default_terms` and `notes`: as before.

If you import a vendor or category name that does not match exactly, QuickBooks creates a new one.

**ledger.csv** — already-captured bills. Checked to flag duplicates. Columns: `vendor`, `invoice_no`, `date`, `total`, `source_file`.

## Receipts

QuickBooks Online does not have a built-in CSV import for expenses (only for bank transactions). The `quickbooks_expenses.csv` file uses the SaasAnt Transactions format, a third-party tool commonly used by bookkeepers to import receipt expenses into QuickBooks.

## Other models

The default uses the `claude` CLI on your subscription (Haiku, ~10 seconds per document).

Same rules, same scoring, any model you have access to:

```
set OPENAI_API_KEY=your-key
python capture.py invoices_in invoices_out_openai --base-url https://api.openai.com/v1 --model gpt-4-vision
```

Kimi K3 via OpenCode runs in seconds and rarely misreads (per testing on similar documents).

## Next steps

- Watched folder: monitor `invoices_in/` and auto-run on new PDFs
- IMAP inbox: scan bill emails, download attachments, extract and route
- OneDrive: read from shared folder, write results back
- Hands-free: schedule daily runs via task scheduler

## Text first, vision last

Most emailed vendor invoices are digital PDFs with a real text layer. Scans and phone photos are pictures.

- `capture.py` first reads the text layer with MarkItDown (no OCR). If there is real text (more than 200 characters per page and invoice labels present), the text goes to a text-only model call. No images are sent.
- If there is little or no text, the file is a picture, and the vision path reads it as before.
- `review.csv` shows the path in the `read_by` column. `check_results.py` prints accuracy, cost, and time per path.
- `--no-text-first` forces vision for everything. `--text-model haiku` uses a cheaper model for the text path only.
- MarkItDown is optional: `pip install "markitdown[pdf]"`. Without it, every document takes the vision path.

Measured on the 22 fake documents (16 digital, 6 pictures), Claude command line, sonnet:

| Run | Fields correct | Cost per document | Time per document |
|---|---|---|---|
| Text path, sonnet (16 docs) | 157 of 157 | $0.084 | 5.8 s |
| Text path, haiku (16 docs) | 156 of 157, the miss was a blank with a flag | $0.032 | 13.0 s |
| Vision path, sonnet (same 16 docs) | same fields correct | $0.104 | 7.4 s |

Most of the cost per call through the Claude command line is fixed overhead, so the gap is smaller here than on a direct API.

Known limit: a digital PDF with a stamp or handwriting added as an image keeps its text layer, so the text path does not see the stamp.
