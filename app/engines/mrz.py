"""Find, clean and validate the Machine Readable Zone (ICAO 9303) of a document.

Formats:
  TD3  passports             2 lines x 44 characters
  TD2  some ID cards / visas 2 lines x 36 characters
  TD1  ID cards (back side)  3 lines x 30 characters
"""

import re
from dataclasses import dataclass, field

import cv2
import numpy as np
from mrz.checker.td1 import TD1CodeChecker
from mrz.checker.td2 import TD2CodeChecker
from mrz.checker.td3 import TD3CodeChecker

from app.engines.image import deskew, resize_to_width, to_gray
from app.engines.ocr import MRZ_CHARSET, read_text

FORMATS = {
    "TD3": {"lines": 2, "length": 44, "checker": TD3CodeChecker},
    "TD2": {"lines": 2, "length": 36, "checker": TD2CodeChecker},
    "TD1": {"lines": 3, "length": 30, "checker": TD1CodeChecker},
}

# Character positions that can only hold digits or only letters, per format and line.
# OCR often confuses O/0, I/1, S/5, B/8, so these positions are corrected by type.
_DIGIT_SLOTS = {
    "TD3": {1: [9, *range(13, 20), *range(21, 28), 42, 43]},
    "TD2": {1: [9, *range(13, 20), *range(21, 28), 35]},
    "TD1": {0: [14], 1: [*range(0, 7), *range(8, 15), 29]},
}
_ALPHA_SLOTS = {
    "TD3": {0: [2, 3, 4], 1: [10, 11, 12]},
    "TD2": {0: [2, 3, 4], 1: [10, 11, 12]},
    "TD1": {0: [2, 3, 4], 1: [15, 16, 17]},
}
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})
_TO_ALPHA = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"})

# Where the name sits: (line index, start column) per format.
_NAME_FIELD = {"TD3": (0, 5), "TD2": (0, 5), "TD1": (2, 0)}
# Letters OCR tends to produce when it misreads a '<' filler.
_FILLER_LOOKALIKES = "KXCLZ"
_STRAY_TRAILING_LETTER = re.compile(rf"<[{_FILLER_LOOKALIKES}](?=<*$)")

_MRZ_LINE = re.compile(r"^[A-Z0-9<]{25,50}$")

_CHECK_NAMES = {
    "document number hash": "document_number",
    "birth date hash": "date_of_birth",
    "expiry date hash": "expiry_date",
    "optional data hash": "optional_data",
    "final hash": "composite",
}


@dataclass
class MrzResult:
    format: str
    lines: list[str]
    valid: bool
    checksums: dict[str, bool]
    failed_checks: list[str]
    fields: dict[str, str] = field(default_factory=dict)


def read_mrz(image: np.ndarray) -> MrzResult | None:
    """OCR the likely MRZ regions of an image and return the best parse, if any."""
    best: MrzResult | None = None
    for candidate in _ocr_candidates(image):
        result = parse_mrz_text(candidate)
        if result is None:
            continue
        if result.valid:
            return result
        if best is None or len(result.failed_checks) < len(best.failed_checks):
            best = result
    return best


def _ocr_candidates(image: np.ndarray):
    gray = to_gray(image)
    if gray.shape[1] < 1500:
        gray = resize_to_width(gray, 1500)
    gray = deskew(gray)

    height = gray.shape[0]
    # The MRZ sits at the bottom of the data page, so try that band first.
    regions = [gray[int(height * 0.60):], gray[int(height * 0.45):], gray]
    for region in regions:
        yield read_text(region, whitelist=MRZ_CHARSET)
        _, binary = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield read_text(binary, whitelist=MRZ_CHARSET)


def parse_mrz_text(text: str) -> MrzResult | None:
    """Pick MRZ-looking lines out of raw OCR text and validate them."""
    lines = [re.sub(r"\s+", "", line.upper()) for line in text.splitlines()]
    lines = [line for line in lines if _MRZ_LINE.match(line) and "<" in line]
    if len(lines) < 2:
        return None

    best: MrzResult | None = None
    for fmt, spec in FORMATS.items():
        count = spec["lines"]
        for start in range(len(lines) - count + 1):
            group = lines[start:start + count]
            if not _plausible_length(group, spec["length"]):
                continue
            fixed = [_fit_length(line, spec["length"]) for line in group]
            fixed = _correct_char_types(fmt, fixed)
            result = _check(fmt, fixed)
            if result.valid:
                return result
            if best is None or len(result.failed_checks) < len(best.failed_checks):
                best = result
    return best


def _plausible_length(group: list[str], length: int) -> bool:
    return all(abs(len(line) - length) <= 3 for line in group)


def _fit_length(line: str, length: int) -> str:
    # OCR commonly adds or drops filler '<' characters at the end of a line.
    if len(line) > length:
        return line[:length]
    return line.ljust(length, "<")


def _correct_char_types(fmt: str, lines: list[str]) -> list[str]:
    corrected = []
    for index, line in enumerate(lines):
        chars = list(line)
        for pos in _DIGIT_SLOTS[fmt].get(index, []):
            if chars[pos] != "<":
                chars[pos] = chars[pos].translate(_TO_DIGIT)
        for pos in _ALPHA_SLOTS[fmt].get(index, []):
            chars[pos] = chars[pos].translate(_TO_ALPHA)
        corrected.append("".join(chars))

    line_index, start = _NAME_FIELD[fmt]
    line = corrected[line_index]
    corrected[line_index] = line[:start] + _clean_name_field(line[start:])
    return corrected


def _clean_name_field(name: str) -> str:
    """Remove OCR noise from the name, which has no check digit to catch it.

    Layout is SURNAME<<GIVEN<NAMES<<<<, and given names are separated by a single
    '<', so anything after the next '<<' must be filler. A lone K/X/C/L/Z just
    before the fillers is almost always a misread '<'.
    """
    surname, sep, given = name.partition("<<")
    if sep:
        given = given.split("<<", 1)[0]
        given = _STRAY_TRAILING_LETTER.sub("", given)
    cleaned = surname + sep + given
    return cleaned.ljust(len(name), "<")


def _check(fmt: str, lines: list[str]) -> MrzResult:
    checker = FORMATS[fmt]["checker"]("\n".join(lines))
    report = checker.report
    checksums = {}
    for name, ok in report.fields:
        if name in _CHECK_NAMES:
            checksums[_CHECK_NAMES[name]] = ok
    failed = [name for name, _ in report.falses]
    fields = checker.fields()._asdict()
    return MrzResult(
        format=fmt,
        lines=lines,
        valid=bool(checker),
        checksums=checksums,
        failed_checks=failed,
        fields={key: value for key, value in fields.items() if not key.endswith("hash")},
    )
