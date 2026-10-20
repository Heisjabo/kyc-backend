"""Proof of address.

The bills are drawn here rather than photographed, so these test the rules rather
than the OCR, but the text is read by the real reader and the places are matched
by the real matcher.

The place expansion tests need neither an image nor a database: they are the
judgement this step turns on, which is whether an address and a document name the
same place when they spell it differently.
"""

import io
import uuid
from datetime import date, timedelta

import cv2
import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import FileKind, Step, StepStatus, VerificationFile, VerificationStep
from app.db.session import get_session
from app.engines.addresses import address_on_document, is_po_box, normalise
from app.main import app
from app.pipeline.address import scan_address_proof
from app.schemas.address import ProofType
from app.schemas.document import CheckResult, CheckStatus, Decision, DocumentCheckResponse, ExtractedFields

ACCOUNT = {
    "email": "address@example.com", "password": "horse-battery", "first_name": "Jean",
    "last_name": "Mugisha", "phone": "788123456", "country_code": "+250",
    "date_of_birth": "1995-04-12", "country": "Rwanda",
}


# The comparison, which is direct

def test_the_same_address_matches_however_it_was_punctuated():
    """Case, spacing and punctuation are how text was printed, not what it says."""
    assert address_on_document("KG 586 Street, Kigali", ["kg 586 street kigali"])
    assert address_on_document("KG 586 Street, Kigali", ["Address: KG 586 STREET, KIGALI"])
    assert address_on_document("KG 586 Street, Kigali", ["KG  586   Street,  Kigali."])


def test_an_address_printed_across_two_lines_still_matches():
    """A line break is not a difference in the address."""
    assert address_on_document("KG 586 Street, Kigali City", ["KG 586 Street", "Kigali City"])


def test_a_street_code_does_not_stand_in_for_the_city():
    """The case to be sure about: Kigali, Rwanda against a bill printing KG 333 St
    is a mismatch, and it fails."""
    assert not address_on_document("Kigali, Rwanda", ["Address: KG 333 St"])


def test_a_different_wording_of_the_same_place_does_not_match():
    assert not address_on_document("Kigali, Rwanda", ["KIGALI CITY"])
    assert not address_on_document("KG 586 Street", ["KG 586 St"])
    assert not address_on_document("Kicukiro, Kigali", ["Kigali, Rwanda"])


def test_a_partly_shared_address_does_not_match():
    assert not address_on_document("KG 586 Street, Kigali", ["KG 12 Street, Kigali"])


def test_an_empty_address_matches_nothing():
    assert not address_on_document("", ["KG 586 Street, Kigali"])
    assert not address_on_document("   ", ["KG 586 Street, Kigali"])


def test_normalising_keeps_the_words_and_drops_the_rest():
    assert normalise("KG 586 Street, Kigali City!") == "kg 586 street kigali city"


def test_a_po_box_is_recognised_however_it_is_written():
    assert is_po_box("P.O Box 19244-00100")
    assert is_po_box("PO BOX 123, Nairobi")
    assert is_po_box("Post Office Box 9")
    assert not is_po_box("KG 586 Street, Kigali")


# The check itself

