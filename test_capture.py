"""Tests for the pure (non-model) logic in capture.py. Run: python test_capture.py"""
import csv
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from capture import (
    apply_rules,
    build_new_filename,
    compute_due,
    compute_flags,
    compute_reviewed,
    duplicate_check,
    export_dir,
    extract,
    find_totals,
    has_real_text,
    load_vendors,
    make_error_record,
    match_vendor,
    math_check,
    normalize_payment_method,
    parse_date,
    parse_money,
    parse_model_json,
    qbo_expense_rows,
    qbo_rows,
    read_reviewed_map,
    REVIEW_FIELDS,
    safe_slug,
    strip_json_fences,
    text_layer,
    to_review_row,
    total_cross_check,
)

VENDORS = [
    {"vendor_name": "Northwind Supply", "default_category": "Office Supplies",
     "default_terms": "Net 30", "notes": ""},
    {"vendor_name": "Riverbend Plumbing", "default_category": "Repairs and Maintenance",
     "default_terms": "Net 15", "notes": ""},
    {"vendor_name": "Quik Fuel", "default_category": "Fuel", "default_terms": "Due on receipt",
     "notes": ""},
]


def test_safe_slug():
    assert safe_slug("St. Mary's Supply!") == "St_Mary_s_Supply"
    assert safe_slug(None) == "unknown"
    assert safe_slug("") == "unknown"
    assert safe_slug("a" * 100) == "a" * 40


def test_fence_stripping_and_parse():
    canned = '```json\n{"is_invoice": true, "document_type": "invoice", "total": 12.0}\n```'
    data = parse_model_json(canned)
    assert data["is_invoice"] is True
    assert data["document_type"] == "invoice"

    plain = strip_json_fences('```\n{"a": 1}\n```')
    assert json.loads(plain) == {"a": 1}

    no_fence = strip_json_fences('{"a": 1}')
    assert json.loads(no_fence) == {"a": 1}


def test_parse_money():
    assert parse_money(12) == 12.0
    assert parse_money(12.5) == 12.5
    assert parse_money("1,234.50") == 1234.50
    assert parse_money("$12.00") == 12.00
    assert parse_money("$1,234.50") == 1234.50
    assert parse_money("(12.00)") == -12.00
    assert parse_money(None) is None
    assert parse_money("") is None
    assert parse_money("not a number") is None


def test_parse_date():
    assert parse_date("09/16/2026") == date(2026, 9, 16)
    assert parse_date("2026-09-16") == date(2026, 9, 16)
    assert parse_date(date(2026, 9, 16)) == date(2026, 9, 16)
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("garbage") is None


def test_match_vendor():
    # suffix stripping: "Co." falls off both sides
    hit = match_vendor("Northwind Supply Co.", VENDORS)
    assert hit["vendor_name"] == "Northwind Supply"

    # first-word strictness: must not cross-match a similar but different vendor
    assert match_vendor("Riverside Plumbing", VENDORS) is None

    # exact match
    assert match_vendor("Quik Fuel", VENDORS)["default_category"] == "Fuel"

    # no match at all
    assert match_vendor("Totally Unrelated Vendor LLC", VENDORS) is None
    assert match_vendor(None, VENDORS) is None
    assert match_vendor("Northwind Supply", []) is None


def test_compute_due():
    # model gave a due date directly
    assert compute_due("10/01/2026", "Net 30", date(2026, 9, 1)) == "10/01/2026"
    # no due date, but Net N terms + invoice date
    assert compute_due(None, "Net 15", date(2026, 9, 1)) == "09/16/2026"
    # Net N but no invoice date to add to
    assert compute_due(None, "Net 15", None) == ""
    # no due date, no usable terms
    assert compute_due(None, "Due on receipt", date(2026, 9, 1)) == ""
    assert compute_due(None, None, None) == ""


