"""Read identity fields from the visible text of any document (no MRZ needed).

Works on OCR lines, not on images, so it is fully testable with plain strings.
Labels cover English, French, Kinyarwanda and Swahili. Extend the tables below
as new document layouts show up.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from app.engines.country_codes import ALPHA2_TO_ALPHA3, ALPHA3_CODES
from app.engines.country_names import COUNTRY_NAMES

# Words that identify the kind of document. Order matters only for readability.
DOCUMENT_TYPE_KEYWORDS = {
    "passport": ["PASSPORT", "PASSEPORT", "PASIPOTI", "PASAPORTE", "PASSAPORTE"],
    "driving_license": [
        "DRIVING LICENCE", "DRIVING LICENSE", "DRIVER LICENCE", "DRIVER LICENSE", "DRIVERS LICENSE",
        "DRIVER'S LICENSE", "DRIVER'S LICENCE", "PERMIS DE CONDUIRE", "URUHUSHYA RWO GUTWARA",
        "LESENI YA UDEREVA", "LESENI YA KUENDESHA",
    ],
    "id_card": [
        "NATIONAL IDENTITY", "IDENTITY CARD", "NATIONAL ID", "ID CARD", "INDANGAMUNTU",
        "CARTE D'IDENTITE", "CARTE NATIONALE D'IDENTITE", "KITAMBULISHO", "GHANA CARD",
        "NATIONAL IDENTIFICATION", "IDENTITY DOCUMENT",
    ],
}

# Known national ID number shapes (digits only, spaces removed before matching).
ID_NUMBER_PATTERNS = {
    "RWA": re.compile(r"^1\d{15}$"),       # 16 digits, starts with 1
    "KEN": re.compile(r"^\d{7,9}$"),
    "NGA": re.compile(r"^\d{11}$"),        # NIN
    "TZA": re.compile(r"^\d{20}$"),        # NIDA
    "UGA": re.compile(r"^C[MF][A-Z0-9]{12}$"),
    "GHA": re.compile(r"^GHA\d{10}$"),
}

LABELS = {
    "date_of_birth": ["DATE OF BIRTH", "BIRTH", "D.O.B", "DOB", "NE LE", "NEE LE", "NAISSANCE",
                      "YAVUKIYEHO", "AMAVUKO", "KUZALIWA", "BORN"],
    "expiry_date": ["DATE OF EXPIRY", "EXPIRY", "EXPIRES", "EXPIRATION", "VALID UNTIL", "VALID TO",
                    "VALABLE JUSQU", "EXP", "IRANGIRA", "KUISHA"],
    "issue_date": ["DATE OF ISSUE", "ISSUE DATE", "ISSUED", "DELIVRE", "DELIVRANCE", "YATANZWE", "KUTOLEWA"],
    "document_number": ["ID NUMBER", "ID NO", "NATIONAL ID NO", "IDENTITY CARD NO", "IDENTITY NUMBER",
                        "NATIONAL IDENTIFICATION NUMBER", "NIN", "LICENCE NO", "LICENSE NO",
                        "LICENCE NUMBER", "LICENSE NUMBER", "DL NO", "DOCUMENT NO", "DOCUMENT NUMBER",
                        "CARD NO", "CARD NUMBER", "PASSPORT NO", "PERSONAL NO", "PERSONAL NUMBER",
                        "SERIAL NO", "SERIAL NUMBER", "NUMERO", "NUMBER", "NO."],
    "surname": ["SURNAME", "LAST NAME", "FAMILY NAME", "NOM DE FAMILLE", "NOM"],
    "given_names": ["GIVEN NAMES", "GIVEN NAME", "FIRST NAME", "FIRST NAMES", "PRENOMS", "PRENOM", "OTHER NAMES"],
    "full_name": ["FULL NAME", "NAMES", "NAME", "AMAZINA", "JINA"],
    "sex": ["SEX", "GENDER", "SEXE", "IGITSINA", "JINSIA"],
}

# Words that mark a line as the document's heading, where the country is printed.
HEADING_WORDS = [
    "REPUBLIC", "REPUBLIQUE", "REPUBULIKA", "REPUBLIK", "REPUBBLICA", "REPUBLICA", "JAMHURI",
    "KINGDOM", "ROYAUME", "STATE OF", "UNITED", "FEDERAL", "GOVERNMENT", "MINISTRY", "NATION",
    "PASSPORT", "PASSEPORT", "IDENTITY", "INDANGAMUNTU", "KITAMBULISHO", "LICENCE", "LICENSE",
    "CARD", "CARTE", "EMBASSY", "AUTHORITY", "BUREAU",
]

_MONTHS = {
    "JAN": 1, "FEB": 2, "FEV": 2, "MAR": 3, "APR": 4, "AVR": 4, "MAY": 5, "MAI": 5, "JUN": 6, "JUI": 7,
    "JUL": 7, "AUG": 8, "AOU": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_DMY = re.compile(r"\b(\d{1,2})\s?[./\-]\s?(\d{1,2})\s?[./\-]\s?(\d{4})\b")
_YMD = re.compile(r"\b(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})\b")
_D_MON_Y = re.compile(r"\b(\d{1,2})[\s./\-]*([A-Z]{3,9})(?:\s*/\s*[A-Z]{3,9})?[\s./\-]*(\d{4}|\d{2})\b")


@dataclass
class VisualFields:
    document_types: list[str] = field(default_factory=list)
    country: str | None = None
    surname: str | None = None
    given_names: str | None = None
    full_name: str | None = None
    date_of_birth: date | None = None
    expiry_date: date | None = None
    issue_date: date | None = None
    document_number: str | None = None
    sex: str | None = None
    all_dates: list[date] = field(default_factory=list)


def normalize(text: str) -> str:
    """Uppercase, strip accents, collapse spaces. 'Née le' -> 'NEE LE'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = ascii_text.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", ascii_text.upper()).strip()


