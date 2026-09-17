#!/usr/bin/env python3
"""capture.py -- auto-extract billing fields from inbound invoice/receipt PDFs.

Reads billing document PDFs, asks an LLM to extract structured fields, post-processes
the answer in plain Python, matches the vendor against vendors.csv, checks for
duplicates/math errors/OCR conflicts, renames a copy of the PDF, and writes
results.json / review.csv / quickbooks_bills.csv + a summary table.
"""
import argparse
import csv
import difflib
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Model used for extraction. Swap this (and extract()) for a direct Anthropic API
# call later -- everything else in this file is model-agnostic.
# sonnet, not haiku: misread digits in amounts/dates are the whole point of this
# pipeline, and a wrong total or date is a wrong bill.
DEFAULT_MODEL = "sonnet"

FIELDS_PROMPT = """Read the invoice PDF at this path: {pdf_path}

It is a scanned or photographed billing document (invoice, receipt, statement, or
credit memo). Extract the billing information and respond with ONLY a single JSON
object (no markdown fences, no commentary) with exactly these keys:

is_invoice (bool -- true only when document_type == "invoice")
document_type ("invoice" | "receipt" | "statement" | "credit_memo" | "other")
vendor_name (string or null -- the company that ISSUED the document, never the bill-to/customer)
vendor_address (string or null)
vendor_phone (string or null)
vendor_email (string or null)
invoice_number (string or null -- exactly as printed, e.g. "INV-10442")
invoice_date (string or null -- MM/DD/YYYY)
due_date (string or null -- MM/DD/YYYY, only if printed)
po_number (string or null)
currency (string or null -- ISO code; "USD" when a $ sign is seen)
subtotal (number or null)
tax (number or null)
total (number or null -- the grand total printed on the document)
amount_due (number or null -- balance due if printed separately, else same as total)
payment_terms (string or null -- e.g. "Net 30", "Due on receipt")
line_items (list of {{"description": string, "quantity": number or null, "unit_price": number or null, "amount": number or null}})
bill_to (string or null)
payment_method_seen (bool -- a card/cash/"paid" payment line is visible, e.g. on a receipt)
marked_paid (bool -- a PAID stamp or handwritten "paid" note is visible)
missing_info (list of strings -- fields present on the page but unreadable)
summary (one sentence string)
confidence ("high" | "medium" | "low")

Rules: use null (or an empty list) when a value is not clearly present -- never
guess a digit. Read every digit of every amount and date carefully. One line_items
entry per printed line; never combine two lines into one. All amounts as plain
numbers with no currency symbols or thousands commas. All dates as MM/DD/YYYY.
vendor_name is whoever issued/sent the bill, not the customer being billed
(bill_to). missing_info lists ONLY fields that are present on the page but
unreadable or ambiguous -- not fields the document simply doesn't have. Return
ONLY the JSON object, nothing else.
"""


