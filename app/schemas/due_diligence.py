"""Due diligence forms: the questions, and what comes back.

A form is a list of fields. Each field has a `key` that stays the same when its
wording is edited, so answers can be read across versions of a form, and a
`type` that decides how the person answers and how the answer is checked.
"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal[
    "text",          # one line
    "textarea",      # a paragraph
    "number",
    "amount",        # a number that cannot be negative
    "date",          # YYYY-MM-DD
    "select",        # one of `options`
    "multi_select",  # one or more of `options`
    "country",       # ISO 3166 code, from the browser's own list
    "yes_no",
    "file",          # an image or a PDF
]
CHOICE_TYPES = ("select", "multi_select")

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


class FormField(BaseModel):
    key: str = Field(description="Stays the same when the wording changes: lower case, digits and _")
    label: str = Field(min_length=1, max_length=500)
    type: FieldType
    required: bool = True
    help: str | None = Field(default=None, max_length=1000)
    placeholder: str | None = Field(default=None, max_length=200)
    options: list[str] = Field(default_factory=list, description="The choices, for select and multi_select")
    # For a text question: the exact word the person has to type, such as
    # "confirm" on Shufti's passport confirmation. Compared ignoring case.
    expected: str | None = Field(default=None, max_length=100)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY.match(value):
            raise ValueError("key must start with a letter and use only lower case letters, digits and _")
        return value

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str]) -> list[str]:
        cleaned = [option.strip() for option in value if option and option.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("options must not repeat")
        return cleaned

    @model_validator(mode="after")
    def _choices_need_options(self):
        if self.type in CHOICE_TYPES and not self.options:
            raise ValueError(f"{self.label!r} is a {self.type} and needs at least one option")
        if self.type not in CHOICE_TYPES:
            self.options = []
        if self.expected is not None:
            self.expected = self.expected.strip() or None
        if self.type != "text":
            self.expected = None
        return self


def check_fields(fields: list[FormField]) -> list[FormField]:
    keys = [field.key for field in fields]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(f"field keys must be unique: {', '.join(duplicated)}")
    return fields


class FormBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["draft", "active", "archived"] = "draft"
    fields: list[FormField] = Field(default_factory=list, max_length=100)
    client_ids: list[str] = Field(default_factory=list, description="Accounts allowed to attach this form")

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: list[FormField]) -> list[FormField]:
        return check_fields(value)

    @model_validator(mode="after")
    def _active_needs_questions(self):
        if self.status == "active" and not self.fields:
            raise ValueError("An active form needs at least one field")
        return self


class CreateFormRequest(FormBody):
    public_id: str | None = Field(
        default=None,
        description="What clients send as form_id. Generated when left out, and never changes.",
    )

    @field_validator("public_id")
    @classmethod
    def _public_id(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLIC_ID.match(value):
            raise ValueError("public_id is 3 to 64 letters, digits, _ or -")
        return value


class UpdateFormRequest(BaseModel):
    """Only what is sent is changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["draft", "active", "archived"] | None = None
    fields: list[FormField] | None = Field(default=None, max_length=100)
    client_ids: list[str] | None = None

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: list[FormField] | None) -> list[FormField] | None:
        return check_fields(value) if value is not None else None


class FormClient(BaseModel):
    id: str
    name: str
    email: str


class FormResponse(BaseModel):
    """A form as the console sees it."""

    id: str = Field(description="The form_id clients send")
    name: str
    description: str | None
    status: str
    fields: list[FormField]
    clients: list[FormClient]
    responses_30d: int = 0
    created_at: datetime
    updated_at: datetime


class ClientForm(BaseModel):
    """A form as a client sees it: enough to choose a form_id."""

    id: str
    name: str
    description: str | None
    fields: list[FormField]
    status: str = "active"
    # Answers given to it on the caller's verifications (every client's, for staff).
    responses: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionForm(BaseModel):
    """What the person is asked, on the session link."""

    form_id: str
    name: str
    description: str | None
    fields: list[FormField]
    max_file_bytes: int


class Answer(BaseModel):
    key: str
    label: str
    type: str
    value: Any


class DueDiligenceResult(BaseModel):
    form_id: str
    name: str
    status: str
    answers: list[Answer]


class ResponseRow(BaseModel):
    """One submitted form, for the console's list of answers."""

    reference: str
    client: str | None
    applicant: str | None
    form_id: str | None
    form_name: str | None
    status: str
    answers: list[Answer]
    submitted_at: datetime | None


class ResponseList(BaseModel):
    items: list[ResponseRow]
    total: int
