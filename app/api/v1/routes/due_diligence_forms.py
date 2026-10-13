"""Due diligence forms, for the client and for Pangolin staff.

  * **A client**, with their API key, lists the forms they have been given, to
    know which `form_id` to send when opening a verification.
  * **Console staff** write the forms: the questions, who may use each one, and
    whether it is a draft, active or archived. Owners and admins change them;
    every other console role can read them. Each change is recorded in
    audit_events with who made it.

A form is never deleted. Verifications point at it by id and their answers were
given to its questions, so retiring a form is archiving it.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import Caller, console_product, console_role, current_caller
from app.db.models import (
    Account,
    Applicant,
    AuditEvent,
    DueDiligenceForm,
    Step,
    StepStatus,
    Verification,
    VerificationStep,
)
from app.db.session import get_session
from app.schemas.due_diligence import (
    Answer,
    ClientForm,
    CreateFormRequest,
    FormClient,
    FormResponse,
    ResponseList,
    ResponseRow,
    UpdateFormRequest,
)
from app.services import due_diligence as forms

router = APIRouter(tags=["due diligence forms"])

# The console roles that may write a form.
FORM_ADMINS = ("owner", "admin")


def _name(account: Account) -> str:
    return f"{account.first_name} {account.last_name}".strip()


def _responses_30d(db: Session, public_ids: list[str]) -> dict[str, int]:
    """Per form: due diligence steps answered in the last 30 days."""
    if not public_ids:
        return {}
    rows = db.execute(
        select(Verification.form_id, func.count())
        .join(Step, Step.verification_id == Verification.id)
        .where(
            Verification.form_id.in_(public_ids),
            Step.step == VerificationStep.due_diligence,
            Step.status == StepStatus.passed,
            Step.completed_at >= datetime.now(UTC) - timedelta(days=30),
        )
        .group_by(Verification.form_id)
    ).all()
    return {form_id: count for form_id, count in rows}


def _as_response(form: DueDiligenceForm, responses: int = 0) -> FormResponse:
    return FormResponse(
        id=form.public_id,
        name=form.name,
        description=form.description,
        status=form.status,
        fields=forms.fields_of(form),
        clients=[FormClient(id=str(a.id), name=_name(a), email=a.email) for a in form.clients],
        responses_30d=responses,
        created_at=form.created_at,
        updated_at=form.updated_at,
    )


def _clients(db: Session, client_ids: list[str]) -> list[Account]:
    try:
        ids = sorted({int(value) for value in client_ids})
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "client_ids must be account ids") from None
    accounts = list(db.scalars(select(Account).where(Account.id.in_(ids))).all()) if ids else []
    missing = sorted(set(ids) - {account.id for account in accounts})
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"No client with id {', '.join(map(str, missing))}"
        )
    return accounts


def _form_or_404(db: Session, public_id: str) -> DueDiligenceForm:
    form = db.scalar(
        select(DueDiligenceForm)
        .where(DueDiligenceForm.public_id == public_id)
        .options(selectinload(DueDiligenceForm.clients))
    )
    if form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No form with that id")
    return form


def _record(db: Session, staff: Account, action: str, form: DueDiligenceForm, changed: list[str]) -> None:
    db.add(
        AuditEvent(
            event_type=f"due_diligence_form.{action}",
            actor=str(staff.id),
            payload={"form_id": form.public_id, "by": staff.email, "changed": changed},
        )
    )


# The client


@router.get("/due-diligence/forms", response_model=list[ClientForm])
def client_forms(
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> list[ClientForm]:
    """For a client, the forms it may attach to a verification by sending
    `form_id`; for Pangolin staff, every form. Each with how many times it has
    been answered on the caller's verifications."""
    given = (
        forms.forms_for(db, caller.account.id)
        if not caller.staff
        else list(db.scalars(select(DueDiligenceForm).order_by(DueDiligenceForm.name)).all())
    )
    answered = select(Verification.form_id, func.count()).join(Step, Step.verification_id == Verification.id).where(
        Verification.form_id.in_([f.public_id for f in given]),
        Step.step == VerificationStep.due_diligence,
        Step.status == StepStatus.passed,
    )
    if caller.scope is not None:
        answered = answered.where(Verification.account_id == caller.scope)
    counts = dict(db.execute(answered.group_by(Verification.form_id)).all()) if given else {}
    return [
        ClientForm(
            id=form.public_id,
            name=form.name,
            description=form.description,
            fields=forms.fields_of(form),
            status=form.status,
            responses=counts.get(form.public_id, 0),
            created_at=form.created_at,
            updated_at=form.updated_at,
        )
        for form in given
    ]