def extract(pdf_path: Path, model: str = DEFAULT_MODEL, base_url: str = None,
            api_key: str = None) -> dict:
    """Call the model to pull structured fields out of one invoice PDF.

    Two backends, same prompt, same JSON contract:
    - default: the local `claude` CLI (Claude subscription, no API key).
    - base_url given: any OpenAI-compatible chat endpoint that accepts images
      (OpenCode Go in the cloud today, Ollama/vLLM on a GPU box tomorrow).
    """
    if base_url:
        return extract_openai(pdf_path, model, base_url, api_key)
    if "/" in model:  # "provider/model" means the opencode CLI, e.g. opencode-go/kimi-k3
        return extract_opencode(pdf_path, model)
    prompt = FIELDS_PROMPT.format(pdf_path=str(pdf_path.resolve()))
    # Prompt goes in via stdin: long prompts with quotes/newlines break the Windows shell.
    proc = subprocess.run(
        ["claude.cmd", "-p", "--model", model,
         "--output-format", "json", "--allowedTools", "Read"],
        input=prompt, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    envelope = json.loads(proc.stdout)
    raw = envelope["result"]
    return parse_model_json(raw)


def pdf_pages_png_b64(pdf_path: Path, scale: float = 2.0, max_pages: int = 4) -> list:
    """Render each page to PNG (about 150 dpi) and base64 it for an image API."""
    import base64
    import io
    import pypdfium2 as pdfium
    out = []
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            buf = io.BytesIO()
            page.render(scale=scale).to_pil().convert("RGB").save(buf, "PNG")
            page.close()
            out.append(base64.b64encode(buf.getvalue()).decode())
    finally:
        doc.close()
    return out


def extract_openai(pdf_path: Path, model: str, base_url: str, api_key: str = None) -> dict:
    """OpenAI-compatible /chat/completions with the document pages attached as images."""
    import urllib.request
    prompt = FIELDS_PROMPT.format(pdf_path="(the document pages are attached as images)")
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        for b64 in pdf_pages_png_b64(pdf_path)
    ]
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "temperature": 0, "max_tokens": 4000}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(resp.read())
    raw = reply["choices"][0]["message"]["content"]
    if isinstance(raw, list):  # some servers return content parts
        raw = "".join(p.get("text", "") for p in raw)
    return parse_model_json(raw)


def opencode_text_from_events(stdout: str) -> str:
    """`opencode run --format json` prints one JSON event per line; join the text parts."""
    parts = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            parts.append(ev.get("part", {}).get("text", ""))
    return "".join(parts)


def extract_opencode(pdf_path: Path, model: str) -> dict:
    """Any model the opencode CLI can reach (its own login, no key handling here)."""
    import base64
    import tempfile
    prompt = FIELDS_PROMPT.format(pdf_path="(the document pages are attached as images)")
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, b64 in enumerate(pdf_pages_png_b64(pdf_path)):
            p = Path(tmp) / f"page{i + 1}.png"
            p.write_bytes(base64.b64decode(b64))
            files += ["-f", str(p)]
        # Prompt first: "-f" is an array flag and would swallow a trailing prompt as a file name.
        proc = subprocess.run(["opencode.exe", "run", prompt, "-m", model, "--format", "json", *files],
                              capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"opencode exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return parse_model_json(opencode_text_from_events(proc.stdout))


def ocr_text(pdf_path: Path) -> str:
    """Windows built-in OCR (free, offline) via ocr_windows.ps1. Empty string if unavailable."""
    script = Path(__file__).with_name("ocr_windows.ps1")
    if not script.exists():
        return ""
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(script), str(pdf_path.resolve())],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(l for l in proc.stdout.splitlines() if not l.startswith("====="))


# Numbers with two decimals right after a totals label. Negative lookbehind on "sub"
# keeps SUBTOTAL from matching the TOTAL alternative.
_TOTAL_LABEL = r"(?:(?<!sub)TOTAL(?:\s+DUE)?|GRAND\s+TOTAL|AMOUNT\s+DUE|BALANCE\s+DUE|INVOICE\s+TOTAL)"
_MONEY_AFTER_LABEL = r"\$?\s*([\d,]+\.\d{2})"


def find_totals(text: str) -> list:
    """Every number following a totals label in OCR text, as floats, de-duplicated, in order."""
    out = []
    for m in re.finditer(_TOTAL_LABEL + r"\W{0,20}" + _MONEY_AFTER_LABEL, text or "", re.I):
        val = float(m.group(1).replace(",", ""))
        if val not in out:
            out.append(val)
    return out


def find_amounts(text: str) -> list:
    """Every money value (two decimals) anywhere in OCR text, de-duplicated, in order.
    Fallback for table layouts where OCR emits the label column and the amount column
    separately, so no label sits next to its number."""
    out = []
    for m in re.finditer(r"\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})", text or ""):
        val = float(m.group(1).replace(",", ""))
        if val not in out:
            out.append(val)
    return out