def bill(*lines: str, width: int = 1400, height: int = 900) -> bytes:
    image = np.full((height, width, 3), 245, dtype=np.uint8)
    y = 90
    for text_line in lines:
        cv2.putText(image, text_line, (70, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2, cv2.LINE_AA)
        y += 70
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def utility_bill(place="KG 586 ST, KIGALI CITY", name="JEAN MUGISHA", issued=None) -> bytes:
    issued = issued or date.today() - timedelta(days=10)
    return bill(
        "REG ELECTRICITY BILL",
        "Customer: " + name,
        "Address: " + place,
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Date: " + issued.strftime("%d-%m-%Y"),
        "Amount due: RWF 12,400",
    )


def scanned_pdf(*lines: str) -> bytes:
    """A photograph or scan wrapped in a PDF: no text inside it, only a picture.

    Built by drawing the page and letting PIL save it as a PDF, which is exactly
    what a phone scanner app produces.
    """
    return as_pdf(bill(*lines))


def as_pdf(picture: bytes) -> bytes:
    """A picture saved as a one-page PDF, the only kind of upload the session page sends."""
    image = cv2.imdecode(np.frombuffer(picture, np.uint8), cv2.IMREAD_COLOR)
    out = io.BytesIO()
    Image.fromarray(image[:, :, ::-1]).save(out, format="PDF")
    return out.getvalue()


def digital_pdf(*lines: str) -> bytes:
    """A PDF with its text inside it, the way a bank generates a statement.

    Written by hand rather than with a library, because what is under test is that
    the text layer is read instead of the page being rendered and guessed at.
    """
    content = "BT /F1 14 Tf 40 780 Td 16 TL\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode()

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


def scan(proof, claimed="KG 586 St, Kigali City", names=("Jean", "Mugisha"),
         proof_type=ProofType.utility_bill):
    return scan_address_proof(
        proof_type=proof_type,
        claimed_address=claimed,
        front=proof,
        names=list(names),
    )


def named(result) -> dict:
    return {check.name: check.status for check in result.checks}


def test_the_address_written_the_same_way_passes():
    result = scan(utility_bill(place="KG 586 ST, KIGALI CITY"))
    assert result.decision == Decision.passed
    assert result.extracted.address_found is True
    assert named(result)["address_on_document_match"] == CheckStatus.passed


def test_a_street_code_on_the_bill_fails_against_a_city_and_country():
    """The case the client asked about: opened with "Kigali, Rwanda", the bill
    prints "KG 333 St". That is a mismatch, and it fails outright."""
    result = scan(utility_bill(place="KG 333 ST"), claimed="Kigali, Rwanda")
    assert result.decision == Decision.failed
    assert "ADDRESS_MISMATCH" in result.reasons
    assert result.extracted.address_found is False


def test_a_different_city_fails():
    result = scan(utility_bill(place="NAIROBI, KENYA"))
    assert result.decision == Decision.failed
    assert "ADDRESS_MISMATCH" in result.reasons


def test_a_near_miss_fails_rather_than_going_to_a_person():
    """Same city, different street. No partial credit and no review."""
    result = scan(utility_bill(place="KG 12 ST, KIGALI CITY"))
    assert result.decision == Decision.failed
    assert "ADDRESS_MISMATCH" in result.reasons


def test_a_po_box_is_refused_without_reading_anything():
    result = scan(utility_bill(), claimed="P.O Box 19244-00100")
    assert result.decision == Decision.failed
    assert result.reasons == ["PO_BOX_NOT_ACCEPTED"]
    # Nothing was read: the address itself was the problem.
    assert [check.name for check in result.checks] == ["address_on_document_match"]


def test_a_bill_in_somebody_elses_name_fails():
    result = scan(utility_bill(name="ALICE MUKAMANA"))
    assert result.decision == Decision.failed
    assert "NAME_NOT_ON_ADDRESS_PROOF" in result.reasons
    assert named(result)["match_address_proofs_with_document_proofs"] == CheckStatus.failed


def test_an_unverified_owner_goes_to_a_person():
    result = scan(utility_bill(), names=())
    assert result.decision == Decision.review
    assert "OWNER_NOT_VERIFIED" in result.reasons


def test_a_bill_older_than_ninety_days_fails():
    result = scan(utility_bill(issued=date.today() - timedelta(days=200)))
    assert result.decision == Decision.failed
    assert "PROOF_TOO_OLD" in result.reasons
    assert result.extracted.proof_age_days >= 200


def test_a_bill_with_no_readable_date_goes_to_a_person():
    proof = bill(
        "REG ELECTRICITY BILL",
        "Customer: JEAN MUGISHA",
        "Address: KG 586 ST, KIGALI CITY",
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Amount due: RWF 12,400",
    )
    result = scan(proof)
    assert result.decision == Decision.review
    assert "PROOF_DATE_NOT_FOUND" in result.reasons


def test_a_document_that_is_not_the_declared_type_goes_to_a_person():
    result = scan(utility_bill(), proof_type=ProofType.bank_statement)
    assert result.decision == Decision.review
    assert "PROOF_TYPE_MISMATCH" in result.reasons


def test_a_bank_statement_is_recognised_as_one():
    proof = bill(
        "BANK OF KIGALI",
        "ACCOUNT STATEMENT",
        "Account holder: JEAN MUGISHA",
        "Address: KG 586 ST, KIGALI",
        "Opening balance: 120,000",
        "Closing balance: 98,400",
        "Date: " + (date.today() - timedelta(days=5)).strftime("%d-%m-%Y"),
    )
    result = scan(proof, claimed="KG 586 St, Kigali", proof_type=ProofType.bank_statement)
    assert result.decision == Decision.passed
    assert named(result)["address_document_type"] == CheckStatus.passed


def test_a_page_with_nothing_on_it_asks_for_another_photograph():
    result = scan(bill("BILL"))
    assert result.decision == Decision.retake
    assert "PROOF_NOT_READABLE" in result.reasons


def test_a_pdf_statement_takes_its_words_from_the_text_layer(monkeypatch):
    """A bank's own PDF carries its words inside it, and reading them is exact where
    OCR of a rendering is a guess. That matters because the address comparison is
    exact: one misread character would fail a genuine statement.

    The pages are still rendered and read, but only to be scored and compared with,
    never as the source of the text."""
    from app.pipeline import address as pipeline

    layer = [
        "BANK OF KIGALI",
        "ACCOUNT STATEMENT",
        "Account holder: JEAN MUGISHA",
        "Address: KG 586 St, Kigali City",
        "Opening balance: 120,000",
        "Closing balance: 98,400",
        "Date: " + (date.today() - timedelta(days=7)).strftime("%d-%m-%Y"),
    ]
    monkeypatch.setattr(pipeline, "text_lines", lambda data, max_pages=None: layer)

    # OCR returns something deliberately mangled. If the step took its text from the
    # rendering, the exact address match would fail on it.
    class Line:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(pipeline, "read_document",
                        lambda image: [Line("KG 5B6 St, Kigah City"), Line("BANK 0F KIGALI")])

    result = scan(digital_pdf(*layer), claimed="KG 586 St, Kigali City",
                  proof_type=ProofType.bank_statement)
    assert result.extracted.address_found is True
    proof = [c for c in result.checks if c.name == "address_document_proof"][0]
    assert "pdf text" in str(proof.details["read"])


def test_a_scan_wrapped_in_a_pdf_is_rendered_and_read():
    """What a phone scanner app produces: a picture in a PDF, no text inside it.
    That has to be rendered and read like any other photograph."""
    proof = scanned_pdf(
        "REG ELECTRICITY BILL",
        "Customer: JEAN MUGISHA",
        "Address: KG 586 ST, KIGALI CITY",
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Date: " + (date.today() - timedelta(days=12)).strftime("%d-%m-%Y"),
        "Amount due: RWF 12,400",
    )
    result = scan(proof)
    assert result.decision == Decision.passed
    assert any("pdf rendered" in str(check.details.get("read", "")) for check in result.checks)


def test_a_digital_pdf_is_scored_even_though_its_text_was_read_directly(monkeypatch):
    """The gap this closes: a statement whose text can be read needs no rendering to
    be understood, so it used to contribute no picture and was never asked whether a
    generator had made it. A generated bill submitted as a digital PDF is the easiest
    forgery in this flow and it went straight through."""
    from app.pipeline import ai_image as check_module

    scored: list = []

    def every_page_is_generated(images):
        scored.extend(images)
        return [0.99] * len(images)

    monkeypatch.setattr(check_module.ai_image, "available", lambda: True)
    monkeypatch.setattr(check_module.ai_image, "probabilities", every_page_is_generated)

    result = scan(
        digital_pdf(
            "BANK OF KIGALI",
            "ACCOUNT STATEMENT",
            "Account holder: JEAN MUGISHA",
            "Address: KG 586 St, Kigali City",
            "Closing balance: 98,400",
            "Date: " + (date.today() - timedelta(days=5)).strftime("%d-%m-%Y"),
        ),
        proof_type=ProofType.bank_statement,
    )
    assert scored, "the pages were never handed to the detector"
    assert "AI_GENERATED_IMAGE" in result.warnings
    # Read from the text layer, and scored on the rendered page: both.
    proof = [c for c in result.checks if c.name == "address_document_proof"][0]
    assert "pdf text" in str(proof.details["read"])
    assert result.decision == Decision.passed


def test_a_text_layer_that_disagrees_with_the_page_goes_to_a_person(monkeypatch):
    """The attack this closes: a PDF's text layer and its rendering are independent.
    On a scan the text layer is whatever a scanner app wrote, or whatever somebody
    replaced it with, so a bill can show Nairobi while its text says Kigali, and
    every check here reads the text."""
    from app.pipeline import address as pipeline

    page_says_nairobi = scanned_pdf(
        "REG ELECTRICITY BILL",
        "Customer: JEAN MUGISHA",
        "Address: NAIROBI KENYA",
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Date: " + (date.today() - timedelta(days=9)).strftime("%d-%m-%Y"),
    )
    # A text layer claiming the address the client gave us.
    monkeypatch.setattr(pipeline, "text_lines", lambda data, max_pages=None: [
        "REG ELECTRICITY BILL",
        "Customer: JEAN MUGISHA",
        "Address: KG 586 ST, KIGALI CITY",
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Date: " + (date.today() - timedelta(days=9)).strftime("%d-%m-%Y"),
    ])

    result = scan(page_says_nairobi)
    assert result.decision == Decision.review
    assert "PDF_TEXT_LAYER_DISAGREES" in result.reasons
    check = [c for c in result.checks if c.name == "pdf_text_matches_page"][0]
    assert check.details["address_in_text_layer"] is True
    assert check.details["address_on_rendered_page"] is False


def test_a_pdf_whose_text_matches_its_page_passes_the_cross_check():
    result = scan(
        digital_pdf(
            "BANK OF KIGALI",
            "ACCOUNT STATEMENT",
            "Account holder: JEAN MUGISHA",
            "Address: KG 586 St, Kigali City",
            "Closing balance: 98,400",
            "Date: " + (date.today() - timedelta(days=5)).strftime("%d-%m-%Y"),
        ),
        proof_type=ProofType.bank_statement,
    )
    check = [c for c in result.checks if c.name == "pdf_text_matches_page"][0]
    assert check.status == CheckStatus.passed
    assert result.decision == Decision.passed


def test_the_cross_check_is_skipped_for_a_photograph():
    """A photograph has no text layer to disagree with: what was read is what is
    on it."""
    result = scan(utility_bill())
    check = [c for c in result.checks if c.name == "pdf_text_matches_page"][0]
    assert check.status == CheckStatus.skipped


def test_a_pdf_is_recognised_by_what_is_in_it_not_what_it_was_called():
    from app.engines.pdf import is_pdf

    assert is_pdf(digital_pdf("HELLO"))
    assert is_pdf(scanned_pdf("HELLO"))
    assert not is_pdf(utility_bill())


def test_quality_is_not_judged_on_a_pdf():
    """Blur and glare mean nothing on a page a computer drew, so refusing a clean
    PDF for lacking contrast would be nonsense."""
    result = scan(digital_pdf(
        "REG ELECTRICITY BILL",
        "Customer: JEAN MUGISHA",
        "Address: KG 586 St, Kigali City",
        "Meter number: 37200145",
        "Units: 45 kWh",
        "Date: " + (date.today() - timedelta(days=3)).strftime("%d-%m-%Y"),
    ))
    assert result.decision == Decision.passed
    assert not [check for check in result.checks if check.name.startswith("quality_")]


def test_authenticity_is_reported_as_not_checked_never_as_passed():
    """Shufti runs a tamper check here and we have no model for it. A blank check
    that reads as a pass in a report somebody relies on is worse than one that
    says plainly it was not run."""
    result = scan(utility_bill())
    assert named(result)["address_document_authenticity"] == CheckStatus.skipped
    assert "AUTHENTICITY_NOT_CHECKED" in result.warnings


# The step, end to end

def _reachable() -> bool:
    try:
        create_engine(settings.DATABASE_URL).connect().close()
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")


@pytest.fixture()
def db_session():
    schema = f"test_{uuid.uuid4().hex[:12]}"
    engine = create_engine(settings.DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    admin = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def client(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_DIR", str(tmp_path / "evidence"))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def api_key(client) -> dict:
    registered = client.post("/v1/auth/register", json=ACCOUNT).json()
    bearer = {"Authorization": f"Bearer {registered['access_token']}"}
    made = client.post("/v1/api-keys", json={"name": "tests"}, headers=bearer).json()
    return {"X-API-Key": made["key"]}


def open_with_address(client, api_key, address="KG 586 St, Kigali City", services=None):
    body = {
        "reference": f"ADDR-{uuid.uuid4().hex[:8]}",
        "services": services or ["document", "address"],
        "applicant": {"first_name": "Jean", "last_name": "Mugisha", "country": "Rwanda", "address": address},
    }
    return client.post("/v1/verifications", json=body, headers=api_key)


def pass_document(client, api_key, monkeypatch, **overrides):
    """A verification whose document step passed, so a name has been proved."""
    from app.api.v1.routes import session_steps

    def fake_scan(document_type, front, back=None, applicant=None, today=None, require_adult=True):
        return DocumentCheckResponse(
            decision=Decision.passed, reasons=[], warnings=[], document_type=document_type,
            extracted=ExtractedFields(source="visual", surname="MUGISHA", given_names="JEAN",
                                      issuing_country="RWA", document_number="1199980012345678"),
            checks=[CheckResult(name="document_detection", status=CheckStatus.passed)], processing_ms=3,
        )

    monkeypatch.setattr(session_steps, "scan_document", fake_scan)
    monkeypatch.setattr(session_steps, "_keep_document_portrait", lambda *a: "ok")

    opened = open_with_address(client, api_key, **overrides).json()
    token = opened["session_url"].rsplit("/", 1)[-1]
    client.post(f"/v1/sessions/{token}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", utility_bill(), "image/jpeg")})
    return opened, token


def submit_address(client, token, proof=None, proof_type="utility_bill"):
    return client.post(
        f"/v1/sessions/{token}/address",
        data={"document_type": proof_type},
        files={"proof": ("bill.pdf", as_pdf(proof or utility_bill()), "application/pdf")},
    )


@needs_db
def test_initiation_without_an_address_is_refused(client, api_key):
    """Refused here rather than at the step, where the person would be holding a
    bill and be told the request was wrong all along."""
    response = client.post("/v1/verifications", json={
        "reference": f"ADDR-{uuid.uuid4().hex[:8]}",
        "services": ["document", "address"],
        "applicant": {"first_name": "Jean", "last_name": "Mugisha", "country": "Rwanda"},
    }, headers=api_key)
    assert response.status_code == 422
    assert "applicant.address is required" in response.text


@needs_db
def test_a_blank_address_counts_as_no_address(client, api_key):
    assert open_with_address(client, api_key, address="   ").status_code == 422


@needs_db
def test_an_address_is_not_required_without_the_address_service(client, api_key):
    response = client.post("/v1/verifications", json={
        "reference": f"ADDR-{uuid.uuid4().hex[:8]}",
        "services": ["document", "face"],
        "applicant": {"first_name": "Jean", "last_name": "Mugisha"},
    }, headers=api_key)
    assert response.status_code == 201


@needs_db
def test_the_step_records_its_verdict_and_keeps_the_file(client, api_key, monkeypatch, db_session):
    opened, token = pass_document(client, api_key, monkeypatch)
    body = submit_address(client, token).json()
    assert body["decision"] == "pass"
    assert body["extracted"]["document_type"] == "utility_bill"
    assert body["extracted"]["full_address"] == "KG 586 St, Kigali City"
    assert body["extracted"]["address_found"] is None   # what was read is not sent to the person

    step = db_session.scalars(
        select(Step).where(Step.step == VerificationStep.address)
    ).one()
    assert step.status == StepStatus.passed
    assert step.extracted["document_type"] == "utility_bill"
    assert step.extracted["address_found"] is True
    assert step.device["user_agent"]

    kept = db_session.scalars(
        select(VerificationFile).where(VerificationFile.kind == FileKind.address_proof)
    ).all()
    assert len(kept) == 1 and kept[0].step_id == step.id


@needs_db
def test_the_address_comes_from_the_verification_not_the_request(client, api_key, monkeypatch):
    """The person chooses which document to upload, and nothing else. There is no
    field here for them to type an address into."""
    opened, token = pass_document(client, api_key, monkeypatch)
    response = client.post(
        f"/v1/sessions/{token}/address",
        data={"document_type": "utility_bill", "full_address": "Nairobi, Kenya"},
        files={"proof": ("bill.pdf", as_pdf(utility_bill(place="NAIROBI")), "application/pdf")},
    )
    body = response.json()
    assert body["extracted"]["full_address"] == "KG 586 St, Kigali City"
    assert body["decision"] == "fail"


@needs_db
def test_the_name_looked_for_is_the_one_the_document_proved(client, api_key, monkeypatch):
    opened, token = pass_document(client, api_key, monkeypatch)
    body = submit_address(client, token, proof=utility_bill(name="ALICE MUKAMANA")).json()
    assert body["decision"] == "fail"
    assert "NAME_NOT_ON_ADDRESS_PROOF" in body["reasons"]


@needs_db
def test_a_pdf_is_accepted_and_kept_as_a_pdf(client, api_key, monkeypatch, db_session):
    opened, token = pass_document(client, api_key, monkeypatch)
    proof = digital_pdf(
        "BANK OF KIGALI",
        "ACCOUNT STATEMENT",
        "Account holder: JEAN MUGISHA",
        "Address: KG 586 St, Kigali City",
        "Closing balance: 98,400",
        "Date: " + (date.today() - timedelta(days=4)).strftime("%d-%m-%Y"),
    )
    response = client.post(
        f"/v1/sessions/{token}/address",
        data={"document_type": "bank_statement"},
        files={"proof": ("statement.pdf", proof, "application/pdf")},
    )
    assert response.json()["decision"] == "pass"

    kept = db_session.scalars(
        select(VerificationFile).where(VerificationFile.kind == FileKind.address_proof)
    ).one()
    # The original file is what is kept, not a picture made from it.
    assert kept.content_type == "application/pdf"


@needs_db
def test_a_file_that_is_not_a_pdf_is_refused(client, api_key, monkeypatch):
    opened, token = pass_document(client, api_key, monkeypatch)
    response = client.post(
        f"/v1/sessions/{token}/address",
        data={"document_type": "utility_bill"},
        files={"proof": ("bill.docx", b"PK\x03\x04 not a document we read",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 415
    assert "must be a PDF" in response.text


@needs_db
def test_the_address_step_waits_its_turn(client, api_key, monkeypatch):
    opened = open_with_address(client, api_key).json()
    token = opened["session_url"].rsplit("/", 1)[-1]
    response = submit_address(client, token)
    assert response.status_code == 409
    assert "document" in response.text


@needs_db
def test_a_verification_that_never_asked_for_an_address_refuses_the_step(client, api_key, monkeypatch):
    opened, token = pass_document(client, api_key, monkeypatch, services=["document"])
    assert submit_address(client, token).status_code == 422


# A PDF that starts like one but cannot be opened (no pages, damaged) used to come back
# as HTTP 500 from the address step.
def _blank_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    canvas.Canvas(buffer, pagesize=A4).save()      # saved with no page at all
    return buffer.getvalue()


@pytest.mark.parametrize("data", [
    _blank_pdf(),
    b"%PDF-1.4\n%garbage that is not a document\n",
    b"%PDF-1.7\n" + b"\x00" * 200,
])
def test_a_pdf_that_cannot_be_opened_is_sent_back_for_a_retake_not_a_crash(data):
    result = scan(data)
    assert result.decision == Decision.retake
    assert "PROOF_NOT_READABLE" in result.reasons