@router.get("/due-diligence/responses", response_model=ResponseList)
def client_responses(
    form_id: str | None = Query(default=None, description="Only this form's answers"),
    client_id: int | None = Query(default=None, description="Pangolin staff only: one client's"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ResponseList:
    """Submitted forms, newest first: a client's own, or every client's for staff."""
    return response_list(db, form_id=form_id, limit=limit, offset=offset, account_id=caller.scope_for(client_id))


# The console


@router.get("/console/due-diligence/forms", response_model=list[FormResponse])
def list_forms(
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> list[FormResponse]:
    every = list(
        db.scalars(
            select(DueDiligenceForm)
            .options(selectinload(DueDiligenceForm.clients))
            .order_by(DueDiligenceForm.updated_at.desc())
        ).all()
    )
    counts = _responses_30d(db, [form.public_id for form in every])
    return [_as_response(form, counts.get(form.public_id, 0)) for form in every]


@router.get("/console/due-diligence/forms/{public_id}", response_model=FormResponse)
def read_form(
    public_id: str,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> FormResponse:
    form = _form_or_404(db, public_id)
    return _as_response(form, _responses_30d(db, [form.public_id]).get(form.public_id, 0))


@router.post("/console/due-diligence/forms", response_model=FormResponse, status_code=status.HTTP_201_CREATED)
def create_form(
    body: CreateFormRequest,
    staff: Account = Depends(console_role("kyc", FORM_ADMINS)),
    db: Session = Depends(get_session),
) -> FormResponse:
    form = DueDiligenceForm(
        public_id=body.public_id or forms.new_public_id(),
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
        status=body.status,
        fields=[field.model_dump(mode="json", exclude_none=True) for field in body.fields],
        created_by_id=staff.id,
    )
    form.clients = _clients(db, body.client_ids)
    db.add(form)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"A form with id {form.public_id!r} already exists") from None
    _record(db, staff, "created", form, ["name", "description", "status", "fields", "clients"])
    db.commit()
    return _as_response(_form_or_404(db, form.public_id))


@router.patch("/console/due-diligence/forms/{public_id}", response_model=FormResponse)
def update_form(
    public_id: str,
    body: UpdateFormRequest,
    staff: Account = Depends(console_role("kyc", FORM_ADMINS)),
    db: Session = Depends(get_session),
) -> FormResponse:
    """Change what is sent; leave out what is not.

    A question's `key` is what ties old answers to it, so rewording a question
    keeps its key, and a removed question's answers stay readable on the steps
    that gave them.
    """
    form = _form_or_404(db, public_id)
    changed = sorted(body.model_fields_set)

    if body.name is not None:
        form.name = body.name.strip()
    if "description" in body.model_fields_set:
        form.description = (body.description or "").strip() or None
    if body.fields is not None:
        form.fields = [field.model_dump(mode="json", exclude_none=True) for field in body.fields]
    if body.status is not None:
        form.status = body.status
    if body.client_ids is not None:
        form.clients = _clients(db, body.client_ids)

    if form.status == "active" and not form.fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An active form needs at least one field")

    form.updated_at = datetime.now(UTC)
    _record(db, staff, "updated", form, changed)
    db.commit()
    return read_form(public_id, staff, db)


@router.get("/console/due-diligence/responses", response_model=ResponseList)
def list_responses(
    form_id: str | None = Query(default=None, description="Only this form's answers"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ResponseList:
    """Submitted forms, newest first, across every client."""
    return response_list(db, form_id=form_id, limit=limit, offset=offset)


def response_list(
    db: Session, *, form_id: str | None, limit: int, offset: int, account_id: int | None = None
) -> ResponseList:
    """Submitted forms newest first, of every client or only on one account's own
    verifications (the client dashboard)."""
    query = (
        select(Step, Verification)
        .join(Verification, Verification.id == Step.verification_id)
        .where(Step.step == VerificationStep.due_diligence, Step.completed_at.is_not(None))
    )
    if account_id is not None:
        query = query.where(Verification.account_id == account_id)
    if form_id:
        query = query.where(Verification.form_id == form_id)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(Step.completed_at.desc()).limit(limit).offset(offset)).all()

    account_ids = {verification.account_id for _, verification in rows}
    applicant_ids = {verification.applicant_id for _, verification in rows if verification.applicant_id}
    accounts = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(account_ids)))} if account_ids else {}
    applicants = (
        {a.id: a for a in db.scalars(select(Applicant).where(Applicant.id.in_(applicant_ids)))}
        if applicant_ids else {}
    )

    items = []
    for step, verification in rows:
        snapshot = (step.result or {}).get("form") or {}
        answers = step.extracted or {}
        applicant = applicants.get(verification.applicant_id)
        account = accounts.get(verification.account_id)
        items.append(
            ResponseRow(
                reference=verification.reference,
                client=_name(account) if account else None,
                applicant=(
                    " ".join(part for part in (applicant.first_name, applicant.last_name) if part) or None
                    if applicant else None
                ),
                form_id=snapshot.get("id") or verification.form_id,
                form_name=snapshot.get("name"),
                status=step.status.value,
                answers=[
                    Answer(key=field["key"], label=field["label"], type=field["type"], value=answers.get(field["key"]))
                    for field in snapshot.get("fields", [])
                ],
                submitted_at=step.completed_at,
            )
        )
    return ResponseList(items=items, total=total)