def test_math_check():
    # both checks pass (no shipping/discount)
    ok = {"line_items": [{"amount": 100.0}, {"amount": 50.0}], "subtotal": 150.0,
          "tax": 12.0, "total": 162.0}
    assert math_check(ok) == ""

    # lines vs subtotal mismatch
    bad_lines = {"line_items": [{"amount": 100.0}, {"amount": 41.0}], "subtotal": 150.0,
                 "tax": 0, "total": 150.0}
    note = math_check(bad_lines)
    assert "lines sum 141.00, subtotal 150.00" in note

    # subtotal - discount + shipping + tax vs total mismatch
    bad_total = {"line_items": [{"amount": 150.0}], "subtotal": 150.0, "tax": 12.0, "total": 200.0}
    note = math_check(bad_total)
    assert "subtotal - discount + shipping + tax 162.00, total 200.00" in note

    # subtotal null: falls back to sum(lines) - discount + shipping + tax vs total
    no_subtotal_ok = {"line_items": [{"amount": 100.0}], "subtotal": None, "tax": 8.0, "total": 108.0}
    assert math_check(no_subtotal_ok) == ""
    no_subtotal_bad = {"line_items": [{"amount": 100.0}], "subtotal": None, "tax": 8.0, "total": 200.0}
    assert "lines - discount + shipping + tax" in math_check(no_subtotal_bad)

    # missing numbers -- nothing to check, no error
    assert math_check({"line_items": [], "subtotal": None, "tax": None, "total": None}) == ""

    # shipping: subtotal + shipping + tax = total -- passes
    shipping_ok = {"line_items": [{"amount": 400.0}], "subtotal": 400.0, "tax": 12.0,
                    "shipping": 9.00, "total": 421.0}
    assert math_check(shipping_ok) == ""

    # discount: subtotal - discount + tax = total -- passes
    discount_ok = {"line_items": [{"amount": 400.0}], "subtotal": 400.0, "tax": 12.0,
                    "discount": 50.0, "total": 362.0}
    assert math_check(discount_ok) == ""

    # both, and it's wrong -- fails with the combined note
    both_bad = {"line_items": [{"amount": 400.0}], "subtotal": 400.0, "tax": 12.0,
                "shipping": 9.0, "discount": 0.0, "total": 421.0 + 9.00}  # +9 off from ok
    note = math_check(both_bad)
    assert "subtotal - discount + shipping + tax 421.00, total 430.00" in note


def test_duplicate_check():
    ledger_candidates = [("Northwind Supply", "INV-100", 250.0, date(2026, 8, 1), "ledger.csv")]

    # rule a: same vendor + same invoice number
    note = duplicate_check("Northwind Supply", "INV-100", 999.0, date(2026, 9, 1), ledger_candidates)
    assert "same vendor + invoice number" in note

    # rule b: same vendor + same total + invoice dates within 3 days, different invoice number
    close = [("Northwind Supply", "INV-777", 250.0, date(2026, 8, 2), "scan_0003.pdf")]
    note = duplicate_check("Northwind Supply", "INV-100", 250.0, date(2026, 8, 4), close)
    assert "same vendor + total, invoice date within 3 days" in note
    assert "scan_0003.pdf" in note

    # negative: same vendor + same total but invoice dates too far apart -- not a duplicate
    far = [("Northwind Supply", "INV-777", 250.0, date(2026, 8, 1), "scan_0003.pdf")]
    assert duplicate_check("Northwind Supply", "INV-100", 250.0, date(2026, 8, 10), far) == ""

    # different vendor entirely -- not a duplicate
    assert duplicate_check("Quik Fuel", "INV-100", 250.0, date(2026, 8, 1), ledger_candidates) == ""


def test_find_totals():
    assert find_totals("SUBTOTAL 100.00\nTOTAL 112.00") == [112.00]
    assert find_totals("Total: $1,234.50") == [1234.50]
    assert find_totals("Amount Due: 88.10") == [88.10]
    assert find_totals("nothing here to find") == []
    # dedup, in order
    assert find_totals("TOTAL DUE 50.00 ... GRAND TOTAL 50.00") == [50.00]


def test_total_cross_check():
    assert total_cross_check({"ocr_totals": [], "total": 100.0}) == ""  # no OCR, nothing to check
    assert total_cross_check({"ocr_totals": [100.0], "total": 100.0}) == ""  # agree
    assert total_cross_check({"ocr_totals": [100.0], "total": None, "amount_due": 100.0}) == ""  # amount_due agrees
    from capture import find_amounts
    assert find_amounts("Subtotal: Tax: TOTAL: AMOUNT $250.00 $70.80 $955.80 IO reams") == [250.0, 70.8, 955.8]
    assert find_amounts("$1,234.50 and 12.3 and 7") == [1234.5]
    # no labelled total: model total must appear among OCR amounts
    assert total_cross_check({"ocr_totals": [], "ocr_amounts": [250.0, 955.8], "total": 955.8}) == ""
    assert "not among OCR amounts" in total_cross_check({"ocr_totals": [], "ocr_amounts": [250.0, 955.8], "total": 955.3})
    assert total_cross_check({"ocr_totals": [], "ocr_amounts": [250.0], "total": None}) == ""
    assert total_cross_check({"ocr_totals": [], "ocr_amounts": [180.0], "total": -180.0}) == ""  # credit memo
    note = total_cross_check({"ocr_totals": [100.0], "total": 250.0, "amount_due": None})
    assert "Total conflict" in note


def test_flag_ordering_and_needs_review():
    today = date(2026, 9, 16)
    # NOT_INVOICE, MISSING_INFO, LOW_CONFIDENCE together -- must come out in canonical order
    data = {"document_type": "statement", "marked_paid": False, "vendor_name": None,
            "total": None, "missing_info": ["account number"], "confidence": "low"}
    flags, notes = compute_flags(data, None, today, True, "", "", "")
    assert flags == ["NOT_INVOICE", "MISSING_INFO", "LOW_CONFIDENCE"]
    assert notes == ["Missing: account number"]

    clean = {"document_type": "invoice", "marked_paid": False, "vendor_name": "Northwind Supply",
              "total": 100.0, "missing_info": [], "confidence": "high"}
    flags, _ = compute_flags(clean, None, today, True, "", "", "")
    assert flags == []


