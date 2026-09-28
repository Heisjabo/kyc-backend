"""Comparing an address with what a document says.

The comparison is **direct**: the address the client gave us has to appear on the
document as it was written. Case, spacing and punctuation are ignored, because
those are how text was printed rather than what it says, and the document's lines
are joined before searching so an address broken across two lines still reads as
one. Nothing else is forgiven: no abbreviations folded together, no street code
taken to mean the city it sits in, no partial credit for sharing a word.

So "Kigali, Rwanda" is not matched by a bill that prints "KG 333 St", and not by
one that prints "Kigali City" either. If the two do not say the same thing, the
check fails and a person is told which address we were given.
"""

import re
import unicodedata

from app.rules import address as rules


def normalise(text: str) -> str:
    """Lower case, no accents, no punctuation, single spaces.

    This is the only thing forgiven, and it is not a difference in the address: it
    is a difference in how the address was typed or printed.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", ascii_text)).strip().lower()


def is_po_box(text: str) -> bool:
    flat = normalise(text).replace(" ", "")
    return any(word.replace(" ", "").replace(".", "") in flat for word in rules.PO_BOX_WORDS)


def document_text(lines: list[str]) -> str:
    """Every line of the document as one normalised string.

    Joined, because an address is routinely printed across two or three lines and
    a line break is not a difference in the address.
    """
    return normalise(" ".join(lines))


def address_on_document(claimed: str, lines: list[str]) -> bool:
    """Does the document say this address, as it was given to us?"""
    wanted = normalise(claimed)
    return bool(wanted) and wanted in document_text(lines)
