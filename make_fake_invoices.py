"""
Generate 20 synthetic invoice/receipt/statement/credit-memo PDFs for
invoice-capture testing, plus vendors.csv, ledger.csv and expected.json.

ALL data here is fake: vendor names, addresses, 555 phone numbers,
example.com emails. Nothing represents a real company or person. Only the
invoice number appears as an identifier on each document -- no SSNs, no tax
IDs, no personal info about the tool's owner.

Pillow + pypdfium2 only. Render helpers (font/new_page/add_speckle/
finalize_page/save_pdf) are copied from fax-triage/make_fake_faxes.py, not
imported -- this project must not depend on fax-triage.

Dates are relative to date.today() so the demo output (overdue, due-soon,
etc.) is always correct regardless of when this is run.
"""
import os
import csv
import json
import random
from datetime import date, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageChops

random.seed(42)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "invoices_in")
PAGE_W, PAGE_H = 1700, 2200  # 8.5x11in @ 200dpi
DPI = 200
TODAY = date.today()

FONT_CACHE = {}
ARIAL = r"C:\Windows\Fonts\arial.ttf"
ARIAL_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
CONSOLA = r"C:\Windows\Fonts\consola.ttf"
HAND = r"C:\Windows\Fonts\Inkfree.ttf"

BILL_TO = "Riverton Holdings LLC\n300 Commerce Dr, Riverton, IL 55555"


# ---------------------------------------------------------------------------
# Helpers copied (not imported) from fax-triage/make_fake_faxes.py
# ---------------------------------------------------------------------------

def font(size, bold=False):
    path = ARIAL_BOLD if bold else ARIAL
    key = (path, size)
    if key not in FONT_CACHE:
        FONT_CACHE[key] = ImageFont.truetype(path, size)
    return FONT_CACHE[key]


def mono_font(size):
    key = (CONSOLA, size)
    if key not in FONT_CACHE:
        FONT_CACHE[key] = ImageFont.truetype(CONSOLA, size)
    return FONT_CACHE[key]


def hand_font(size):
    key = (HAND, size)
    if key not in FONT_CACHE:
        FONT_CACHE[key] = ImageFont.truetype(HAND, size)
    return FONT_CACHE[key]


def new_page(w=PAGE_W, h=PAGE_H, color=255):
    return Image.new("L", (w, h), color=color)