def total_cross_check(record: dict) -> str:
    """Two readers must agree. Labelled OCR totals are the strong check; when OCR found
    none, the model's total must at least appear somewhere among the OCR amounts."""
    ocr_totals = record.get("ocr_totals") or []
    # abs(): a credit memo prints 180.00 and the model reports -180.00
    model_vals = [abs(v) for v in (record.get("total"), record.get("amount_due")) if v is not None]
    if ocr_totals:
        if not model_vals:
            return (f"OCR read total {', '.join(f'{v:.2f}' for v in ocr_totals)}, "
                    f"model read none: verify")
        if not any(abs(v - m) <= 0.01 for v in ocr_totals for m in model_vals):
            return (f"Total conflict: model {', '.join(f'{v:.2f}' for v in model_vals)}, "
                    f"OCR {', '.join(f'{v:.2f}' for v in ocr_totals)}: verify")
        return ""
    amounts = record.get("ocr_amounts") or []
    if amounts and model_vals and not any(abs(v - m) <= 0.01 for v in amounts for m in model_vals):
        return (f"Total conflict: model {', '.join(f'{v:.2f}' for v in model_vals)} "
                f"not among OCR amounts ({len(amounts)} found): verify")
    return ""


def load_api_key(env_name: str, key_file: str = None, key_path: str = None) -> str:
    """Key from an env var, else from a JSON file by dotted path (never printed)."""
    import os
    if os.environ.get(env_name):
        return os.environ[env_name]
    if key_file and key_path:
        node = json.loads(Path(key_file).read_text())
        for part in key_path.split("."):
            node = node[part]
        return node
    return None


def strip_json_fences(raw: str) -> str:
    """Defensively strip ```json ... ``` / ``` ... ``` fences around a model reply."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def parse_model_json(raw: str) -> dict:
    """Parse the model's answer text into a dict. Raises on bad JSON (caller handles)."""
    return json.loads(strip_json_fences(raw))


def safe_slug(text, max_len: int = 40) -> str:
    """Letters/digits/underscore only, collapsed, truncated. Empty/None -> 'unknown'."""
    if not text:
        return "unknown"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_")
    return (slug[:max_len].rstrip("_")) or "unknown"


def make_error_record(source_file: str, error: str) -> dict:
    """Row used when the model reply couldn't be parsed -- batch keeps going."""
    return {
        "source_file": source_file,
        "error": error,
        "is_invoice": False,
        "document_type": "other",
        "vendor_name": None,
        "vendor_address": None,
        "vendor_phone": None,
        "vendor_email": None,
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "po_number": None,
        "currency": None,
        "subtotal": None,
        "tax": None,
        "total": None,
        "amount_due": None,
        "payment_terms": None,
        "line_items": [],
        "bill_to": None,
        "payment_method_seen": False,
        "marked_paid": False,
        "missing_info": ["could not parse model response"],
        "summary": f"Extraction failed: {error}",
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# Money / date coercion -- the model may hand back "$1,234.50", "1234.5", or
# a plain number; dates may come as MM/DD/YYYY or YYYY-MM-DD.
# ---------------------------------------------------------------------------

def parse_money(value):
    """Coerce a model/CSV amount to float, or None. Accepts "$1,234.50", "(12.00)", numbers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(value):
    """MM/DD/YYYY or YYYY-MM-DD (string), or a date -- returns a date or None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def format_date(d) -> str:
    return d.strftime("%m/%d/%Y") if d else ""


# ---------------------------------------------------------------------------
# Vendor list matching
# ---------------------------------------------------------------------------

_STOPWORDS = {"the", "a", "an"}
_VENDOR_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "company"}


def normalize_vendor_name(name) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    words = [w for w in s.split() if w not in _STOPWORDS]
    while words and words[-1] in _VENDOR_SUFFIXES:
        words.pop()
    return " ".join(words)


