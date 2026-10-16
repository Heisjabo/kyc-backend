"""The admin console's team: who can sign in, with which role, and invitations.

Owners and admins manage the team. An admin cannot change or remove an owner,
or make anyone an owner; only an owner can. Nobody changes their own role or
access here, and the last active owner cannot be demoted, suspended or removed,
so the console is never left without one.

There is no email yet. Inviting someone makes a one-time link, valid for
INVITE_DAYS, that the inviter copies and sends. Opening it shows the invite,
and accepting it creates the account with the chosen password and signs the
person in. Only a hash of the link's token is stored, so a lost link is
replaced with a new one rather than shown again.

Every change is kept in audit_events under team.*.
"""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_console_account
from app.api.v1.routes.console_auth import _as_user
from app.core import security
from app.db.models import CONSOLE_PRODUCTS, CONSOLE_ROLES, Account, AuditEvent, ConsoleInvite
from app.db.session import get_session
from app.schemas.console import (
    ConsoleInviteAccept,
    ConsoleInviteCreated,
    ConsoleInvitePreview,
    ConsoleInviteRequest,
    ConsoleMemberUpdate,
    ConsoleTeam,
    ConsoleTeamInvite,
    ConsoleTeamMember,
    ConsoleTokenResponse,
)
from app.services.network import client_ip

router = APIRouter(prefix="/console", tags=["admin console"])

MANAGERS = ("owner", "admin")
INVITE_DAYS = 7


def team_manager(account: Account = Depends(current_console_account)) -> Account:
    if account.console_role not in MANAGERS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owners and administrators can manage the team")
    return account


def _name(account: Account | None) -> str | None:
    if account is None:
        return None
    return f"{account.first_name} {account.last_name}".strip() or account.email


def _member(account: Account) -> ConsoleTeamMember:
    return ConsoleTeamMember(
        id=str(account.id),
        email=account.email,
        first_name=account.first_name,
        last_name=account.last_name,
        role=account.console_role or "",
        products=list(account.console_products or []),
        active=account.is_active,
        last_login_at=account.last_login_at,
        created_at=account.created_at,
    )


def _invite(invite: ConsoleInvite, inviter: Account | None) -> ConsoleTeamInvite:
    return ConsoleTeamInvite(
        id=str(invite.id),
        email=invite.email,
        first_name=invite.first_name,
        last_name=invite.last_name,
        role=invite.role,
        products=list(invite.products or []),
        invited_by=_name(inviter),
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


def _record(db: Session, staff: Account | None, event_type: str, request: Request, **payload) -> None:
    db.add(
        AuditEvent(
            event_type=event_type,
            actor=str(staff.id) if staff else "system",
            payload={"by": staff.email if staff else None, "ip": client_ip(request), **payload},
        )
    )


def _check_role(role: str, staff: Account) -> str:
    role = role.strip().lower()
    if role not in CONSOLE_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Role must be one of: {', '.join(CONSOLE_ROLES)}")
    if role == "owner" and staff.console_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can make someone an owner")
    return role


def _check_products(products: list[str]) -> list[str]:
    clean = sorted({p.strip().lower() for p in products if p.strip()})
    unknown = [p for p in clean if p not in CONSOLE_PRODUCTS]
    if unknown or not clean:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Products must be one or more of: {', '.join(CONSOLE_PRODUCTS)}")
    return clean


def _pending(now: datetime):
    return (
        ConsoleInvite.accepted_at.is_(None),
        ConsoleInvite.revoked_at.is_(None),
        ConsoleInvite.expires_at > now,
    )


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, security.fingerprint(token)


def _team(db: Session) -> ConsoleTeam:
    members = db.scalars(
        select(Account).where(Account.console_role.is_not(None)).order_by(Account.is_active.desc(), Account.first_name, Account.last_name)
    ).all()
    invites = db.scalars(select(ConsoleInvite).where(*_pending(datetime.now(UTC))).order_by(ConsoleInvite.created_at.desc())).all()
    ids = {i.invited_by_id for i in invites if i.invited_by_id}
    inviters = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))} if ids else {}
    return ConsoleTeam(
        members=[_member(m) for m in members],
        invites=[_invite(i, inviters.get(i.invited_by_id)) for i in invites],
    )


def _active_owners(db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(Account).where(Account.console_role == "owner", Account.is_active.is_(True))
    ) or 0


# Managing the team

@router.get("/team", response_model=ConsoleTeam)
def read_team(_: Account = Depends(team_manager), db: Session = Depends(get_session)) -> ConsoleTeam:
    """Every console member, and the invitations not yet accepted."""
    return _team(db)