def add_speckle(img, count=1200):
    """Sparse dark/light speckle noise, cheap (no numpy needed)."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for _ in range(count):
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        shade = random.choice([0, 60, 200, 255, 255])
        draw.point((x, y), fill=shade)
    return img


def finalize_page(img, rotate_deg=None, low_contrast=False, speckle=True):
    if speckle:
        add_speckle(img)
    if low_contrast:
        img = ImageEnhance.Contrast(img).enhance(0.55)
        img = ImageEnhance.Brightness(img).enhance(1.08)
    if rotate_deg is None:
        rotate_deg = random.uniform(0.15, 0.5) * random.choice([-1, 1])
    img = img.rotate(rotate_deg, resample=Image.BICUBIC, fillcolor=255, expand=False)
    return img.convert("RGB")


def save_pdf(pages, filename):
    path = os.path.join(OUT_DIR, filename)
    pages[0].save(path, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    return path


# ---------------------------------------------------------------------------
# money / date formatting
# ---------------------------------------------------------------------------

def money(n):
    """Printed representation: '$1,234.50' (always positive form)."""
    return f"${abs(n):,.2f}"


def fmt_date(d):
    return d.strftime("%m/%d/%Y")


def dt(offset_days):
    return TODAY + timedelta(days=offset_days)


def exp_total(n):
    """expected.json total string: f'{total:.2f}', handles negatives."""
    return f"{n:.2f}"


# ---------------------------------------------------------------------------
# Shared invoice-drawing building blocks
# ---------------------------------------------------------------------------

def logo_box(draw, x, y, vendor, w=340, h=130):
    draw.rectangle([x, y, x + w, y + h], outline=0, width=3)
    initials = "".join(w[0] for w in vendor.split() if w[0].isalpha())[:4]
    draw.text((x + 18, y + h // 2 - 20), initials.upper(), font=font(40, bold=True), fill=0)
    return y + h


def vendor_block(draw, x, y, vendor, address, phone, email, name_size=34):
    draw.text((x, y), vendor, font=font(name_size, bold=True), fill=0)
    y += name_size + 10
    draw.text((x, y), address, font=font(20), fill=0)
    y += 28
    draw.text((x, y), f"Phone: {phone}   Email: {email}", font=font(20), fill=0)
    y += 34
    return y


def line_item_table(draw, x0, y, items, grid=True, width=1600):
    col_desc = x0
    col_qty = x0 + 940
    col_unit = x0 + 1080
    col_amt = x0 + 1320
    hdr = font(23, bold=True)
    draw.text((col_desc, y), "DESCRIPTION", font=hdr, fill=0)
    draw.text((col_qty, y), "QTY", font=hdr, fill=0)
    draw.text((col_unit, y), "UNIT PRICE", font=hdr, fill=0)
    draw.text((col_amt, y), "AMOUNT", font=hdr, fill=0)
    y += 36
    draw.line([(x0, y - 4), (x0 + width, y - 4)], fill=0, width=2)
    for desc, qty, unit, amt in items:
        draw.text((col_desc, y), desc, font=font(21), fill=0)
        if qty is not None:
            draw.text((col_qty, y), str(qty), font=font(21), fill=0)
        if unit is not None:
            draw.text((col_unit, y), money(unit), font=font(21), fill=0)
        draw.text((col_amt, y), money(amt), font=font(21), fill=0)
        y += 40
        if grid:
            draw.line([(x0, y - 10), (x0 + width, y - 10)], fill=0, width=1)
    return y + 10


def totals_block(draw, x, y, subtotal, tax, total, credit=False, right_edge=1620):
    def row(label, value_str, bold=False):
        nonlocal y
        f = font(25, bold=bold)
        draw.text((x, y), label, font=f, fill=0)
        vw = draw.textlength(value_str, font=f)
        draw.text((right_edge - vw, y), value_str, font=f, fill=0)
        y += 40

    row("Subtotal:", money(subtotal))
    if tax:
        row("Tax:", money(tax))
    total_str = f"-{money(total)}" if credit else money(total)
    row("TOTAL:", total_str, bold=True)
    return y


def stamp_paid(img, text_lines):
    """Paste a rotated handwritten PAID stamp over the given page (in place),
    roughly centered over the lower-right totals area."""
    sw, sh = 460, 190
    stamp = Image.new("L", (sw, sh), color=255)
    sd = ImageDraw.Draw(stamp)
    sd.rectangle([6, 6, sw - 6, sh - 6], outline=70, width=7)
    sd.text((30, 15), text_lines[0], font=hand_font(70), fill=60)
    if len(text_lines) > 1:
        sd.text((30, 100), text_lines[1], font=hand_font(44), fill=60)
    stamp = stamp.rotate(-13, resample=Image.BICUBIC, fillcolor=255, expand=True)
    img.paste(stamp, (1050, 1650))


# ---------------------------------------------------------------------------
# render_invoice(spec) -- one generic renderer, layout switch per spec
# ---------------------------------------------------------------------------

def render_invoice(spec):
    """spec keys: vendor, address, phone, email, invoice_no, invoice_date,
    due_date (date|None), due_label (str|None, printed as-is e.g. 'Net 15'),
    po (str|None), items [(desc,qty,unit,amt)], subtotal, tax, total,
    layout, paid_stamp (bool), credit (bool), doc_title (str)."""
    layout = spec["layout"]
    title = spec.get("doc_title", "INVOICE")
    pages = []

    img = new_page()
    d = ImageDraw.Draw(img)

    if layout == "box_right":
        y = vendor_block(d, 60, 60, spec["vendor"], spec["address"], spec["phone"], spec["email"])
        logo_box(d, 1260, 60, spec["vendor"])
    else:
        logo_box(d, 60, 60, spec["vendor"])
        y = vendor_block(d, 60, 210, spec["vendor"], spec["address"], spec["phone"], spec["email"])

    d.text((60, y + 10), title, font=font(44, bold=True), fill=0)
    y += 70

    if layout == "twocol":
        d.text((900, 250), "BILL TO:", font=font(22, bold=True), fill=0)
        d.text((900, 282), BILL_TO, font=font(21), fill=0)
        d.text((900, 350), f"Invoice #: {spec['invoice_no']}", font=font(23), fill=0)
        d.text((900, 384), f"Invoice Date: {fmt_date(spec['invoice_date'])}", font=font(23), fill=0)
        yy = 418
        if spec.get("due_date"):
            d.text((900, yy), f"Due Date: {fmt_date(spec['due_date'])}", font=font(23), fill=0)
            yy += 34
        if spec.get("due_label"):
            d.text((900, yy), f"Terms: {spec['due_label']}", font=font(23), fill=0)
        y = max(y, 470)
    else:
        d.text((60, y), f"Invoice #: {spec['invoice_no']}", font=font(23), fill=0)
        y += 34
        d.text((60, y), f"Invoice Date: {fmt_date(spec['invoice_date'])}", font=font(23), fill=0)
        y += 34
        if spec.get("due_date"):
            d.text((60, y), f"Due Date: {fmt_date(spec['due_date'])}", font=font(23), fill=0)
            y += 34
        if spec.get("due_label"):
            d.text((60, y), f"Terms: {spec['due_label']}", font=font(23), fill=0)
            y += 34
        if spec.get("po"):
            d.text((60, y), f"PO Number: {spec['po']}", font=font(23), fill=0)
            y += 34
        d.text((60, y), "BILL TO:", font=font(22, bold=True), fill=0)
        y += 30
        for line in BILL_TO.split("\n"):
            d.text((60, y), line, font=font(21), fill=0)
            y += 28
        y += 20

    y += 30
    grid = layout != "nogrid"
    items = spec["items"]

    if layout == "twopage" and len(items) > 8:
        first, rest = items[:8], items[8:]
        y = line_item_table(d, 60, y, first, grid=grid)
        d.text((60, 2100), "(continued on page 2)", font=font(20), fill=0)
        p1 = finalize_page(img)
        pages.append(p1)

        img2 = new_page()
        d2 = ImageDraw.Draw(img2)
        d2.text((60, 60), f"{spec['vendor']} - Invoice {spec['invoice_no']} (page 2)", font=font(26, bold=True), fill=0)
        y2 = 140
        y2 = line_item_table(d2, 60, y2, rest, grid=grid)
        y2 += 40
        totals_block(d2, 1120, y2, spec["subtotal"], spec["tax"], spec["total"], credit=spec.get("credit", False))
        p2 = finalize_page(img2)
        pages.append(p2)
        return pages

    y = line_item_table(d, 60, y, items, grid=grid)
    y += 40
    totals_block(d, 1120, y, spec["subtotal"], spec["tax"], spec["total"], credit=spec.get("credit", False))

    if spec.get("paid_stamp"):
        stamp_paid(img, ["PAID", "9/12"])

    pages.append(finalize_page(img))
    return pages


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def add_shadow_gradient(strip):
    w, h = strip.size
    mask = Image.new("L", (w, h))
    md = ImageDraw.Draw(mask)
    for x in range(w):
        val = 255 - int(95 * (x / w))
        md.line([(x, 0), (x, h)], fill=val)
    return ImageChops.multiply(strip, mask)


def render_receipt(store, address, phone, receipt_date, items, total,
                    receipt_no=None, shadow=False, faint=False, tax=None):
    strip_w = 900
    strip = Image.new("L", (strip_w, 1700), color=255)
    d = ImageDraw.Draw(strip)
    y = 30

    def center_text(text, size, y):
        f = mono_font(size)
        w = d.textlength(text, font=f)
        d.text(((strip_w - w) // 2, y), text, font=f, fill=0)
        return y + size + 12

    y = center_text(store.upper(), 34, y)
    y = center_text(address, 20, y)
    y = center_text(f"Tel: {phone}", 20, y)
    y += 8
    d.line([(40, y), (strip_w - 40, y)], fill=0, width=2)
    y += 20
    y = center_text(f"{receipt_date.strftime('%m/%d/%Y')} {random.randint(8, 18):02d}:{random.randint(0, 59):02d}", 20, y)
    if receipt_no:
        y = center_text(f"Receipt #{receipt_no}", 20, y)
    y += 8
    d.line([(40, y), (strip_w - 40, y)], fill=0, width=2)
    y += 24
    for desc, amt in items:
        f = mono_font(22)
        d.text((50, y), desc, font=f, fill=0)
        aw = d.textlength(money(amt), font=f)
        d.text((strip_w - 50 - aw, y), money(amt), font=f, fill=0)
        y += 34
    y += 8
    d.line([(40, y), (strip_w - 40, y)], fill=0, width=2)
    y += 24
    if tax:
        f = mono_font(22)
        for label, amt in (("SUBTOTAL", total - tax), ("TAX", tax)):
            d.text((50, y), label, font=f, fill=0)
            aw = d.textlength(money(amt), font=f)
            d.text((strip_w - 50 - aw, y), money(amt), font=f, fill=0)
            y += 34
    f = mono_font(32)
    d.text((50, y), "TOTAL", font=f, fill=0)
    tw = d.textlength(money(total), font=f)
    d.text((strip_w - 50 - tw, y), money(total), font=f, fill=0)
    y += 50
    y = center_text("VISA ****4477  APPROVED", 20, y)
    y += 16
    y = center_text("THANK YOU FOR SHOPPING WITH US", 22, y)
    y += 20

    strip = strip.crop((0, 0, strip_w, y))
    add_speckle(strip, count=500)
    if shadow:
        strip = add_shadow_gradient(strip)
    if faint:
        strip = ImageEnhance.Contrast(strip).enhance(0.45)
        strip = ImageEnhance.Brightness(strip).enhance(1.2)

    rot = random.uniform(3, 8) * random.choice([-1, 1])
    strip = strip.rotate(rot, resample=Image.BICUBIC, fillcolor=130, expand=True)

    page = Image.new("L", (PAGE_W, PAGE_H), color=95)
    px = (PAGE_W - strip.size[0]) // 2
    py = (PAGE_H - strip.size[1]) // 2
    page.paste(strip, (px, py))
    add_speckle(page, count=300)
    return page.convert("RGB")


# ---------------------------------------------------------------------------
# Statement (doc 16)
# ---------------------------------------------------------------------------

def render_statement(vendor, address, phone, statement_date, rows):
    img = new_page()
    d = ImageDraw.Draw(img)
    logo_box(d, 60, 60, vendor)
    vendor_block(d, 60, 210, vendor, address, phone, "billing@" + vendor.lower().replace(" ", "") + ".example.com")
    d.text((60, 340), "STATEMENT OF ACCOUNT", font=font(48, bold=True), fill=0)
    d.text((60, 410), f"Statement Date: {fmt_date(statement_date)}", font=font(24), fill=0)
    d.text((60, 445), f"Account: {BILL_TO.splitlines()[0]}", font=font(24), fill=0)

    y = 520
    hdr = font(23, bold=True)
    d.text((60, y), "INVOICE #", font=hdr, fill=0)
    d.text((400, y), "DATE", font=hdr, fill=0)
    d.text((650, y), "AMOUNT", font=hdr, fill=0)
    d.text((1000, y), "STATUS", font=hdr, fill=0)
    y += 40
    d.line([(60, y - 6), (1620, y - 6)], fill=0, width=2)
    running = 0.0
    for inv_no, inv_date, amt, status in rows:
        d.text((60, y), inv_no, font=font(21), fill=0)
        d.text((400, y), fmt_date(inv_date), font=font(21), fill=0)
        d.text((650, y), money(amt), font=font(21), fill=0)
        d.text((1000, y), status, font=font(21), fill=0)
        running += amt
        y += 40
    y += 30
    d.text((60, y), f"Balance Forward: {money(running)}", font=font(26, bold=True), fill=0)
    y += 60
    d.text((60, y), "This is a summary statement, not an invoice. Please remit payment", font=font(20), fill=0)
    y += 28
    d.text((60, y), "per the terms of each individual invoice listed above.", font=font(20), fill=0)
    return [finalize_page(img)]


# ---------------------------------------------------------------------------
# Vendor roster (fake)
# ---------------------------------------------------------------------------

V = {
    "northwind": dict(name="Northwind Supply Co.", address="1420 Harrow St, Denton, TX 55555",
                       phone="555-0110", email="billing@northwindsupplyco.example.com",
                       category="Office Supplies", terms="Net 30"),
    "cedarridge": dict(name="Cedar Ridge Electric", address="88 Ridge Line Rd, Marlow, OK 55555",
                        phone="555-0134", email="accounts@cedarridgeelectric.example.com",
                        category="Utilities", terms="Net 30"),
    "blueanchor": dict(name="Blue Anchor Software", address="500 Bayfront Plaza Ste 12, Port Ellis, WA 55555",
                        phone="555-0189", email="billing@blueanchorsoftware.example.com",
                        category="Software", terms="Net 30"),
    "granitepeak": dict(name="Granite Peak Consulting", address="2200 Summit Ave, Boulder Falls, CO 55555",
                         phone="555-0221", email="invoices@granitepeakconsulting.example.com",
                         category="Professional Fees", terms="Net 30"),
    "apex": dict(name="Apex Repair & Maintenance", address="77 Industrial Pkwy, Dunmore, PA 55555",
                 phone="555-0256", email="office@apexrepairmaint.example.com",
                 category="Repairs and Maintenance", terms="Net 30"),
    "fieldstone": dict(name="Fieldstone Job Materials", address="900 Quarry Rd, Millhaven, OH 55555",
                        phone="555-0278", email="sales@fieldstonejobmaterials.example.com",
                        category="Job Materials", terms="Due on receipt"),
    "silverline": dict(name="Silverline Print & Copy", address="14 Main St, Rushford, MN 55555",
                        phone="555-0293", email="orders@silverlineprintcopy.example.com",
                        category="Office Supplies", terms="Net 15"),
    "maple": dict(name="Maple Hardware", address="12 Elm St, Brookdale, MI 55555",
                  phone="555-0402", category="Job Materials"),
    "quikfuel": dict(name="Quik Fuel 12", address="Hwy 9 & Main, Brookdale, MI 55555",
                      phone="555-0455", category="Fuel"),
}

TRAILHEAD = dict(name="Trailhead Restoration Co.", address="615 Canyon View Dr, Ashcombe, AZ 55555",
                  phone="555-0317", email="info@trailheadrestoration.example.com")

EXPECTED = {}


def add_expected(fname, **kw):
    EXPECTED[fname] = kw


# ---------------------------------------------------------------------------
# Build all 20 documents
# ---------------------------------------------------------------------------

def build_doc1():
    v = V["northwind"]
    items = [
        ("Copy paper, letter size, case of 10 reams", 40, 6.25, 250.00),
        ("Toner cartridge, black (HP compatible)", 6, 85.00, 510.00),
        ("Stapler sets w/ staples", 10, 12.50, 125.00),
    ]
    subtotal, tax, total = 885.00, 70.80, 955.80
    invoice_date, due_date = dt(0), dt(30)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="INV-10441", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="grid_left")
    pages = render_invoice(spec)
    add_expected("scan_0001.pdf", type="invoice", vendor=v["name"], invoice_no="INV-10441",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc2():
    v = V["cedarridge"]
    items = [("Commercial electric service - September usage", None, None, 1842.30)]
    subtotal, tax, total = 1842.30, 0.00, 1842.30
    invoice_date, due_date = dt(-27), dt(3)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="CRE-88213", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="box_right")
    pages = render_invoice(spec)
    add_expected("scan_0002.pdf", type="invoice", vendor=v["name"], invoice_no="CRE-88213",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="DUE_SOON", needs_review="True", category=v["category"])
    return pages


def build_doc3():
    v = V["blueanchor"]
    items = [
        ("Annual subscription - Team plan", 1, 1200.00, 1200.00),
        ("Add-on seats", 5, 40.00, 200.00),
    ]
    subtotal, tax, total = 1400.00, 0.00, 1400.00
    invoice_date, due_date = dt(0), dt(25)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="BAS-5521", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="twocol")
    pages = render_invoice(spec)
    add_expected("scan_0003.pdf", type="invoice", vendor=v["name"], invoice_no="BAS-5521",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc4():
    v = V["granitepeak"]
    items = [(f"Consulting services - week {i+1}", 1, 375.00, 375.00) for i in range(12)]
    subtotal, tax, total = 4500.00, 0.00, 4500.00
    invoice_date, due_date = dt(0), dt(40)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="GPC-30045", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="twopage")
    pages = render_invoice(spec)
    add_expected("scan_0004.pdf", type="invoice", vendor=v["name"], invoice_no="GPC-30045",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc5():
    v = V["apex"]
    items = [
        ("HVAC filter replacement", 1, 145.00, 145.00),
        ("Labor", 3, 95.00, 285.00),
        ("Service call fee", 1, 75.00, 75.00),
    ]
    subtotal, tax, total = 505.00, 40.40, 545.40
    invoice_date, due_date = dt(0), dt(35)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="ARM-9987", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], po="PO-77321", items=items, subtotal=subtotal, tax=tax,
                total=total, layout="grid_left")
    pages = render_invoice(spec)
    add_expected("scan_0005.pdf", type="invoice", vendor=v["name"], invoice_no="ARM-9987",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc6():
    v = V["fieldstone"]
    items = [
        ("Lumber, 2x4x8 studs", 100, 4.75, 475.00),
        ("Concrete mix, 60lb bags", 30, 6.20, 186.00),
        ("Rebar bundles", 8, 32.50, 260.00),
    ]
    subtotal, tax, total = 921.00, 73.68, 994.68
    invoice_date = dt(0)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="FJM-2210", invoice_date=invoice_date, due_date=None,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="nogrid")
    pages = render_invoice(spec)
    add_expected("scan_0006.pdf", type="invoice", vendor=v["name"], invoice_no="FJM-2210",
                 date=fmt_date(invoice_date), due="", total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc7():
    v = V["silverline"]
    items = [
        ("Business card printing, 1000 count", 1, 85.00, 85.00),
        ("Brochure printing, 500 count", 1, 210.00, 210.00),
        ("Binding services", 1, 45.00, 45.00),
    ]
    subtotal, tax, total = 340.00, 27.20, 367.20
    invoice_date = dt(0)
    due_date = invoice_date + timedelta(days=15)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="SPC-6634", invoice_date=invoice_date, due_date=None,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="grid_left")
    pages = render_invoice(spec)
    add_expected("scan_0007.pdf", type="invoice", vendor=v["name"], invoice_no="SPC-6634",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return pages


def build_doc8():
    v = TRAILHEAD
    items = [
        ("Water damage assessment", 1, 350.00, 350.00),
        ("Drywall repair, 120 sq ft", 1, 840.00, 840.00),
        ("Paint and finish work", 1, 310.00, 310.00),
    ]
    subtotal, tax, total = 1500.00, 120.00, 1620.00
    invoice_date, due_date = dt(0), dt(28)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="TRC-1187", invoice_date=invoice_date, due_date=due_date,
                due_label="Net 30", items=items, subtotal=subtotal, tax=tax, total=total,
                layout="nogrid")
    pages = render_invoice(spec)
    add_expected("scan_0008.pdf", type="invoice", vendor=v["name"], invoice_no="TRC-1187",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="UNKNOWN_VENDOR", needs_review="True", category="")
    return pages


def build_doc9():
    v = V["maple"]
    items = [("Hand tools, assorted", 28.50), ("Wood screws, box", 9.67)]
    total = 42.17
    rdate = dt(-2)
    page = render_receipt(v["name"], v["address"], v["phone"], rdate, items, total,
                           receipt_no="R-88214", shadow=True, tax=4.00)
    add_expected("scan_0009.pdf", type="receipt", vendor=v["name"], invoice_no="R-88214",
                 date=fmt_date(rdate), due="", total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return [page]


def build_doc10():
    v = V["quikfuel"]
    items = [("Unleaded fuel, 14.2 gal", 58.40)]
    total = 58.40
    rdate = dt(-1)
    page = render_receipt(v["name"], v["address"], v["phone"], rdate, items, total)
    add_expected("scan_0010.pdf", type="receipt", vendor=v["name"], invoice_no="",
                 date=fmt_date(rdate), due="", total=exp_total(total),
                 flags="", needs_review="False", category=v["category"])
    return [page]


def build_doc11():
    name, address, phone = "Daily Grind Coffee", "5 Market Sq, Brookdale, MI 55555", "555-0488"
    items = [("Drip coffee, large", 3.25), ("Blueberry muffin", 3.50)]
    total = 6.75
    rdate = dt(-3)
    page = render_receipt(name, address, phone, rdate, items, total, faint=True)
    add_expected("scan_0011.pdf", type="receipt", vendor=name, invoice_no="",
                 date=fmt_date(rdate), due="", total=exp_total(total),
                 flags="UNKNOWN_VENDOR", needs_review="True", category="")
    return [page]


def build_doc12():
    name, address, phone = "Corner Market Mart", "900 5th Ave, Brookdale, MI 55555", "555-0512"
    items = [("Bottled water, 24pk", 5.99), ("Snack assortment", 12.49),
             ("Paper towels", 5.10)]
    total = 23.58
    rdate = dt(-1)
    page = render_receipt(name, address, phone, rdate, items, total, receipt_no="0004417")
    add_expected("scan_0012.pdf", type="receipt", vendor=name, invoice_no="0004417",
                 date=fmt_date(rdate), due="", total=exp_total(total),
                 flags="UNKNOWN_VENDOR", needs_review="True", category="")
    return [page]


def build_doc14():
    v = V["blueanchor"]
    items = [
        ("Annual subscription - Team plan", 1, 1200.00, 1200.00),
        ("Add-on seats", 5, 40.00, 200.00),
    ]
    subtotal, tax, total = 1400.00, 0.00, 1400.00
    invoice_date, due_date = dt(2), dt(27)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="BAS-5588", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="twocol")
    pages = render_invoice(spec)
    add_expected("scan_0014.pdf", type="invoice", vendor=v["name"], invoice_no="BAS-5588",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="DUPLICATE", needs_review="True", category=v["category"])
    return pages


def build_doc15():
    v = V["northwind"]
    items = [("Return: Toner cartridge, black (HP compatible) x2", 2, 85.00, 170.00),
             ("Restocking adjustment", None, None, 10.00)]
    subtotal, tax, total = 180.00, 0.00, 180.00
    invoice_date = dt(0)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="CM-3390", invoice_date=invoice_date, due_date=None, due_label=None,
                items=items, subtotal=subtotal, tax=tax, total=total, layout="grid_left",
                credit=True, doc_title="CREDIT MEMO")
    pages = render_invoice(spec)
    add_expected("scan_0015.pdf", type="credit_memo", vendor=v["name"], invoice_no="CM-3390",
                 date=fmt_date(invoice_date), due="", total=exp_total(-total),
                 flags="CREDIT_MEMO", needs_review="True", category=v["category"])
    return pages


def build_doc16():
    v = V["cedarridge"]
    statement_date = dt(0)
    rows = [
        ("CRE-87920", dt(-95), 210.40, "Paid"),
        ("CRE-88011", dt(-64), 198.75, "Paid"),
        ("CRE-88213", dt(-27), 1842.30, "Open"),
    ]
    pages = render_statement(v["name"], v["address"], v["phone"], statement_date, rows)
    add_expected("scan_0016.pdf", type="statement", vendor=v["name"],
                 date=fmt_date(statement_date), flags="NOT_INVOICE", needs_review="True",
                 category=v["category"])
    return pages


def build_doc17():
    v = V["apex"]
    # printed subtotal is 9.00 MORE than the true sum of the line items
    items = [("Replacement parts", None, None, 120.00), ("Labor", None, None, 180.00)]
    printed_subtotal, tax, total = 309.00, 24.72, 333.72
    invoice_date, due_date = dt(0), dt(30)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="ARM-9991", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=printed_subtotal, tax=tax,
                total=total, layout="grid_left")
    pages = render_invoice(spec)
    add_expected("scan_0017.pdf", type="invoice", vendor=v["name"], invoice_no="ARM-9991",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="MATH_ERROR", needs_review="True", category=v["category"])
    return pages


def build_doc18():
    v = V["cedarridge"]
    items = [("Commercial electric service - prior period", None, None, 210.00)]
    subtotal, tax, total = 210.00, 0.00, 210.00
    invoice_date, due_date = dt(-45), dt(-15)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="CRE-88250", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="box_right", paid_stamp=True)
    pages = render_invoice(spec)
    add_expected("scan_0018.pdf", type="invoice", vendor=v["name"], invoice_no="CRE-88250",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="MARKED_PAID", needs_review="True", category=v["category"])
    return pages


def build_doc19():
    v = V["silverline"]
    items = [
        ("Business card printing, 500 count", 1, 55.00, 55.00),
        ("Poster printing, large format", 1, 345.00, 345.00),
    ]
    subtotal, tax, total = 400.00, 32.00, 432.00
    invoice_date, due_date = dt(-40), dt(-10)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="SPC-6650", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="grid_left")
    pages = render_invoice(spec)
    add_expected("scan_0019.pdf", type="invoice", vendor=v["name"], invoice_no="SPC-6650",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="OVERDUE", needs_review="True", category=v["category"])
    return pages


def build_doc20():
    v = V["northwind"]
    items = [("Warehouse shelving units", 2, 305.00, 610.00)]
    subtotal, tax, total = 610.00, 0.00, 610.00
    invoice_date, due_date = dt(0), dt(30)
    spec = dict(vendor=v["name"], address=v["address"], phone=v["phone"], email=v["email"],
                invoice_no="INV-10399", invoice_date=invoice_date, due_date=due_date,
                due_label=v["terms"], items=items, subtotal=subtotal, tax=tax, total=total,
                layout="box_right")
    pages = render_invoice(spec)
    add_expected("scan_0020.pdf", type="invoice", vendor=v["name"], invoice_no="INV-10399",
                 date=fmt_date(invoice_date), due=fmt_date(due_date), total=exp_total(total),
                 flags="DUPLICATE", needs_review="True", category=v["category"])
    return pages


# ---------------------------------------------------------------------------
# vendors.csv / ledger.csv
# ---------------------------------------------------------------------------

def write_vendors_csv():
    path = os.path.join(HERE, "vendors.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vendor_name", "default_category", "default_terms", "notes"])
        for key in ("northwind", "cedarridge", "blueanchor", "granitepeak", "apex",
                    "fieldstone", "silverline", "maple", "quikfuel"):
            v = V[key]
            w.writerow([v["name"], v["category"], v.get("terms", ""), ""])
    return path


def write_ledger_csv():
    path = os.path.join(HERE, "ledger.csv")
    rows = [
        (V["northwind"]["name"], "INV-10399", fmt_date(dt(-120)), "610.00", "old_scan_014.pdf"),
        (V["apex"]["name"], "ARM-9820", fmt_date(dt(-150)), "275.50", "old_scan_015.pdf"),
        (V["granitepeak"]["name"], "GPC-29500", fmt_date(dt(-90)), "890.00", "old_scan_016.pdf"),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vendor", "invoice_no", "date", "total", "source_file"])
        for row in rows:
            w.writerow(row)
    return path


# ---------------------------------------------------------------------------
# main / selftest
# ---------------------------------------------------------------------------

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for fn in os.listdir(OUT_DIR):
        if fn.upper().endswith(".PDF"):
            os.remove(os.path.join(OUT_DIR, fn))
    EXPECTED.clear()

    doc1_pages = build_doc1()
    save_pdf(doc1_pages, "scan_0001.pdf")
    save_pdf(build_doc2(), "scan_0002.pdf")
    doc3_pages = build_doc3()
    save_pdf(doc3_pages, "scan_0003.pdf")
    save_pdf(build_doc4(), "scan_0004.pdf")
    save_pdf(build_doc5(), "scan_0005.pdf")
    save_pdf(build_doc6(), "scan_0006.pdf")
    save_pdf(build_doc7(), "scan_0007.pdf")
    save_pdf(build_doc8(), "scan_0008.pdf")
    save_pdf(build_doc9(), "scan_0009.pdf")
    save_pdf(build_doc10(), "scan_0010.pdf")
    save_pdf(build_doc11(), "scan_0011.pdf")
    save_pdf(build_doc12(), "scan_0012.pdf")

    # doc 13: pixel-identical re-render of doc 1 -- reuse the SAME rendered
    # pages (not a fresh call, which would consume RNG state differently)
    save_pdf(doc1_pages, "scan_0013.pdf")
    add_expected("scan_0013.pdf", **{**EXPECTED["scan_0001.pdf"], "flags": "DUPLICATE",
                                      "needs_review": "True"})

    save_pdf(build_doc14(), "scan_0014.pdf")
    save_pdf(build_doc15(), "scan_0015.pdf")
    save_pdf(build_doc16(), "scan_0016.pdf")
    save_pdf(build_doc17(), "scan_0017.pdf")
    save_pdf(build_doc18(), "scan_0018.pdf")
    save_pdf(build_doc19(), "scan_0019.pdf")
    save_pdf(build_doc20(), "scan_0020.pdf")

    write_vendors_csv()
    write_ledger_csv()

    with open(os.path.join(HERE, "expected.json"), "w", encoding="utf-8") as f:
        json.dump(EXPECTED, f, indent=2)

    pdfs = sorted(fn for fn in os.listdir(OUT_DIR) if fn.upper().endswith(".PDF"))
    print(f"Wrote {len(pdfs)} PDFs to {OUT_DIR}")
    for fn in pdfs:
        print(" ", fn)
    print(f"Wrote {os.path.join(HERE, 'expected.json')} ({len(EXPECTED)} entries)")
    print(f"Wrote {os.path.join(HERE, 'vendors.csv')}")
    print(f"Wrote {os.path.join(HERE, 'ledger.csv')}")


def demo():
    """ponytail: smallest runnable check -- render doc 1 and confirm a real
    multi-page-capable PDF comes out, and that money()/exp_total() agree."""
    os.makedirs(OUT_DIR, exist_ok=True)
    pages = build_doc1()
    assert len(pages) >= 1
    assert pages[0].size == (PAGE_W, PAGE_H)
    tmp = os.path.join(OUT_DIR, "_selftest.pdf")
    pages[0].save(tmp, "PDF", resolution=DPI)
    assert os.path.getsize(tmp) > 1000
    os.remove(tmp)
    assert money(1234.5) == "$1,234.50"
    assert exp_total(-180.0) == "-180.00"
    assert EXPECTED["scan_0001.pdf"]["flags"] == ""
    print("demo() self-check passed")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        main()