def load_vendors(path="vendors.csv") -> list:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def load_ledger(path="ledger.csv") -> list:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def match_vendor(name, rows):
    """Fuzzy-match a vendor name against vendors.csv. None if no good match."""
    target = normalize_vendor_name(name)
    if not target or not rows:
        return None
    by_norm = {}
    for row in rows:
        norm = normalize_vendor_name(row.get("vendor_name"))
        if norm:
            by_norm[norm] = row
    # Strict on purpose: a wrong match miscategorizes a bill. High similarity AND
    # the same first word ("northwind supply" must never match "northgate supply").
    first = target.split()[0]
    for norm, row in by_norm.items():
        if norm.split()[0] == first and (norm in target or target in norm):
            return row
    hit = difflib.get_close_matches(target, by_norm.keys(), n=1, cutoff=0.8)
    if hit and hit[0].split()[0] == first:
        return by_norm[hit[0]]
    return None


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------

_NET_TERMS = re.compile(r"net\s*(\d+)", re.I)


def compute_due(due_date_raw, terms, invoice_date) -> str:
    """Model due_date; else invoice_date + N days for "Net N" terms; else ""."""
    d = parse_date(due_date_raw)
    if d:
        return format_date(d)
    if due_date_raw:
        return str(due_date_raw)  # unparseable but present -- keep it rather than guess
    m = _NET_TERMS.search(terms or "")
    if m and invoice_date:
        return format_date(invoice_date + timedelta(days=int(m.group(1))))
    return ""


def math_check(data: dict) -> str:
    """sum(line items) vs subtotal, and subtotal+tax vs total (tolerance 0.02 each).
    When subtotal is null but line items + total exist, checks sum(lines)+tax vs total."""
    items = data.get("line_items") or []
    subtotal = data.get("subtotal")
    tax = data.get("tax") or 0
    total = data.get("total")
    amounts = [i.get("amount") for i in items if i.get("amount") is not None]
    lines_sum = sum(amounts) if amounts and len(amounts) == len(items) else None

    notes = []
    if lines_sum is not None and subtotal is not None and abs(lines_sum - subtotal) > 0.02:
        notes.append(f"lines sum {lines_sum:.2f}, subtotal {subtotal:.2f}")
    if subtotal is not None and total is not None:
        if abs(subtotal + tax - total) > 0.02:
            notes.append(f"subtotal + tax {subtotal + tax:.2f}, total {total:.2f}")
    elif subtotal is None and lines_sum is not None and total is not None:
        if abs(lines_sum + tax - total) > 0.02:
            notes.append(f"lines + tax {lines_sum + tax:.2f}, total {total:.2f}")
    return "; ".join(notes)