def find_dates(text: str, today: date) -> list[date]:
    text = normalize(text)
    found = []
    for match in _YMD.finditer(text):
        found.append(_safe_date(int(match[1]), int(match[2]), int(match[3])))
    for match in _DMY.finditer(text):
        day, month, year = int(match[1]), int(match[2]), int(match[3])
        # African documents write day first; fall back to month first only if impossible.
        found.append(_safe_date(year, month, day) or _safe_date(year, day, month))
    for match in _D_MON_Y.finditer(text):
        month = _MONTHS.get(match[2][:3])
        if month:
            found.append(_safe_date(_full_year(match[3], today), month, int(match[1])))
    return [d for d in found if d]


def _full_year(value: str, today: date) -> int:
    if len(value) == 4:
        return int(value)
    year = 2000 + int(value)
    return year - 100 if year > today.year + 20 else year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day) if 1900 <= year <= 2100 else None
    except ValueError:
        return None


def _has_label(line: str, labels: list[str]) -> bool:
    return any(re.search(rf"(?<![A-Z]){re.escape(label)}(?![A-Z])", line) for label in labels)


def _value_after_label(lines: list[str], index: int, labels: list[str]) -> list[str]:
    """Candidate values for a label: the rest of its own line, then the next two lines."""
    line = lines[index]
    rest = line
    for label in labels:
        rest = re.sub(rf"(?<![A-Z]){re.escape(label)}(?![A-Z])", " ", rest)
    rest = re.sub(r"^[\s:/.\-]+", "", rest).strip()
    return [rest, *lines[index + 1:index + 3]]


def detect_document_types(lines: list[str]) -> list[str]:
    text = " ".join(normalize(line) for line in lines)
    return [doc_type for doc_type, words in DOCUMENT_TYPE_KEYWORDS.items() if any(word in text for word in words)]


def detect_country(lines: list[str]) -> str | None:
    """Find the issuing country in the printed text.

    A country name only counts inside a heading ("REPUBLIC OF KENYA", "REPUBULIKA
    Y'U RWANDA") or on a line of its own. Otherwise a person named Jordan or Chad
    would set the country, since several countries are also given names.
    """
    name_labels = LABELS["surname"] + LABELS["given_names"] + LABELS["full_name"]
    for index, raw_line in enumerate(lines):
        line = normalize(raw_line)
        # A line right after a name label holds a person's name, not a country.
        if index and _has_label(normalize(lines[index - 1]), name_labels):
            continue
        heading = any(word in line for word in HEADING_WORDS)
        for name, code in COUNTRY_NAMES:
            if re.search(rf"(?<![A-Z]){re.escape(name)}(?![A-Z])", line):
                letters = len(re.sub(r"[^A-Z]", "", line))
                if heading or len(name.replace(" ", "")) >= letters * 0.9:
                    return code
                break
    return None


