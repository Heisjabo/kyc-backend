"""The plans a client can be on, and what each one may use.

The same terms the pricing page shows (the landing site's pricing page): change them together. They live in code rather
than in a table so that the page and the rules move in one release, and so a
plan's features can be read in review rather than looked up in a database.

Only features the product has today are named here. Add one to its plan when
it is built, and to the pricing page in the same release.
"""

PLANS = ("free", "payg", "enterprise")
PRODUCTS = ("kyc",)

# What a new account is given, once. Not a monthly allowance: a free grant that
# has been used up is never refilled.
FREE_GRANTS = {"kyc": 100}

# Pay as you go: one verification, in US cents.
STANDARD_UNIT_CENTS = 100
# Top-ups are bought in blocks of at least this many, so the smallest is $200.
MIN_TOP_UP_UNITS = 200

_FREE = {
    "document",
    "face",
    "deepfake_detection",
    "child_verification",
}
_PAYG = _FREE | {
    "aml",
    "address",
    "age_verification",
    "due_diligence_form",
}
# Enterprise has no features of its own yet: it differs in its terms (an agreed
# rate, overdraft), which live on the account.
_ENTERPRISE = _PAYG

PLAN_FEATURES: dict[str, frozenset[str]] = {
    "free": frozenset(_FREE),
    "payg": frozenset(_PAYG),
    "enterprise": frozenset(_ENTERPRISE),
}

# The feature behind each verification service a client can ask for.
SERVICE_FEATURES = {
    "document": "document",
    "face": "face",
    "address": "address",
    "due_diligence": "due_diligence_form",
    "aml": "aml",
}