def test_overdue_and_due_soon_boundaries():
    today = date(2026, 9, 16)
    base = {"document_type": "invoice", "marked_paid": False, "vendor_name": "Northwind Supply",
            "total": 100.0, "missing_info": [], "confidence": "high"}

    # due yesterday -> OVERDUE
    flags, _ = compute_flags(base, date(2026, 9, 15), today, True, "", "", "")
    assert flags == ["OVERDUE"]

    # due today -> DUE_SOON (today counts as within the window)
    flags, _ = compute_flags(base, date(2026, 9, 16), today, True, "", "", "")
    assert flags == ["DUE_SOON"]

    # due exactly 7 days out -> still DUE_SOON
    flags, _ = compute_flags(base, date(2026, 9, 23), today, True, "", "", "")
    assert flags == ["DUE_SOON"]

    # due 8 days out -> neither
    flags, _ = compute_flags(base, date(2026, 9, 24), today, True, "", "", "")
    assert flags == []

    # marked paid -- never OVERDUE/DUE_SOON even if the date is in the window
    paid = {**base, "marked_paid": True}
    flags, _ = compute_flags(paid, date(2026, 9, 15), today, True, "", "", "")
    assert "OVERDUE" not in flags and "DUE_SOON" not in flags
    assert flags == ["MARKED_PAID"]


def test_build_new_filename_collision():
    existing = set()
    name1 = build_new_filename("Northwind Supply", "INV-100", "09/16/2026", existing)
    assert name1 == "Northwind_Supply_INV_100_20260916.pdf"
    name2 = build_new_filename("Northwind Supply", "INV-100", "09/16/2026", existing)
    assert name2 == "Northwind_Supply_INV_100_20260916_2.pdf"
    name3 = build_new_filename("Northwind Supply", "INV-100", "09/16/2026", existing)
    assert name3 == "Northwind_Supply_INV_100_20260916_3.pdf"
    assert existing == {name1, name2, name3}

    # missing invoice number / date fall back to placeholders
    name4 = build_new_filename("Acme", None, None, set())
    assert name4 == "Acme_noinv_nodate.pdf"


def test_to_review_row():
    record = {
        "source_file": "scan_0001.pdf", "document_type": "invoice", "read_by": "text",
        "vendor": "Northwind Supply",
        "invoice_number": "INV-100", "invoice_date": "09/16/2026", "due": "10/16/2026",
        "total": 162.5, "category": "Office Supplies", "flags": ["DUE_SOON", "MISSING_INFO"],
        "notes": ["Missing: po number"], "needs_review": True, "new_file": "Northwind_INV100.pdf",
        "reviewed": "",
    }
    row = to_review_row(record)
    assert row == {
        "file": "scan_0001.pdf", "type": "invoice", "read_by": "text", "vendor": "Northwind Supply",
        "invoice_no": "INV-100", "date": "09/16/2026", "due": "10/16/2026", "total": "162.50",
        "category": "Office Supplies", "flags": "DUE_SOON;MISSING_INFO",
        "notes": "Missing: po number", "needs_review": "True", "new_file": "Northwind_INV100.pdf",
        "reviewed": "",
    }

    blank = to_review_row({"source_file": "x.pdf", "document_type": "statement"})
    assert blank["total"] == "" and blank["needs_review"] == "False" and blank["flags"] == ""
    assert blank["reviewed"] == "" and blank["read_by"] == ""

    # read_by column sits right after type, per SPEC3 B.6
    assert REVIEW_FIELDS.index("read_by") == REVIEW_FIELDS.index("type") + 1


