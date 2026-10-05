"""Due diligence forms: finding the one a verification uses, and checking answers.

The questions are written in the admin console and stored on the form. A client
attaches one to a verification by sending its id as `form_id`, and may only use
forms they have been given. The person answers on the session link, and the
answers are checked here against the form's own questions: a required question
left blank, a choice that is not one of the options, or a country that is not a
country is refused with the question it is about, so the page can say which.
"""

import secrets
import string
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DueDiligenceForm, DueDiligenceFormClient
from app.engines.country_codes import ALPHA2_TO_ALPHA3
from app.engines.fields import to_alpha3
from app.schemas.due_diligence import FormField

# Shufti's limit for a file in a questionnaire, which the session page states.
MAX_FILE_BYTES = 15 * 1024 * 1024
ALLOWED_FILE_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_TEXT = 2000

_ID_ALPHABET = string.ascii_letters + string.digits


def new_public_id() -> str:
    """A form id in the shape of Shufti's questionnaire uuids, a little longer."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


def by_public_id(db: Session, public_id: str) -> DueDiligenceForm | None:
    return db.scalar(select(DueDiligenceForm).where(DueDiligenceForm.public_id == public_id))


def usable_by(db: Session, form: DueDiligenceForm, account_id) -> bool:
    return db.scalar(
        select(DueDiligenceFormClient).where(
            DueDiligenceFormClient.form_id == form.id,
            DueDiligenceFormClient.account_id == account_id,
        )
    ) is not None


def forms_for(db: Session, account_id) -> list[DueDiligenceForm]:
    """The active forms a client has been given."""
    return list(
        db.scalars(
            select(DueDiligenceForm)
            .join(DueDiligenceFormClient, DueDiligenceFormClient.form_id == DueDiligenceForm.id)
            .where(DueDiligenceFormClient.account_id == account_id, DueDiligenceForm.status == "active")
            .order_by(DueDiligenceForm.name)
        ).all()
    )


def fields_of(form: DueDiligenceForm) -> list[FormField]:
    return [FormField.model_validate(field) for field in form.fields or []]


class AnswerError(ValueError):
    """An answer the form does not accept. Carries the question it is about."""

    def __init__(self, field: FormField, message: str):
        super().__init__(f"{field.label}: {message}")
        self.key = field.key


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def check_answer(field: FormField, value):
    """The answer as it will be stored, or AnswerError. Files are checked apart."""
    if _blank(value):
        if field.required:
            raise AnswerError(field, "this question is required")
        return None

    kind = field.type
    if kind in ("text", "textarea"):
        if not isinstance(value, str):
            raise AnswerError(field, "expected text")
        if len(value) > MAX_TEXT:
            raise AnswerError(field, f"keep it under {MAX_TEXT} characters")
        return value.strip()

    if kind in ("number", "amount"):
        try:
            number = Decimal(str(value).replace(",", "").strip())
        except InvalidOperation:
            raise AnswerError(field, "expected a number") from None
        if not number.is_finite():
            raise AnswerError(field, "expected a number")
        if kind == "amount" and number < 0:
            raise AnswerError(field, "an amount cannot be negative")
        # Stored as a string so 10000.50 is kept exactly as given.
        return format(number.normalize(), "f")

    if kind == "date":
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise AnswerError(field, "expected a date as YYYY-MM-DD") from None

    if kind == "select":
        if value not in field.options:
            raise AnswerError(field, "choose one of the options")
        return value

    if kind == "multi_select":
        chosen = value if isinstance(value, list) else [value]
        unknown = [choice for choice in chosen if choice not in field.options]
        if unknown:
            raise AnswerError(field, "choose from the options")
        # In the form's own order, once each.
        return [option for option in field.options if option in chosen]

    if kind == "country":
        code = str(value).strip().upper()
        if len(code) == 2 and code in ALPHA2_TO_ALPHA3:
            return code
        if to_alpha3(code) is None:
            raise AnswerError(field, "choose a country from the list")
        return code

    if kind == "yes_no":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("yes", "true", "1"):
            return True
        if text in ("no", "false", "0"):
            return False
        raise AnswerError(field, "answer yes or no")

    raise AnswerError(field, f"unknown question type {kind}")