@router.post("/team/invites", response_model=ConsoleInviteCreated, status_code=status.HTTP_201_CREATED)
def invite(
    body: ConsoleInviteRequest,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleInviteCreated:
    """Invite someone. A pending invite to the same address is replaced."""
    email = body.email.strip().lower()
    role = _check_role(body.role, staff)
    products = _check_products(body.products)

    existing = db.scalar(select(Account).where(Account.email == email))
    if existing is not None:
        if existing.console_role:
            raise HTTPException(status.HTTP_409_CONFLICT, "This person is already on the team")
        raise HTTPException(status.HTTP_409_CONFLICT, "This address belongs to a client account, so it cannot be invited")

    now = datetime.now(UTC)
    for old in db.scalars(select(ConsoleInvite).where(ConsoleInvite.email == email, *_pending(now))):
        old.revoked_at = now

    token, token_hash = _new_token()
    row = ConsoleInvite(
        email=email,
        first_name=(body.first_name or "").strip() or None,
        last_name=(body.last_name or "").strip() or None,
        role=role,
        products=products,
        token_hash=token_hash,
        invited_by_id=staff.id,
        expires_at=now + timedelta(days=INVITE_DAYS),
    )
    db.add(row)
    _record(db, staff, "team.invited", request, email=email, role=role, products=products)
    db.commit()
    db.refresh(row)
    return ConsoleInviteCreated(invite=_invite(row, staff), token=token)


def _open_invite(db: Session, invite_id: int) -> ConsoleInvite:
    row = db.get(ConsoleInvite, invite_id)
    if row is None or row.accepted_at is not None or row.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending invite with that id")
    return row


@router.post("/team/invites/{invite_id}/link", response_model=ConsoleInviteCreated)
def new_link(
    invite_id: int,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleInviteCreated:
    """A new link for a pending or expired invite. The old link stops working."""
    row = _open_invite(db, invite_id)
    if row.role == "owner" and staff.console_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can manage an invite to be an owner")
    token, row.token_hash = _new_token()
    row.expires_at = datetime.now(UTC) + timedelta(days=INVITE_DAYS)
    _record(db, staff, "team.invite_link_reset", request, email=row.email)
    db.commit()
    db.refresh(row)
    return ConsoleInviteCreated(invite=_invite(row, db.get(Account, row.invited_by_id) if row.invited_by_id else None), token=token)


@router.delete("/team/invites/{invite_id}", response_model=ConsoleTeam)
def revoke_invite(
    invite_id: int,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleTeam:
    """Cancel an invite. Its link stops working."""
    row = _open_invite(db, invite_id)
    if row.role == "owner" and staff.console_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can manage an invite to be an owner")
    row.revoked_at = datetime.now(UTC)
    _record(db, staff, "team.invite_revoked", request, email=row.email)
    db.commit()
    return _team(db)


def _other_member(db: Session, member_id: int, staff: Account) -> Account:
    member = db.get(Account, member_id)
    if member is None or not member.console_role:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No team member with that id")
    if member.id == staff.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot change your own role or access")
    if member.console_role == "owner" and staff.console_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can change another owner")
    return member


def _keep_an_owner(db: Session, member: Account) -> None:
    if member.console_role == "owner" and member.is_active and _active_owners(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "The console needs at least one active owner")


@router.patch("/team/members/{member_id}", response_model=ConsoleTeamMember)
def update_member(
    member_id: int,
    body: ConsoleMemberUpdate,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleTeamMember:
    """Change a member's role or products, or suspend or reactivate them."""
    member = _other_member(db, member_id, staff)
    before = {"role": member.console_role, "products": list(member.console_products or []), "active": member.is_active}

    role = _check_role(body.role, staff) if body.role is not None else member.console_role
    if (role != member.console_role or body.active is False) and member.console_role == "owner":
        _keep_an_owner(db, member)
    if body.role is not None:
        member.console_role = role
    if body.products is not None:
        member.console_products = _check_products(body.products)
    if body.active is not None:
        member.is_active = body.active

    after = {"role": member.console_role, "products": list(member.console_products or []), "active": member.is_active}
    if after != before:
        _record(db, staff, "team.member_updated", request, email=member.email, before=before, after=after)
    db.commit()
    db.refresh(member)
    return _member(member)


@router.delete("/team/members/{member_id}", response_model=ConsoleTeam)
def remove_member(
    member_id: int,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleTeam:
    """Take away console access. The account and everything it did are kept."""
    member = _other_member(db, member_id, staff)
    _keep_an_owner(db, member)
    _record(db, staff, "team.member_removed", request, email=member.email, role=member.console_role)
    member.console_role = None
    member.console_products = []
    db.commit()
    return _team(db)


# Accepting an invite: the link's own endpoints, used before the person has an account.

def _by_token(db: Session, token: str) -> ConsoleInvite:
    row = db.scalar(select(ConsoleInvite).where(ConsoleInvite.token_hash == security.fingerprint(token)))
    if row is None or row.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link is not valid. Ask for a new one.")
    if row.accepted_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This invite has already been used. Sign in instead.")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "This invite link has expired. Ask for a new one.")
    return row


@router.get("/invites/{token}", response_model=ConsoleInvitePreview)
def preview_invite(token: str, db: Session = Depends(get_session)) -> ConsoleInvitePreview:
    row = _by_token(db, token)
    inviter = db.get(Account, row.invited_by_id) if row.invited_by_id else None
    return ConsoleInvitePreview(
        email=row.email,
        first_name=row.first_name,
        last_name=row.last_name,
        role=row.role,
        products=list(row.products or []),
        invited_by=_name(inviter),
        expires_at=row.expires_at,
    )


@router.post("/invites/{token}/accept", response_model=ConsoleTokenResponse)
def accept_invite(
    token: str,
    body: ConsoleInviteAccept,
    request: Request,
    db: Session = Depends(get_session),
) -> ConsoleTokenResponse:
    """Create the account the invite is for, and sign the person in."""
    row = _by_token(db, token)
    if db.scalar(select(Account).where(Account.email == row.email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this address already exists. Ask the person who invited you.")

    now = datetime.now(UTC)
    account = Account(
        email=row.email,
        password_hash=security.hash_password(body.password),
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        phone=body.phone.strip(),
        country_code=body.country_code.strip(),
        country=body.country.strip(),
        date_of_birth=body.date_of_birth,
        console_role=row.role,
        console_products=list(row.products or []),
        last_login_at=now,
    )
    db.add(account)
    db.flush()
    row.accepted_at = now
    row.account_id = account.id
    _record(db, account, "team.invite_accepted", request, email=row.email, role=row.role, products=list(row.products or []))
    db.commit()
    db.refresh(account)

    access, seconds = security.issue_console_token(
        str(account.id), account.email, account.console_role, list(account.console_products or []),
        account.token_version,
    )
    return ConsoleTokenResponse(access_token=access, expires_in=seconds, user=_as_user(account))