def test_qbo_rows():
    invoice = {
        "document_type": "invoice", "flags": [], "invoice_number": "INV-100",
        "vendor": "Northwind Supply", "invoice_date": "09/01/2026", "due": "10/01/2026",
        "terms": "Net 30", "category": "Office Supplies", "source_file": "scan_0001.pdf",
        "currency": "USD", "tax": 12.00,
        "line_items": [{"description": "Widgets", "amount": 100.0},
                       {"description": "Gadgets", "amount": 50.0}],
    }
    rows = qbo_rows(invoice)
    assert len(rows) == 3  # 2 line items + 1 sales tax row
    assert rows[0]["LineDescription"] == "Widgets" and rows[0]["LineAmount"] == "100.00"
    assert rows[1]["LineDescription"] == "Gadgets" and rows[1]["LineAmount"] == "50.00"
    assert rows[2]["LineDescription"] == "Sales tax" and rows[2]["LineAmount"] == "12.00"
    for r in rows:
        assert r["BillNo"] == "INV-100" and r["Supplier"] == "Northwind Supply"
        assert r["Account"] == "Office Supplies" and r["Memo"] == "scan_0001.pdf"

    # no line items -> one row from subtotal/total + summary
    no_items = {"document_type": "invoice", "flags": [], "invoice_number": "INV-101",
                "vendor": "Quik Fuel", "line_items": [], "subtotal": None, "total": 42.0,
                "summary": "Fuel purchase", "tax": None}
    rows = qbo_rows(no_items)
    assert len(rows) == 1
    assert rows[0]["LineDescription"] == "Fuel purchase" and rows[0]["LineAmount"] == "42.00"

    # qbo_vendor (client's QuickBooks spelling) takes priority over vendor for Supplier
    renamed = {**invoice, "vendor": "Northwind Supply", "qbo_vendor": "Northwind Supply Inc"}
    assert qbo_rows(renamed)[0]["Supplier"] == "Northwind Supply Inc"

    # excluded: DUPLICATE, MARKED_PAID, and non-invoice document types (receipts, statements)
    assert qbo_rows({**invoice, "flags": ["DUPLICATE"]}) == []
    assert qbo_rows({**invoice, "flags": ["MARKED_PAID"]}) == []
    assert qbo_rows({**invoice, "document_type": "receipt"}) == []
    assert qbo_rows({**invoice, "document_type": "statement"}) == []


def test_normalize_payment_method():
    assert normalize_payment_method("VISA ****4477 APPROVED") == "Credit Card"
    assert normalize_payment_method("MASTERCARD ****9910") == "Credit Card"
    assert normalize_payment_method("CASH TENDERED $60.00 CHANGE $1.60") == "Cash"
    assert normalize_payment_method("Check 1042") == "Check"
    assert normalize_payment_method("Cheque 1042") == "Check"
    assert normalize_payment_method("Store credit") == "Store credit"  # printed as-is
    assert normalize_payment_method(None) == ""
    assert normalize_payment_method("") == ""


def test_qbo_expense_rows():
    receipt = {
        "document_type": "receipt", "flags": [], "invoice_number": None,
        "vendor": "Maple Hardware", "qbo_vendor": "Maple Hardware",
        "invoice_date": "09/10/2026", "category": "Job Materials", "source_file": "scan_0009.pdf",
        "currency": "USD", "tax": 4.50, "payment_method": "VISA ****4477 APPROVED",
        "line_items": [{"description": "Lumber", "amount": 60.00}],
    }
    rows = qbo_expense_rows(receipt)
    assert len(rows) == 2  # 1 line item + 1 sales tax row
    assert rows[0]["Category Description"] == "Lumber" and rows[0]["Category Line Amount"] == "60.00"
    assert rows[1]["Category Description"] == "Sales tax" and rows[1]["Category Line Amount"] == "4.50"
    for r in rows:
        assert r["Payee"] == "Maple Hardware" and r["Payment Method"] == "Credit Card"
        assert r["Category Account"] == "Job Materials" and r["Memo"] == "scan_0009.pdf"
        assert r["Account"] == ""  # bank/card account left for the bookkeeper
        assert r["Payment Date"] == "09/10/2026"

    # no line items -> one row from subtotal/total + summary
    no_items = {"document_type": "receipt", "flags": [], "vendor": "Quik Fuel 12",
                "line_items": [], "subtotal": None, "total": 38.20, "summary": "Fuel",
                "payment_method": "Cash", "tax": None}
    rows = qbo_expense_rows(no_items)
    assert len(rows) == 1
    assert rows[0]["Category Description"] == "Fuel" and rows[0]["Category Line Amount"] == "38.20"

    # excluded: DUPLICATE, and non-receipt document types
    assert qbo_expense_rows({**receipt, "flags": ["DUPLICATE"]}) == []
    assert qbo_expense_rows({**receipt, "document_type": "invoice"}) == []


def test_make_error_record_through_apply_rules():
    rec = make_error_record("scan_0099.pdf", "Expecting value: line 1 column 1")
    assert rec["source_file"] == "scan_0099.pdf"
    assert rec["is_invoice"] is False
    assert rec["confidence"] == "low"
    assert rec["missing_info"]
    apply_rules(rec, VENDORS, [], date(2026, 9, 16), [])
    assert rec["vendor"] == ""
    assert rec["needs_review"] is True
    # document_type "other" -> NOT_INVOICE, plus LOW_CONFIDENCE and MISSING_INFO
    assert rec["flags"] == ["NOT_INVOICE", "MISSING_INFO", "LOW_CONFIDENCE"]