def _norm_invoice_no(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").casefold())


def duplicate_check(vendor, invoice_number, total, invoice_date, candidates) -> str:
    """candidates: (vendor, invoice_number, total, invoice_date, label) tuples -- ledger
    rows and/or earlier records in the batch. invoice_date items are date objects or None.
    Rule a: same vendor + same invoice number. Rule b: same vendor + same total (0.01) +
    invoice dates within 3 days. Returns a note, or "" if no duplicate found."""
    v_norm = normalize_vendor_name(vendor)
    inv_norm = _norm_invoice_no(invoice_number)
    if not v_norm:
        return ""
    for c_vendor, c_invno, c_total, c_date, label in candidates:
        if normalize_vendor_name(c_vendor) != v_norm:
            continue
        if inv_norm and _norm_invoice_no(c_invno) == inv_norm:
            return f"duplicate of {label} (same vendor + invoice number)"
        if (total is not None and c_total is not None and abs(total - c_total) <= 0.01
                and invoice_date and c_date and abs((invoice_date - c_date).days) <= 3):
            return f"duplicate of {label} (same vendor + total, invoice date within 3 days)"
    return ""


# Canonical order; only the ones that apply are kept.
_FLAG_ORDER = ("NOT_INVOICE", "CREDIT_MEMO", "DUPLICATE", "MARKED_PAID", "MATH_ERROR",
               "TOTAL_CONFLICT", "OVERDUE", "DUE_SOON", "UNKNOWN_VENDOR", "NO_TOTAL",
               "MISSING_INFO", "LOW_CONFIDENCE")


def compute_flags(data, due_obj, today, vendor_on_list, dup_note, math_note, total_conflict_note):
    """-> (flags list in canonical order, notes list). Pure, no model."""
    doc_type = data.get("document_type")
    marked_paid = bool(data.get("marked_paid"))
    present = {}
    notes = []

    if doc_type in ("statement", "other"):
        present["NOT_INVOICE"] = None
    if doc_type == "credit_memo":
        present["CREDIT_MEMO"] = None
    if dup_note:
        present["DUPLICATE"] = dup_note
    if marked_paid and doc_type == "invoice":
        present["MARKED_PAID"] = None
    if math_note:
        present["MATH_ERROR"] = math_note
    if total_conflict_note:
        present["TOTAL_CONFLICT"] = total_conflict_note
    if due_obj and doc_type in ("invoice", "receipt") and not marked_paid:
        if due_obj < today:
            present["OVERDUE"] = None
        elif due_obj <= today + timedelta(days=7):
            present["DUE_SOON"] = None
    if data.get("vendor_name") and not vendor_on_list:
        present["UNKNOWN_VENDOR"] = None
    if data.get("total") is None and doc_type not in ("statement", "other"):
        present["NO_TOTAL"] = None
    if data.get("missing_info"):
        present["MISSING_INFO"] = "Missing: " + "; ".join(data["missing_info"])
    if (data.get("confidence") or "").lower() == "low":
        present["LOW_CONFIDENCE"] = None

    flags = [f for f in _FLAG_ORDER if f in present]
    for f in flags:
        if present[f]:
            notes.append(present[f])
    return flags, notes


def build_new_filename(vendor, invoice_number, invoice_date, existing) -> str:
    """VENDOR_INVOICENO_YYYYMMDD.pdf, deduped against `existing` (mutated in place)."""
    d = invoice_date if isinstance(invoice_date, date) else parse_date(invoice_date)
    date_part = d.strftime("%Y%m%d") if d else "nodate"
    inv_part = safe_slug(invoice_number) if invoice_number else "noinv"
    base = f"{safe_slug(vendor, 30)}_{inv_part}_{date_part}"
    name = f"{base}.pdf"
    n = 2
    while name in existing:
        name = f"{base}_{n}.pdf"
        n += 1
    existing.add(name)
    return name


def apply_rules(data: dict, vendors: list, ledger: list, today, earlier_records: list = None) -> dict:
    """Vendor match, money/date coercion, duplicate/math/OCR checks, flags. Pure, no model."""
    earlier_records = earlier_records or []

    vendor_row = match_vendor(data.get("vendor_name"), vendors)
    vendor_canon = (vendor_row.get("vendor_name") if vendor_row else None) or data.get("vendor_name") or ""
    if not vendor_row and vendor_canon.isupper():
        vendor_canon = vendor_canon.title()
    data["vendor_on_list"] = bool(vendor_row)
    data["vendor"] = vendor_canon
    data["category"] = (vendor_row.get("default_category") if vendor_row else "") or ""
    terms = data.get("payment_terms") or (vendor_row.get("default_terms") if vendor_row else "") or ""
    data["terms"] = terms

    data["total"] = parse_money(data.get("total"))
    data["amount_due"] = parse_money(data.get("amount_due"))
    data["subtotal"] = parse_money(data.get("subtotal"))
    data["tax"] = parse_money(data.get("tax"))
    for item in data.get("line_items") or []:
        item["quantity"] = parse_money(item.get("quantity"))
        item["unit_price"] = parse_money(item.get("unit_price"))
        item["amount"] = parse_money(item.get("amount"))

    invoice_date_obj = parse_date(data.get("invoice_date"))
    data["invoice_date"] = format_date(invoice_date_obj) if invoice_date_obj else (data.get("invoice_date") or None)

    doc_type = data.get("document_type")
    billable = doc_type in ("invoice", "receipt")  # statements/credit memos: no due, no math
    due_str = compute_due(data.get("due_date"), terms, invoice_date_obj) if billable else ""
    data["due"] = due_str
    due_obj = parse_date(due_str) if due_str else None

    dup_note = ""
    if doc_type in ("invoice", "receipt"):
        candidates = [
            (row.get("vendor"), row.get("invoice_no"), parse_money(row.get("total")),
             parse_date(row.get("date")), row.get("source_file") or "ledger")
            for row in ledger
        ]
        candidates += [
            (rec.get("vendor") or rec.get("vendor_name"), rec.get("invoice_number"), rec.get("total"),
             parse_date(rec.get("invoice_date")), rec.get("source_file"))
            for rec in earlier_records
            if rec.get("document_type") in ("invoice", "receipt") and "DUPLICATE" not in (rec.get("flags") or [])
        ]
        dup_note = duplicate_check(vendor_canon, data.get("invoice_number"), data.get("total"),
                                    invoice_date_obj, candidates)

    math_note = math_check(data) if billable else ""
    total_conflict_note = total_cross_check(data)

    flags, notes = compute_flags(data, due_obj, today, data["vendor_on_list"], dup_note,
                                  math_note, total_conflict_note)
    data["flags"] = flags
    data["notes"] = notes
    data["needs_review"] = bool(flags)
    return data


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

REVIEW_FIELDS = ["file", "type", "vendor", "invoice_no", "date", "due", "total", "category",
                  "flags", "notes", "needs_review", "new_file"]

QBO_FIELDS = ["BillNo", "Supplier", "BillDate", "DueDate", "Terms", "Location", "Memo", "Account",
              "LineDescription", "LineAmount", "LineTaxCode", "LineTaxAmount", "Currency"]


def to_review_row(record: dict) -> dict:
    total = record.get("total")
    return {
        "file": record.get("source_file"),
        "type": record.get("document_type"),
        "vendor": record.get("vendor") or "",
        "invoice_no": record.get("invoice_number") or "",
        "date": record.get("invoice_date") or "",
        "due": record.get("due") or "",
        "total": f"{total:.2f}" if total is not None else "",
        "category": record.get("category") or "",
        "flags": ";".join(record.get("flags") or []),
        "notes": "; ".join(record.get("notes") or []),
        "needs_review": "True" if record.get("needs_review") else "False",
        "new_file": record.get("new_file") or "",
    }


def qbo_rows(record: dict) -> list:
    """One QBO bill-import row per line item (+ a sales-tax row). Invoices only,
    excludes DUPLICATE/MARKED_PAID (and receipts, since those aren't document_type invoice)."""
    if record.get("document_type") != "invoice":
        return []
    flags = record.get("flags") or []
    if "DUPLICATE" in flags or "MARKED_PAID" in flags:
        return []
    base = {
        "BillNo": record.get("invoice_number") or "",
        "Supplier": record.get("vendor") or "",
        "BillDate": record.get("invoice_date") or "",
        "DueDate": record.get("due") or "",
        "Terms": record.get("terms") or "",
        "Location": "",
        "Memo": record.get("source_file") or "",
        "Account": record.get("category") or "",
        "LineTaxCode": "",
        "LineTaxAmount": "",
        "Currency": record.get("currency") or "USD",
    }
    rows = []
    items = record.get("line_items") or []
    if items:
        for item in items:
            amount = item.get("amount")
            rows.append({**base, "LineDescription": item.get("description") or "",
                         "LineAmount": f"{amount:.2f}" if amount is not None else ""})
    else:
        amount = record.get("subtotal") if record.get("subtotal") is not None else record.get("total")
        rows.append({**base, "LineDescription": record.get("summary") or "",
                     "LineAmount": f"{amount:.2f}" if amount is not None else ""})
    tax = record.get("tax")
    if tax:
        rows.append({**base, "LineDescription": "Sales tax", "LineAmount": f"{tax:.2f}"})
    return rows


def print_summary_table(records: list) -> None:
    cols = ("file", "type", "vendor", "invoice_no", "total", "due", "flags", "needs_review")
    rows = [cols]
    for r in records:
        row = to_review_row(r)
        rows.append(tuple(str(row.get(c)) if row.get(c) not in (None, "") else "-" for c in cols))
    widths = [max(len(row[i]) for row in rows) for i in range(len(cols))]
    for row in rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def process_dir(input_dir: Path, output_dir: Path, model: str, base_url: str = None,
                api_key: str = None, today=None, vendors_path="vendors.csv",
                ledger_path="ledger.csv") -> list:
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.json"
    existing = []
    done_sources = set()
    if results_path.exists():
        existing = json.loads(results_path.read_text())
        done_sources = {r["source_file"] for r in existing}

    vendors = load_vendors(vendors_path)
    ledger = load_ledger(ledger_path)
    today = today or date.today()

    pdfs = sorted(set(input_dir.glob("*.pdf")) | set(input_dir.glob("*.PDF")))
    records = list(existing)
    for pdf in pdfs:
        if pdf.name in done_sources:
            continue
        print(f"processing {pdf.name} ...")
        try:
            data = extract(pdf, model, base_url, api_key)
        except Exception as e:  # noqa: BLE001 -- never crash the batch
            data = make_error_record(pdf.name, str(e))
        data["source_file"] = pdf.name
        records.append(data)

    # Rules run on every record, old and new, in sorted filename order: edit a rule or
    # vendors.csv/ledger.csv and rerun, no model calls needed for docs already extracted.
    records.sort(key=lambda r: r.get("source_file") or "")

    used_names = set()
    clean_so_far = []
    for data in records:
        pdf = input_dir / data["source_file"]
        if "ocr_amounts" not in data and pdf.exists():  # free second reader, done once per file
            text = ocr_text(pdf)
            data["ocr_totals"] = find_totals(text)
            data["ocr_amounts"] = find_amounts(text)
        apply_rules(data, vendors, ledger, today, clean_so_far)
        data["new_file"] = build_new_filename(data.get("vendor"), data.get("invoice_number"),
                                              data.get("invoice_date"), used_names)
        if pdf.exists():
            shutil.copy2(pdf, output_dir / data["new_file"])
        clean_so_far.append(data)
        print(f"  {data['source_file']} -> vendor={data.get('vendor') or '-'}  "
              f"needs_review={data['needs_review']}")

    results_path.write_text(json.dumps(records, indent=2, default=str))
    with (output_dir / "review.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for r in records:
            writer.writerow(to_review_row(r))

    with (output_dir / "quickbooks_bills.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=QBO_FIELDS)
        writer.writeheader()
        for r in records:
            for row in qbo_rows(r):
                writer.writerow(row)

    return records


def main():
    ap = argparse.ArgumentParser(description="Capture and triage inbound invoices.")
    ap.add_argument("input_dir", nargs="?", default="invoices_in")
    ap.add_argument("output_dir", nargs="?", default="invoices_out")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=None,
                    help="OpenAI-compatible endpoint, e.g. https://opencode.ai/zen/go/v1 "
                         "or http://localhost:11434/v1 (Ollama). Omit to use the claude CLI.")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY",
                    help="Name of the env var holding the API key for --base-url")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, default: today")
    ap.add_argument("--vendors", default="vendors.csv")
    ap.add_argument("--ledger", default="ledger.csv")
    args = ap.parse_args()

    api_key = load_api_key(args.api_key_env) if args.base_url else None
    if args.base_url and not api_key and "localhost" not in args.base_url:
        sys.exit(f"set {args.api_key_env} to the API key for {args.base_url}")
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    records = process_dir(Path(args.input_dir), Path(args.output_dir), args.model,
                          args.base_url, api_key, today, args.vendors, args.ledger)
    print()
    print_summary_table(records)


if __name__ == "__main__":
    main()
