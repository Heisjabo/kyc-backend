"""Reading places out of an address, and out of a page of text.

Both sides of the address check go through here, so a claimed address and a
utility bill are reduced the same way before they are compared. What comes out is
a set of places at three levels: street, city, country.

The expansion is what makes the comparison work in practice. "KG 586 St" names no
city, but its prefix says Kigali; "Kigali" names no country, but it is in Rwanda.
Without that, an address and a bill that plainly describe the same place would
share no text at all.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from app.rules import address as rules
from app.rules import places


@dataclass
class Places:
    """The places one piece of text names, once reduced."""

    streets: set[str] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)
    countries: set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (self.streets or self.cities or self.countries)

    def at(self, level: str) -> set[str]:
        return {
            places.LEVEL_STREET: self.streets,
            places.LEVEL_CITY: self.cities,
            places.LEVEL_COUNTRY: self.countries,
        }[level]


def normalise(text: str) -> str:
    """Lower case, no accents, no punctuation, single spaces."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", ascii_text)).strip().lower()


def is_po_box(text: str) -> bool:
    flat = normalise(text).replace(" ", "")
    return any(word.replace(" ", "").replace(".", "") in flat for word in rules.PO_BOX_WORDS)


def street_codes(text: str) -> set[str]:
    """Rwandan street codes: KG 586 St, KK 15 Ave, and the unspaced KG586."""
    found = set()
    for prefix, number in re.findall(r"\b(kg|kk|kn)\s?(\d{1,4})\b", normalise(text)):
        found.add(f"{prefix} {number}")
    return found


def read_places(text: str) -> Places:
    """Everything `text` says about where somebody is, expanded.

    Works on one address line and on a whole page of OCR text alike: the bill is
    just a longer piece of text with more places in it.
    """
    flat = normalise(text)
    found = Places()

    for code in street_codes(flat):
        found.streets.add(code)
        city, country = places.STREET_CODE_CITIES[code.split()[0]]
        found.cities.add(city)
        found.countries.add(country)

    # Multi-word countries ("united states", "south africa") before single tokens,
    # so "united states of america" is one country and not three unknown words.
    for alias, country in places.COUNTRY_ALIASES.items():
        if " " in alias and alias in flat:
            found.countries.add(country)

    tokens = [token for token in flat.split() if token not in rules.FILLER_WORDS]
    for token in tokens:
        if token in places.COUNTRY_ALIASES:
            found.countries.add(places.COUNTRY_ALIASES[token])
            continue
        if token in places.CITY_REGIONS:
            found.cities.add(token)
            # A district sits inside a city, which sits inside a country. Each
            # parent goes to the level it belongs to: Kicukiro is in Kigali, and
            # Kigali is not a country.
            for parent in places.CITY_REGIONS[token]:
                if parent in places.COUNTRY_ALIASES:
                    found.countries.add(places.COUNTRY_ALIASES[parent])
                else:
                    found.cities.add(parent)
            continue
        if token in places.US_STATES:
            found.cities.add(token)
            found.countries.add("united states")
            continue
        if len(token) >= rules.MIN_TOKEN_LENGTH and not token.isdigit():
            # An unrecognised word long enough to be a place name: a street, a
            # sector, a town nobody has put in the table yet.
            found.streets.add(token)

    # Anything that turned out to be a city is not also a street name.
    found.streets -= found.cities
    return found