def test_apply_rules_end_to_end():
    """Full pipeline through apply_rules: vendor match, money coercion, due date,
    duplicate detection against an earlier record in the same batch."""
    today = date(2026, 9, 16)
    first = {
        "document_type": "invoice", "is_invoice": True, "vendor_name": "Northwind Supply Co.",
        "invoice_number": "INV-500", "invoice_date": "08/20/2026", "due_date": None,
        "payment_terms": None, "subtotal": "200.00", "tax": "16.00", "total": "216.00",
        "amount_due": None, "line_items": [{"description": "Paper", "amount": "200.00"}],
        "marked_paid": False, "missing_info": [], "confidence": "high", "source_file": "scan_0001.pdf",
    }
    apply_rules(first, VENDORS, [], today, [])
    assert first["vendor"] == "Northwind Supply"  # suffix stripped, matched, canonical name used
    assert first["category"] == "Office Supplies"
    assert first["terms"] == "Net 30"  # from vendors.csv, model gave none
    assert first["due"] == "09/19/2026"  # 08/20 + 30 days
    assert first["total"] == 216.00
    assert first["flags"] == ["DUE_SOON"]  # math checks out; due is 3 days from today

    dup = {
        "document_type": "invoice", "is_invoice": True, "vendor_name": "Northwind Supply Co.",
        "invoice_number": "INV-500", "invoice_date": "08/20/2026", "due_date": None,
        "payment_terms": None, "subtotal": "200.00", "tax": "16.00", "total": "216.00",
        "amount_due": None, "line_items": [{"description": "Paper", "amount": "200.00"}],
        "marked_paid": False, "missing_info": [], "confidence": "high", "source_file": "scan_0002.pdf",
    }
    apply_rules(dup, VENDORS, [], today, [first])
    assert dup["flags"] == ["DUPLICATE", "DUE_SOON"]
    assert "scan_0001.pdf" in dup["notes"][0]

    # duplicate check against the ledger ignores a ledger row that is this record's own
    # source_file (an exported bill rerunning must not flag itself)
    ledger = [{"vendor": "Northwind Supply", "invoice_no": "INV-500", "date": "08/20/2026",
               "total": "216.00", "source_file": "scan_0001.pdf"}]
    rerun_self = {**first, "source_file": "scan_0001.pdf"}
    apply_rules(rerun_self, VENDORS, ledger, today, [])
    assert "DUPLICATE" not in rerun_self["flags"]

    # a different file with the same vendor+invoice number IS flagged against that ledger row
    other_file = {**first, "source_file": "scan_0003.pdf"}
    apply_rules(other_file, VENDORS, ledger, today, [])
    assert "DUPLICATE" in other_file["flags"]

    # qbo_vendor: vendors.csv qbo_vendor_name wins, blank falls back to canonical vendor_name
    vendors_with_qbo = [
        {"vendor_name": "Northwind Supply", "qbo_vendor_name": "Northwind Supply Inc",
         "default_category": "Office Supplies", "default_terms": "Net 30", "notes": ""},
        {"vendor_name": "Quik Fuel", "qbo_vendor_name": "", "default_category": "Fuel",
         "default_terms": "Due on receipt", "notes": ""},
    ]
    renamed = {**first, "source_file": "scan_0004.pdf"}
    apply_rules(renamed, vendors_with_qbo, [], today, [])
    assert renamed["qbo_vendor"] == "Northwind Supply Inc"

    blank_qbo = {**first, "source_file": "scan_0005.pdf", "vendor_name": "Quik Fuel"}
    apply_rules(blank_qbo, vendors_with_qbo, [], today, [])
    assert blank_qbo["qbo_vendor"] == "Quik Fuel"  # blank qbo_vendor_name -> canonical name

    # no match at all -> qbo_vendor falls back to the model's own vendor_name
    unknown = {**first, "source_file": "scan_0006.pdf", "vendor_name": "Totally Unrelated LLC"}
    apply_rules(unknown, vendors_with_qbo, [], today, [])
    assert unknown["qbo_vendor"] == "Totally Unrelated LLC"


def test_opencode_event_parsing():
    from capture import opencode_text_from_events
    stdout = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"text","part":{"type":"text","text":"```json\\n{\\"total\\": "}}\n'
        'not json noise line\n'
        '{"type":"text","part":{"type":"text","text":"12.00}\\n```"}}\n'
        '{"type":"step_finish","part":{"reason":"stop"}}\n'
    )
    text = opencode_text_from_events(stdout)
    assert parse_model_json(text) == {"total": 12.00}


