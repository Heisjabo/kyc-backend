from datetime import date

import pytest
from fastapi.testclient import TestClient
from mrz.generator.td1 import TD1CodeGenerator
from mrz.generator.td3 import TD3CodeGenerator

from app.engines.fields import detect_country, extract_visual_fields, to_alpha3
from app.engines.mrz import parse_mrz_text
from app.main import app
from app.pipeline.document import scan_document
from app.schemas.document import Applicant, DocumentType
from tests.synthetic import blurred, mono_font, render_card, render_document, render_qr_like, render_text_page, to_bytes

TODAY = date(2026, 9, 22)
SPECIMEN = ["P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<", "L898902C36UTO7408122F1204159ZE184226B<<<<<10"]
RWA_PASSPORT = str(
    TD3CodeGenerator("P", "RWA", "Mugisha", "Jean Claude", "PC1234567", "RWA", "950315", "M", "310101", "")
).split("\n")
KEN_ID_BACK = str(
    TD1CodeGenerator("I", "KEN", "123456789", "900101", "F", "300101", "KEN", "Wanjiru", "Grace Akinyi")
).split("\n")

needs_font = pytest.mark.skipif(mono_font(20) is None, reason="no monospace font to render test documents")
client = TestClient(app)


# MRZ parsing on text (no OCR involved)

def test_parses_icao_specimen():
    result = parse_mrz_text("\n".join(SPECIMEN))
    assert result.format == "TD3"
    assert result.valid
    assert result.fields["surname"] == "ERIKSSON"
    assert result.fields["name"] == "ANNA MARIA"


def test_corrects_ocr_letter_digit_confusion_and_short_lines():
    noisy = "P<UT0ERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<\nL898902C36UTO74O8122F12O4159ZE184226B<<<<<10"
    result = parse_mrz_text(noisy)
    assert result.valid
    assert result.lines == SPECIMEN


def test_detects_tampered_birth_date():
    tampered = "\n".join(SPECIMEN).replace("7408122", "7408123")
    result = parse_mrz_text(tampered)
    assert not result.valid
    assert result.checksums["date_of_birth"] is False


def test_drops_stray_filler_letter_from_name():
    line1 = "P<RWAMUGISHA<<JEAN<CLAUDE<X<<<<<<<<<<<<<<<<<"
    result = parse_mrz_text(f"{line1}\n{RWA_PASSPORT[1]}")
    assert result.fields["name"] == "JEAN CLAUDE"


def test_parses_td1_id_card():
    result = parse_mrz_text("\n".join(KEN_ID_BACK))
    assert result.format == "TD1"
    assert result.valid


def test_ignores_text_without_mrz():
    assert parse_mrz_text("REPUBLIC OF RWANDA\nNATIONAL ID\n") is None


# Full pipeline on rendered images

@needs_font
def test_valid_passport_passes():
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons
    assert result.extracted.document_number == "PC1234567"
    assert result.extracted.expiry_date == date(2031, 1, 1)


@needs_font
def test_swapped_first_and_last_name_still_matches():
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Mugisha", last_name="Jean"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons


@needs_font
def test_expired_passport_fails():
    result = scan_document(DocumentType.passport, to_bytes(render_document(SPECIMEN)), today=TODAY)
    assert result.decision == "fail"
    assert "DOCUMENT_EXPIRED" in result.reasons


@needs_font
def test_name_and_dob_mismatch_fails():
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Alice", last_name="Uwase", date_of_birth=date(1990, 1, 1)),
        today=TODAY,
    )
    assert result.decision == "fail"
    assert {"NAME_MISMATCH", "DOB_MISMATCH"} <= set(result.reasons)


@needs_font
def test_blurry_photo_asks_for_retake():
    result = scan_document(DocumentType.passport, to_bytes(blurred(render_document(RWA_PASSPORT))), today=TODAY)
    assert result.decision == "retake"
    assert "IMAGE_BLURRY" in result.reasons


