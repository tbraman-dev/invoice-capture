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
Reads `invoices_in/`, writes renamed copies plus `results.json`, `review.csv`, and `quickbooks_bills.csv` into `invoices_out/`. Reruns skip files already done. Delete `invoices_out/` to start over.

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

**quickbooks_bills.csv** — ready to import to QuickBooks Online. One row per line item (bill details repeated). Includes only invoices that are not marked paid or flagged as duplicates.

**Renamed PDFs** — moved to `invoices_out/` with safe names for filing.

**results.json** — full extraction for all documents: vendor info, line items, confidence score, flags, notes.

## Flags (12 total)

- `NOT_INVOICE` — statement or other document type (skip QuickBooks)
- `CREDIT_MEMO` — credit note (skip QuickBooks)
- `DUPLICATE` — same vendor + invoice number as an earlier file or ledger
- `MARKED_PAID` — has a paid stamp visible (skip QuickBooks)
- `MATH_ERROR` — line items do not add up (tolerance ±0.02)
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

**vendors.csv** — vendor reference list. The bookkeeper maintains this per client. Columns: `vendor_name`, `default_category`, `default_terms`, `notes`. Used to match vendor names, fill missing terms, and route to QuickBooks categories.

**ledger.csv** — already-captured bills. Checked to flag duplicates. Columns: `vendor`, `invoice_no`, `date`, `total`, `source_file`.

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