def extract_visual_fields(raw_lines: list[str], today: date) -> VisualFields:
    # MRZ lines are handled separately and would only add noise here.
    lines = [normalize(line) for line in raw_lines if "<<" not in line]
    fields = VisualFields(document_types=detect_document_types(lines), country=detect_country(lines))

    for line in lines:
        fields.all_dates.extend(find_dates(line, today))
    date_digits = {re.sub(r"[^0-9]", "", line) for line in lines if find_dates(line, today)}

    for index, line in enumerate(lines):
        for name in ("date_of_birth", "expiry_date", "issue_date"):
            if getattr(fields, name) is None and _has_label(line, LABELS[name]):
                for candidate in _value_after_label(lines, index, LABELS[name]):
                    dates = find_dates(candidate, today)
                    if dates:
                        setattr(fields, name, dates[0])
                        break

        if fields.sex is None and _has_label(line, LABELS["sex"]):
            for candidate in _value_after_label(lines, index, LABELS["sex"]):
                sex = _parse_sex(candidate)
                if sex:
                    fields.sex = sex
                    break

        if fields.document_number is None and _has_label(line, LABELS["document_number"]):
            for candidate in _value_after_label(lines, index, LABELS["document_number"]):
                number = _parse_document_number(candidate, fields.country, date_digits)
                if number:
                    fields.document_number = number
                    break

        for name in ("surname", "given_names", "full_name"):
            if getattr(fields, name) is None and _has_label(line, LABELS[name]):
                for candidate in _value_after_label(lines, index, LABELS[name]):
                    value = _parse_name(candidate)
                    if value:
                        setattr(fields, name, value)
                        break

    if fields.document_number is None:
        fields.document_number = _find_known_id_number(lines, fields.country)

    # Without a readable label, fall back on what the dates themselves imply.
    if fields.date_of_birth is None:
        past = [d for d in fields.all_dates if 16 <= (today - d).days / 365.25 <= 110]
        fields.date_of_birth = min(past) if past else None
    if fields.expiry_date is None:
        future = [d for d in fields.all_dates if d > today]
        fields.expiry_date = max(future) if future else None
    return fields


def _parse_sex(text: str) -> str | None:
    tokens = set(re.findall(r"[A-Z]+", text))
    if tokens & {"M", "MALE", "MASCULIN", "GABO", "ME"}:
        return "M"
    if tokens & {"F", "FEMALE", "FEMININ", "GORE", "KE"}:
        return "F"
    return None


def _number_candidates(text: str) -> list[str]:
    """Ways a document number can appear on one line.

    Numbers are printed in groups ("1 1995 8 0012345 0 12"), with separators
    ("GHA-123456789-0"), or next to other words, so try the whole line joined up
    and each run of digits and letters on its own.
    """
    compact = re.sub(r"[^A-Z0-9]", "", text)
    tokens = [re.sub(r"[^A-Z0-9]", "", token) for token in re.split(r"\s+", text) if token]
    # Also join neighbouring groups, for numbers printed with spaces between parts.
    joined = ["".join(tokens[start:end]) for start in range(len(tokens)) for end in range(start + 2, len(tokens) + 1)]
    return [c for c in dict.fromkeys([compact, *tokens, *joined]) if c]


def _parse_document_number(text: str, country: str | None, known_dates: set[str] = frozenset()) -> str | None:
    pattern = ID_NUMBER_PATTERNS.get(country or "")
    candidates = _number_candidates(text)
    if pattern:
        for candidate in candidates:
            if pattern.fullmatch(candidate):
                return candidate
    # Generic: 5 to 20 letters and digits, at least 4 digits, and not a printed date.
    for candidate in candidates:
        if 5 <= len(candidate) <= 20 and sum(ch.isdigit() for ch in candidate) >= 4:
            if candidate not in known_dates:
                return candidate
    return None


def _find_known_id_number(lines: list[str], country: str | None) -> str | None:
    """Last resort: the number matches a known national format, wherever it sits."""
    pattern = ID_NUMBER_PATTERNS.get(country or "")
    if not pattern:
        return None
    for line in lines:
        for candidate in _number_candidates(line):
            if pattern.fullmatch(candidate):
                return candidate
    return None


def _all_label_words() -> set[str]:
    return {word for labels in LABELS.values() for label in labels for word in label.split()}


_LABEL_WORDS = _all_label_words()


def _parse_name(text: str) -> str | None:
    words = [w for w in re.split(r"[^A-Z'\-]+", text) if w and w not in _LABEL_WORDS]
    if not words or len(words) > 6 or any(ch.isdigit() for ch in text):
        return None
    return " ".join(words)


def to_alpha3(value: str | None) -> str | None:
    """Accept a country as 'RW' (what our backend sends Shufti), 'RWA' or 'Rwanda'.

    Returns the 3-letter code used by the MRZ and by this service, or None.
    """
    if not value:
        return None
    text = normalize(value)
    if len(text) == 3 and text in ALPHA3_CODES:
        return text
    if len(text) == 2:
        return ALPHA2_TO_ALPHA3.get(text)
    for name, code in COUNTRY_NAMES:
        if text == name:
            return code
    return None
