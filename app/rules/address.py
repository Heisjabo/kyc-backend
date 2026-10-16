"""Thresholds and vocabulary for the address check. Tune against real samples."""

# What counts as a proof of address, matching the supported_types we send Shufti
# (tuesday-backend/src/shufti/shufti.service.ts). A driving licence is not offered
# unless the client asks for it in address_types: only a few countries print an
# address on one.
PROOF_TYPES = ("utility_bill", "bank_statement", "credit_card_statement", "rent_agreement")

# How recent the proof has to be, in days. Shufti is sent document_validity: 90,
# so a client moving between us and them sees the same answer.
MAX_PROOF_AGE_DAYS = 90

# Words that say what kind of document this is. A proof that names none of them
# is not refused, only sent to a person: a small local bill can look like nothing
# on any list.
TYPE_WORDS = {
    "utility_bill": (
        "electricity", "electric", "power", "energy", "water", "gas", "utility",
        "meter", "kwh", "units", "reg", "eucl", "wasac", "kplc", "umucyo",
    ),
    "bank_statement": (
        "bank", "statement", "account", "balance", "iban", "swift", "debit",
        "credit", "deposit", "withdrawal", "opening balance", "closing balance",
    ),
    "credit_card_statement": (
        "credit card", "card statement", "minimum payment", "payment due",
        "statement", "visa", "mastercard", "card number", "credit limit",
    ),
    "rent_agreement": (
        "lease", "tenancy", "tenant", "landlord", "rent", "rental", "lessor",
        "lessee", "premises", "agreement",
    ),
    "driving_license": (
        "driving licence", "driving license", "driver license", "driver's license",
        "drivers license", "driver licence", "permis de conduire", "endorsements",
        "restrictions", "dmv", "dvla", "motor vehicle",
    ),
}

# Uploads. Only a PDF: a screenshot or a photo of a bill is the easiest kind of
# proof to alter, and the statement or bill itself is almost always a PDF.
ALLOWED_CONTENT_TYPES = {"application/pdf"}

# How many pages of a PDF are read. A statement's address is on the first page;
# this is enough for a covering page in front of it without reading somebody's
# whole year of transactions.
PDF_MAX_PAGES = 3

# Rendering scale for a PDF with no text in it, where pdfium's 1.0 is 72 dpi. 2.8
# puts the print at about 200 dpi, which is the resolution the OCR reads
# photographs of documents at.
PDF_RENDER_SCALE = 2.8

# How many lines of text a page has to give up before it is worth judging. A bill
# or a statement is a page of print; anything less than this is a photograph of
# something else, or a photograph too poor to read.
MIN_TEXT_LINES = 5

# A PO box is not an address anybody lives at. Our Shufti request says
# po_box: false, so this refuses the same thing.
PO_BOX_WORDS = ("po box", "p o box", "p.o box", "p.o. box", "post office box", "postbox")
