"""Thresholds and vocabulary for the address check. Tune against real samples."""

# What counts as a proof of address, matching the supported_types we send Shufti
# (tuesday-backend/src/shufti/shufti.service.ts). A driving licence is not on the
# list: only a few countries print an address on one, and ours does not.
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
}

# How many lines of text a page has to give up before it is worth judging. A bill
# or a statement is a page of print; anything less than this is a photograph of
# something else, or a photograph too poor to read.
MIN_TEXT_LINES = 5

# A PO box is not an address anybody lives at. Our Shufti request says
# po_box: false, so this refuses the same thing.
PO_BOX_WORDS = ("po box", "p o box", "p.o box", "p.o. box", "post office box", "postbox")

# Words that carry no place in them. Dropped from both sides before matching, so
# "KG 586 Street" and "KG 586 St" compare as the same thing.
FILLER_WORDS = {
    "street", "st", "avenue", "ave", "road", "rd", "lane", "ln", "drive", "dr",
    "boulevard", "blvd", "highway", "hwy", "close", "way", "plot", "house",
    "apartment", "apartments", "apt", "flat", "floor", "suite", "unit", "room",
    "building", "block", "city", "town", "village", "cell", "sector", "district",
    "province", "county", "state", "zone", "number", "no", "nr", "of", "the",
    "and", "po", "box", "zip", "code", "postal", "postcode",
}

# A token shorter than this says nothing on its own: "KG" is a street prefix, not
# a place, and OCR noise is mostly one or two characters.
MIN_TOKEN_LENGTH = 3

# How close two place names have to read to count as the same place, as a
# rapidfuzz ratio out of 100. "Kigali" against OCR's "Kigah" is the case this is
# for; it is deliberately tighter than the name matcher, because place names are
# short and a loose line would match "Kigali" to "Kicali" in another country.
PLACE_MATCH_SCORE = 88
