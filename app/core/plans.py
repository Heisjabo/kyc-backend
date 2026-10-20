"""The plans a client can be on, and what each one may use.

The same terms the pricing page shows (pangolinkyc-landing, lib/pricing.ts and
components/pricing/plans.tsx): change them together. They live in code rather
than in a table so that the page and the rules move in one release, and so a
plan's features can be read in review rather than looked up in a database.

A feature named here that does not exist yet costs nothing: it is listed so that
the day it is built, which plans may use it is already decided.
"""

PLANS = ("free", "payg", "enterprise")
PRODUCTS = ("kyc", "sign")

# What a new account is given, once. Not a monthly allowance: a free grant that
# has been used up is never refilled.
FREE_GRANTS = {"kyc": 100, "sign": 100}

# Pay as you go: one verification or one Sign request, in US cents.
STANDARD_UNIT_CENTS = 100
# Top-ups are bought in blocks of at least this many, so the smallest is $200.
MIN_TOP_UP_UNITS = 200

_FREE = {
    "document",
    "face",
    "deepfake_detection",
    "child_verification",
    "e_signature",
}
_PAYG = _FREE | {
    "aml",
    "know_your_business",
    "face_authentication",
    "address",
    "age_verification",
    "email_mfa",
    "due_diligence_form",
    "fast_id",
    "signer_identity_verification",
}
_ENTERPRISE = _PAYG | {
    "phone_mfa",
    "aml_monitoring",
    "business_aml",
    "aml_bulk",
    "risk_assessment",
    "consent_verification",
    "blocklist",
    "branding",
}

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
    "consent": "consent_verification",
}