@needs_font
def test_id_card_reads_mrz_from_back():
    result = scan_document(
        DocumentType.id_card,
        to_bytes(render_document(["front side"], header="ID CARD")),
        back=to_bytes(render_document(KEN_ID_BACK, header="")),
        applicant=Applicant(first_name="Grace", last_name="Wanjiru"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons
    assert result.extracted.issuing_country == "KEN"


RWA_ID_FRONT_ROWS = [
    ("Amazina / Names", "MUGISHA Jean Claude"),
    ("Itariki yavukiyeho / Date of Birth", "15/03/1995"),
    ("Igitsina / Sex", "Gabo / M"),
    ("Aho Yatangiwe / Place of Issue", "Gasabo"),
    ("Indangamuntu / National ID No.", "1 1995 8 0012345 0 12"),
]
KEN_LICENCE_ROWS = [
    ("Surname", "WANJIRU"),
    ("Other Names", "Grace Akinyi"),
    ("Date of Birth", "01.01.1990"),
    ("Licence No.", "DL4471029"),
    ("Date of Issue", "12.06.2021"),
    ("Date of Expiry", "11.06.2027"),
]


def rwa_id_front():
    return to_bytes(render_card("REPUBULIKA Y'U RWANDA", "INDANGAMUNTU / NATIONAL IDENTITY CARD", RWA_ID_FRONT_ROWS))


def ken_licence(rows=KEN_LICENCE_ROWS):
    return to_bytes(render_card("REPUBLIC OF KENYA", "DRIVING LICENCE", rows))


@needs_font
def test_id_card_without_mrz_is_read_from_printed_text():
    result = scan_document(
        DocumentType.id_card,
        rwa_id_front(),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons
    assert result.extracted.source == "visual"
    assert result.extracted.document_number == "1199580012345012"
    assert result.extracted.date_of_birth == date(1995, 3, 15)
    assert result.extracted.sex == "M"
    # Rwandan IDs have no expiry date: noted, not held against the document.
    assert "EXPIRY_DATE_NOT_FOUND" in result.warnings


@needs_font
def test_id_card_without_mrz_needs_applicant_name():
    result = scan_document(DocumentType.id_card, rwa_id_front(), today=TODAY)
    assert result.decision == "review"
    assert "APPLICANT_NAME_REQUIRED" in result.reasons


@needs_font
def test_driving_licence_is_read_from_printed_text():
    result = scan_document(
        DocumentType.driving_license,
        ken_licence(),
        applicant=Applicant(first_name="Grace", last_name="Wanjiru", date_of_birth=date(1990, 1, 1), issuing_country="KEN"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons
    assert result.extracted.surname == "WANJIRU"
    assert result.extracted.document_number == "DL4471029"
    assert result.extracted.expiry_date == date(2027, 6, 11)
    assert result.extracted.issue_date == date(2021, 6, 12)


@needs_font
def test_expired_driving_licence_fails():
    rows = [row if row[0] != "Date of Expiry" else ("Date of Expiry", "11.06.2024") for row in KEN_LICENCE_ROWS]
    result = scan_document(
        DocumentType.driving_license,
        ken_licence(rows),
        applicant=Applicant(first_name="Grace", last_name="Wanjiru"),
        today=TODAY,
    )
    assert result.decision == "fail"
    assert "DOCUMENT_EXPIRED" in result.reasons


@needs_font
def test_wrong_document_type_goes_to_review():
    result = scan_document(
        DocumentType.id_card,
        ken_licence(),
        applicant=Applicant(first_name="Grace", last_name="Wanjiru"),
        today=TODAY,
    )
    assert result.decision == "review"
    assert "DOCUMENT_TYPE_MISMATCH" in result.reasons


@needs_font
def test_someone_elses_id_fails():
    result = scan_document(
        DocumentType.id_card,
        rwa_id_front(),
        applicant=Applicant(first_name="Alice", last_name="Uwase", date_of_birth=date(1990, 1, 1)),
        today=TODAY,
    )
    assert result.decision == "fail"
    assert {"NAME_MISMATCH", "DOB_MISMATCH"} <= set(result.reasons)


def rwa_id_back():
    rows = [
        ("Aho Yatangiwe / Place of Issue", "Gasabo"),
        ("Itariki Yatangiweho / Date of Issue", "15/04/2015"),
        ("Urwego Rwabitanze / Issuing Authority", "NIDA"),
    ]
    return to_bytes(render_card("REPUBULIKA Y'U RWANDA", "", rows))


@needs_font
def test_back_side_identical_to_front_fails():
    front = rwa_id_front()
    result = scan_document(
        DocumentType.id_card,
        front,
        back=front,
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "retake"
    assert "FRONT_AND_BACK_IDENTICAL" in result.reasons
    assert "BACK_SIDE_IS_FRONT" in result.reasons


@needs_font
def test_back_side_with_portrait_fails(monkeypatch):
    import app.pipeline.document as doc_mod
    from app.engines.face import Face, Landmarks

    marks = Landmarks(right_eye=(0, 0), left_eye=(0, 0), nose=(0, 0), right_mouth=(0, 0), left_mouth=(0, 0))
    fake_face = [Face(x=20, y=20, width=150, height=150, score=0.95, landmarks=marks)]
    monkeypatch.setattr(doc_mod, "detect_faces", lambda img: fake_face)

    result = scan_document(
        DocumentType.id_card,
        rwa_id_front(),
        back=rwa_id_back(),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "retake"
    assert "PORTRAIT_ON_BACK_SIDE" in result.reasons
    assert "BACK_SIDE_IS_FRONT" in result.reasons


@needs_font
def test_valid_back_side_passes():
    result = scan_document(
        DocumentType.id_card,
        rwa_id_front(),
        back=rwa_id_back(),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons


def test_qr_code_is_not_a_document():
    qr = to_bytes(render_qr_like())
    result = scan_document(DocumentType.id_card, qr, back=qr, today=TODAY)
    assert result.decision == "retake"
    assert result.reasons[0] == "DOCUMENT_NOT_DETECTED"


@needs_font
def test_ordinary_text_page_is_not_a_document():
    page = to_bytes(render_text_page(["Meeting notes", "Discuss the budget for next quarter", "Call the supplier"]))
    result = scan_document(DocumentType.passport, page, today=TODAY)
    assert result.decision == "retake"
    assert "DOCUMENT_NOT_DETECTED" in result.reasons


# API

@needs_font
def test_api_check_passport():
    response = client.post(
        "/v1/document/scan",
        data={"document_type": "passport", "first_name": "Jean", "last_name": "Mugisha", "date_of_birth": "1995-03-15"},
        files={"front": ("passport.png", to_bytes(render_document(RWA_PASSPORT)), "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "pass"
    assert body["extracted"]["surname"] == "MUGISHA"


def test_api_rejects_non_image_content_type():
    response = client.post(
        "/v1/document/scan",
        data={"document_type": "passport"},
        files={"front": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 415


def test_api_reports_unreadable_image():
    response = client.post(
        "/v1/document/scan",
        data={"document_type": "passport"},
        files={"front": ("fake.png", b"not really a png", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["reasons"] == ["IMAGE_UNREADABLE"]


@needs_font
@pytest.mark.parametrize("angle", [-8, -4, 4, 8])
def test_tilted_phone_photo_is_straightened(angle):
    photo = render_document(RWA_PASSPORT).rotate(angle, expand=True, fillcolor=(60, 60, 60))
    result = scan_document(DocumentType.passport, to_bytes(photo, "JPEG"), today=TODAY)
    assert result.decision == "pass", result.reasons


# Country codes: the backend sends Shufti 2-letter codes, the MRZ uses 3-letter ones.

@pytest.mark.parametrize(
    "value,expected",
    [("RW", "RWA"), ("rw", "RWA"), ("RWA", "RWA"), ("Rwanda", "RWA"), ("KE", "KEN"),
     ("NG", "NGA"), ("Unknown", None), ("XX", None), (None, None)],
)
def test_country_accepts_backend_and_mrz_formats(value, expected):
    assert to_alpha3(value) == expected


@needs_font
@pytest.mark.parametrize("country", ["RW", "RWA", "Rwanda"])
def test_scan_accepts_any_country_format(country):
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", issuing_country=country),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons


@needs_font
def test_unrecognised_country_is_only_a_warning():
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", issuing_country="Unknown"),
        today=TODAY,
    )
    assert result.decision == "pass", result.reasons
    assert "COUNTRY_NOT_RECOGNISED" in result.warnings


@needs_font
def test_country_mismatch_goes_to_review():
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", issuing_country="KE"),
        today=TODAY,
    )
    assert result.decision == "review"
    assert "COUNTRY_MISMATCH" in result.reasons


# Country printed on the document: every country, but only when it reads as a heading.

@pytest.mark.parametrize(
    "lines,expected",
    [
        (["REPUBLIC OF KENYA", "DRIVING LICENCE"], "KEN"),
        (["REPUBULIKA Y'U RWANDA", "INDANGAMUNTU"], "RWA"),
        (["FEDERAL REPUBLIC OF NIGERIA", "NATIONAL ID CARD"], "NGA"),
        (["JAMHURI YA MUUNGANO WA TANZANIA"], "TZA"),
        (["REPUBLIQUE FRANCAISE", "CARTE NATIONALE D'IDENTITE"], "FRA"),
        (["BUNDESREPUBLIK DEUTSCHLAND", "PERSONALAUSWEIS"], "DEU"),
        (["REPUBLIC OF THE PHILIPPINES"], "PHL"),
        (["KENYA"], "KEN"),
        # Several countries are also personal names; those must not set the country.
        (["NATIONAL IDENTITY CARD", "Names", "MUGISHA Jordan Claude"], None),
        (["NATIONAL IDENTITY CARD", "Surname", "CHAD", "Given names", "Emmanuel"], None),
        (["REPUBLIC OF CHAD", "IDENTITY CARD"], "TCD"),
    ],
)
def test_detects_country_from_heading_only(lines, expected):
    assert detect_country(lines) == expected


# Layouts taken from real Rwandan documents (names and numbers changed).
# Both print the number on the same line as its label, which earlier returned nothing.

REAL_RWA_LICENCE_LINES = [
    "REPUBULIKA Y'U RWANDA",
    "REPUBLIC OF RWANDA",
    "URUHUSHYA RWO GUTWARA IBINYABIZIGA",
    "DRIVING LICENSE",
    "Amazina / Names",
    "KWIZERA Alice Grace",
    "Yavutse / Date of Birth",
    "24/2/2002",
    "Igitsina / Sex Izarangira / Expiry",
    "Gore / F",
    "12/8/2035",
    "Yatangiwe / Place of Issue",
    "KIGALI",
    "Class:",
    "B",
    "D.L.No.1200280111110017",
]
REAL_RWA_ID_LINES = [
    "REPUBULIKA Y'U RWANDA",
    "REPUBLIC OFRWANDA",
    "INDANGAMUNTU",
    "NATIONAL IDENTITY CARD",
    "Amazina/Names",
    "KWIZERA Alice Grace",
    "Itariki yavutseho/Date of Birth",
    "24/02/2002",
    "Igitsina / Sex Aho Yatangiwe / Place of Issue",
    "Gore/F",
    "Ruhango /Kinazi",
    "Umukono wa Nyirayo/ Signature",
    "Indangamuntu / National ID No. 1 2002 8 0111110 0 63",
]
# The back of a Rwandan card carries only instructions: not a document on its own.
REAL_RWA_ID_BACK_LINES = [
    "Utoraguye iyi karita, wayishyikiriza ubuyobozi bwa polisi bukwegereye",
    "If found, please return this card to the nearest police",
    "Uzayikoresha binyuranyije nitegeko azahanwa",
    "Whoever uses this card contrary to the law will be punished",
]


def test_reads_real_rwandan_licence_layout():
    fields = extract_visual_fields(REAL_RWA_LICENCE_LINES, TODAY)
    assert fields.document_types == ["driving_license"]
    assert fields.country == "RWA"
    assert fields.document_number == "1200280111110017"
    assert fields.date_of_birth == date(2002, 2, 24)
    assert fields.expiry_date == date(2035, 8, 12)
    assert fields.sex == "F"
    assert fields.full_name == "KWIZERA ALICE GRACE"


def test_reads_real_rwandan_id_layout():
    fields = extract_visual_fields(REAL_RWA_ID_LINES, TODAY)
    assert fields.document_types == ["id_card"]
    assert fields.country == "RWA"
    # Printed in groups on the label's own line: "1 2002 8 0111110 0 63"
    assert fields.document_number == "1200280111110063"
    assert fields.date_of_birth == date(2002, 2, 24)
    assert fields.expiry_date is None
    assert fields.full_name == "KWIZERA ALICE GRACE"


def test_card_back_alone_is_not_a_document():
    fields = extract_visual_fields(REAL_RWA_ID_BACK_LINES, TODAY)
    assert fields.document_types == []
    assert fields.country is None