def test_openai_backend_against_mock_server():
    """Spin up a fake /chat/completions, send a real invoice PDF through extract_openai."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from capture import extract_openai

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["model"] = body["model"]
            seen["images"] = sum(1 for p in body["messages"][0]["content"] if p["type"] == "image_url")
            seen["has_prompt"] = "is_invoice" in body["messages"][0]["content"][0]["text"]
            reply = {"choices": [{"message": {"content": '```json\n{"is_invoice": true, "document_type": "invoice", "total": 12.0}\n```'}}]}
            out = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):  # quiet
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pdf = next(iter(sorted(Path("invoices_in").glob("*.pdf"))), None)
        if pdf is None:
            print("  (skipped: no invoices_in PDFs)")
            return
        data = extract_openai(pdf, "test-model", f"http://127.0.0.1:{srv.server_port}/v1", "sk-test")
    finally:
        srv.shutdown()
    assert data["document_type"] == "invoice"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["model"] == "test-model"
    assert seen["images"] >= 1 and seen["has_prompt"]


def test_reviewed_prefill_and_carryover():
    # new file (no row in the previous review.csv): pre-fill -- "ok" when clean, "" when flagged
    assert compute_reviewed("scan_0001.pdf", False, {}) == "ok"
    assert compute_reviewed("scan_0002.pdf", True, {}) == ""

    # existing file: whatever the bookkeeper left survives a rerun, even if it looks
    # stale for this run's flags -- their decision is never overwritten
    prior = {"scan_0001.pdf": "skip", "scan_0002.pdf": "ok", "scan_0003.pdf": ""}
    assert compute_reviewed("scan_0001.pdf", False, prior) == "skip"
    assert compute_reviewed("scan_0002.pdf", True, prior) == "ok"
    assert compute_reviewed("scan_0003.pdf", False, prior) == ""  # carried blank, not re-prefilled


def test_read_reviewed_map():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "review.csv"
        assert read_reviewed_map(path) == {}  # no file yet -- nothing to carry over

        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "reviewed"])
            writer.writeheader()
            writer.writerow({"file": "scan_0001.pdf", "reviewed": "ok"})
            writer.writerow({"file": "scan_0002.pdf", "reviewed": ""})
        assert read_reviewed_map(path) == {"scan_0001.pdf": "ok", "scan_0002.pdf": ""}


def test_load_vendors_old_and_new_columns():
    with tempfile.TemporaryDirectory() as tmp:
        old_path = Path(tmp) / "vendors_old.csv"
        old_path.write_text("vendor_name,default_category,default_terms,notes\n"
                             "Acme Co,Office Supplies,Net 30,\n")
        rows = load_vendors(old_path)
        assert rows[0]["vendor_name"] == "Acme Co"
        assert rows[0].get("qbo_vendor_name") is None  # column doesn't exist at all -- tolerated

        new_path = Path(tmp) / "vendors_new.csv"
        new_path.write_text("vendor_name,qbo_vendor_name,default_category,default_terms,notes\n"
                             "Acme Co,Acme Corporation,Office Supplies,Net 30,\n")
        rows = load_vendors(new_path)
        assert rows[0]["qbo_vendor_name"] == "Acme Corporation"


def test_export_dir():
    """--export end to end: reviewed=='ok' invoices -> bills, receipts -> expenses, skip/blank
    held, non-invoice/receipt 'ok' rows warned-and-skipped, ledger append is idempotent."""
    records = [
        {"source_file": "scan_0001.pdf", "document_type": "invoice", "vendor": "Northwind Supply",
         "qbo_vendor": "Northwind Supply", "invoice_number": "INV-700", "invoice_date": "09/01/2026",
         "due": "10/01/2026", "terms": "Net 30", "category": "Office Supplies", "currency": "USD",
         "tax": None, "subtotal": 100.0, "total": 100.0,
         "line_items": [{"description": "Paper", "amount": 100.0}], "flags": [], "needs_review": False},
        {"source_file": "scan_0002.pdf", "document_type": "receipt", "vendor": "Maple Hardware",
         "qbo_vendor": "Maple Hardware", "invoice_number": None, "invoice_date": "09/05/2026",
         "category": "Job Materials", "currency": "USD", "tax": None, "subtotal": 20.0, "total": 20.0,
         "payment_method": "Cash", "line_items": [{"description": "Nails", "amount": 20.0}],
         "flags": [], "needs_review": False},
        {"source_file": "scan_0003.pdf", "document_type": "invoice", "vendor": "Cedar Ridge Electric",
         "qbo_vendor": "Cedar Ridge Electric", "invoice_number": "INV-701", "invoice_date": "09/02/2026",
         "category": "Utilities", "currency": "USD", "tax": None, "subtotal": 50.0, "total": 50.0,
         "line_items": [{"description": "Service", "amount": 50.0}], "flags": [], "needs_review": False},
        {"source_file": "scan_0004.pdf", "document_type": "invoice", "vendor": "Blue Anchor Software",
         "qbo_vendor": "Blue Anchor Software", "invoice_number": "INV-702", "invoice_date": "09/03/2026",
         "category": "Software", "currency": "USD", "tax": None, "subtotal": 30.0, "total": 30.0,
         "line_items": [{"description": "License", "amount": 30.0}], "flags": [], "needs_review": False},
        {"source_file": "scan_0005.pdf", "document_type": "statement", "vendor": "Granite Peak Consulting",
         "qbo_vendor": "Granite Peak Consulting", "invoice_number": None, "invoice_date": None,
         "category": "", "currency": "USD", "tax": None, "subtotal": None, "total": None,
         "line_items": [], "flags": ["NOT_INVOICE"], "needs_review": True},
        {"source_file": "scan_0006.pdf", "document_type": "invoice", "vendor": "Northwind Supply",
         "qbo_vendor": "Northwind Supply", "invoice_number": "INV-700", "invoice_date": "09/01/2026",
         "category": "Office Supplies", "currency": "USD", "tax": None, "subtotal": 100.0, "total": 100.0,
         "line_items": [{"description": "Paper", "amount": 100.0}], "flags": ["DUPLICATE"],
         "needs_review": True},
    ]
    reviewed = {"scan_0001.pdf": "ok", "scan_0002.pdf": "ok", "scan_0003.pdf": "skip",
                "scan_0004.pdf": "", "scan_0005.pdf": "ok", "scan_0006.pdf": "ok"}

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        out.mkdir()
        (out / "results.json").write_text(json.dumps(records))
        with (out / "review.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "reviewed"])
            writer.writeheader()
            for src, rv in reviewed.items():
                writer.writerow({"file": src, "reviewed": rv})

        ledger_path = Path(tmp) / "ledger.csv"
        with ledger_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["vendor", "invoice_no", "date", "total", "source_file"])
            writer.writeheader()
            writer.writerow({"vendor": "Old Vendor", "invoice_no": "OLD-1", "date": "01/01/2026",
                             "total": "5.00", "source_file": "old_scan.pdf"})

        export_dir(out, str(ledger_path))

        bills = list(csv.DictReader((out / "quickbooks_bills.csv").open(newline="")))
        assert len(bills) == 1  # only scan_0001 (invoice, ok); scan_0006 is DUPLICATE
        assert bills[0]["BillNo"] == "INV-700" and bills[0]["LineAmount"] == "100.00"

        expenses = list(csv.DictReader((out / "quickbooks_expenses.csv").open(newline="")))
        assert len(expenses) == 1  # only scan_0002 (receipt, ok)
        assert expenses[0]["Category Description"] == "Nails"

        ledger_rows = list(csv.DictReader(ledger_path.open(newline="")))
        assert len(ledger_rows) == 3  # 1 pre-existing + scan_0001 + scan_0002
        assert {r["source_file"] for r in ledger_rows} == {"old_scan.pdf", "scan_0001.pdf", "scan_0002.pdf"}

        # idempotent: exporting again (nothing changed) does not duplicate ledger rows
        export_dir(out, str(ledger_path))
        ledger_rows_2 = list(csv.DictReader(ledger_path.open(newline="")))
        assert len(ledger_rows_2) == 3


def test_has_real_text():
    long_with_labels = "INVOICE #1042\nTotal Due: $100.00\n" + "line of body text. " * 40
    assert has_real_text(long_with_labels, 1) is True
    # near-empty text -- a label is present but nowhere near 200 chars
    assert has_real_text("Total", 1) is False
    assert has_real_text("", 1) is False
    # long text but none of the usual billing labels -- not a billing text layer
    long_no_labels = "lorem ipsum dolor sit amet consectetur adipiscing elit " * 20
    assert has_real_text(long_no_labels, 1) is False
    # threshold scales with page count: same text, more pages -> no longer "real"
    assert has_real_text(long_with_labels, 5) is False


def test_text_layer_missing_markitdown():
    """Simulate markitdown not being installed: import returns "" so the whole
    pipeline still runs on vision. Page count still comes from pypdfium2."""
    pdfs = sorted(Path("invoices_in").glob("*.pdf")) if Path("invoices_in").exists() else []
    if not pdfs:
        print("  (skipped: no invoices_in PDFs)")
        return
    had = "markitdown" in sys.modules
    prior = sys.modules.get("markitdown")
    sys.modules["markitdown"] = None  # forces "import markitdown" to raise ImportError
    try:
        text, pages = text_layer(pdfs[0])
    finally:
        if had:
            sys.modules["markitdown"] = prior
        else:
            sys.modules.pop("markitdown", None)
    assert text == ""
    assert pages >= 1


def test_extract_routes_text_vs_vision():
    """extract() routing, with text_layer/extract_text/extract_vision monkeypatched --
    no model calls, no network, no real PDF needed."""
    import capture
    calls = []

    def fake_extract_text(pdf_path, text, model, base_url, api_key):
        calls.append(("text", model))
        return {"document_type": "invoice", "total": 1.0}

    def fake_extract_vision(pdf_path, model, base_url, api_key):
        calls.append(("vision", model))
        return {"document_type": "invoice", "total": 2.0}

    orig_text_layer, orig_extract_text, orig_extract_vision = (
        capture.text_layer, capture.extract_text, capture.extract_vision)
    capture.extract_text = fake_extract_text
    capture.extract_vision = fake_extract_vision
    real_text = "INVOICE TOTAL " * 30 + "Amount Due 100.00"
    try:
        # real text + labels, text_first on -> text path
        capture.text_layer = lambda p: (real_text, 1)
        data = capture.extract(Path("fake.pdf"), text_first=True)
        assert data["read_by"] == "text" and calls[-1] == ("text", "sonnet")
        assert data["text_chars"] == len(real_text)

        # near-empty text -> falls back to vision even with text_first on
        capture.text_layer = lambda p: ("", 1)
        data = capture.extract(Path("fake.pdf"), text_first=True)
        assert data["read_by"] == "vision" and calls[-1] == ("vision", "sonnet")

        # text_first off (the default) -> always vision, even with real text present
        capture.text_layer = lambda p: (real_text, 1)
        data = capture.extract(Path("fake.pdf"), text_first=False)
        assert data["read_by"] == "vision"
        assert data["text_chars"] == 0  # text_layer never called when text_first is off

        # text_model overrides model for the text path only
        data = capture.extract(Path("fake.pdf"), model="sonnet", text_first=True,
                               text_model="haiku")
        assert calls[-1] == ("text", "haiku")

        # cost/duration/token keys are always present, None for a non-claude backend
        for key in ("cost_usd", "duration_ms", "input_tokens", "output_tokens"):
            assert key in data and data[key] is None
    finally:
        capture.text_layer, capture.extract_text, capture.extract_vision = (
            orig_text_layer, orig_extract_text, orig_extract_vision)


def test_split_by_path():
    from check_results import split_by_path
    expected = {"a.pdf": {"vendor": "Acme"}, "b.pdf": {"vendor": "Acme"},
                "c.pdf": {"vendor": "Widgets"}}
    review_rows = {"a.pdf": {"vendor": "Acme"}, "b.pdf": {"vendor": "Wrong"},
                   "c.pdf": {"vendor": "Widgets"}}
    results_by_file = {
        "a.pdf": {"read_by": "text", "cost_usd": 0.004, "duration_ms": 6000},
        "b.pdf": {"read_by": "text", "cost_usd": 0.006, "duration_ms": 8000},
        "c.pdf": {"read_by": "vision", "cost_usd": 0.04, "duration_ms": 15000},
    }
    split = split_by_path(expected, review_rows, results_by_file)
    assert split["text"]["docs"] == 2 and split["text"]["fields"] == 2
    assert split["text"]["correct"] == 1  # a.pdf right, b.pdf wrong
    assert abs(split["text"]["avg_cost"] - 0.005) < 1e-9
    assert abs(split["text"]["avg_duration"] - 7.0) < 1e-9
    assert split["vision"]["docs"] == 1 and split["vision"]["correct"] == 1
    assert abs(split["vision"]["avg_duration"] - 15.0) < 1e-9

    # a doc missing from results.json entirely -> grouped under "unknown", not crashed
    split2 = split_by_path({"z.pdf": {"vendor": "X"}}, {"z.pdf": {"vendor": "X"}}, {})
    assert split2["unknown"]["docs"] == 1 and split2["unknown"]["correct"] == 1
    assert split2["unknown"]["avg_cost"] == 0.0


def test_find_amounts_ocr_noise():
    from capture import find_amounts
    # real OCR output from a phone-photo receipt: stray space and letter o for zero
    noisy = "SUBTOTAL TAX TOTAL VISA 4477 APPROVED $28. se $9.67 $38.17 $4.oo $42 .17"
    got = find_amounts(noisy)
    assert 42.17 in got and 4.0 in got and 38.17 in got and 9.67 in got, got


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("OK")

def test_export_lines_balance():
    from capture import export_lines
    # clean: lines + shipping - discount + tax == total
    rec = {"total": 455.40, "tax": 30.40, "shipping": 45.00, "discount": None,
           "line_items": [{"description": "A", "amount": 380.00}], "summary": "s"}
    lines = export_lines(rec)
    assert [d for d, _ in lines] == ["A", "Shipping", "Sales tax"]
    assert abs(sum(a for _, a in lines) - 455.40) < 0.005
    rec = {"total": 1550.00, "tax": None, "discount": 50.00,
           "line_items": [{"description": "A", "amount": 1600.00}], "summary": "s"}
    assert export_lines(rec) == [("A", 1600.00), ("Discount", -50.00)]
    # a line amount missing -> one line for the total
    rec = {"total": 58.40, "tax": None, "line_items": [{"description": "fuel", "amount": None}], "summary": "Fuel receipt"}
    assert export_lines(rec) == [("Fuel receipt", 58.40)]
    # lines present but do not add up (math error) -> one line for the total, tax kept separate
    rec = {"total": 333.72, "tax": 24.72, "line_items": [{"description": "A", "amount": 300.00}], "summary": "s"}
    lines = export_lines(rec)
    assert lines == [("s", 309.00), ("Sales tax", 24.72)]
    assert export_lines({"total": None, "line_items": []}) == []


if __name__ == "__main__":
    main()
