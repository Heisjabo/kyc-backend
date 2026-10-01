"""Give someone access to the admin console (pangolin-admin), from the command line.

There is no screen for this yet, and the first owner has to come from somewhere.
Run it with the environment of the database you mean (DATABASE_URL), for example
on the production server:

    python -m app.scripts.console_admin create --email you@tuesday.africa \\
        --first-name Jean --last-name Mugisha --role owner --products kyc,sign

    python -m app.scripts.console_admin grant --email someone@tuesday.africa \\
        --role reviewer --products kyc

    python -m app.scripts.console_admin revoke --email someone@tuesday.africa

`create` opens a new account and asks for its password without echoing it.
`grant` gives an existing account a console role. `revoke` takes it away again.

Before writing anything it prints the database host and waits for you to type it
back, so a command meant for one database is not run against another by mistake.
"""

import argparse
import getpass
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.db.models import CONSOLE_PRODUCTS, CONSOLE_ROLES, Account


class ConsoleAdminError(ValueError):
    """Something about the request is wrong; the message says what."""


def _products(value: str) -> list[str]:
    products = sorted({p.strip().lower() for p in value.split(",") if p.strip()})
    unknown = [p for p in products if p not in CONSOLE_PRODUCTS]
    if unknown or not products:
        raise ConsoleAdminError(f"Products must be one or more of: {', '.join(CONSOLE_PRODUCTS)}")
    return products


def _role(value: str) -> str:
    role = value.strip().lower()
    if role not in CONSOLE_ROLES:
        raise ConsoleAdminError(f"Role must be one of: {', '.join(CONSOLE_ROLES)}")
    return role


def _check_password(password: str) -> None:
    if not settings.MIN_PASSWORD_LENGTH <= len(password) <= settings.MAX_PASSWORD_LENGTH:
        raise ConsoleAdminError(
            f"The password must be {settings.MIN_PASSWORD_LENGTH} to {settings.MAX_PASSWORD_LENGTH} characters"
        )


def create_console_account(
    db: Session,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    role: str,
    products: list[str],
    phone: str,
    country_code: str,
    country: str,
    date_of_birth: date,
) -> Account:
    """A new account with a console role. Fails if the address is taken."""
    email = email.lower().strip()
    _check_password(password)
    role = _role(role)
    products = _products(",".join(products))
    if db.scalar(select(Account).where(Account.email == email)):
        raise ConsoleAdminError(f"{email} already has an account. Use `grant` to give it console access.")

    account = Account(
        email=email,
        password_hash=security.hash_password(password),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip(),
        country_code=country_code.strip(),
        country=country.strip(),
        date_of_birth=date_of_birth,
        console_role=role,
        console_products=products,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def grant_console_access(db: Session, *, email: str, role: str, products: list[str]) -> Account:
    """Give an existing account a console role and products."""
    account = db.scalar(select(Account).where(Account.email == email.lower().strip()))
    if account is None:
        raise ConsoleAdminError(f"No account with the address {email}")
    account.console_role = _role(role)
    account.console_products = _products(",".join(products))
    db.commit()
    db.refresh(account)
    return account


def revoke_console_access(db: Session, *, email: str) -> Account:
    """Take an account's console role away. The account itself stays."""
    account = db.scalar(select(Account).where(Account.email == email.lower().strip()))
    if account is None:
        raise ConsoleAdminError(f"No account with the address {email}")
    account.console_role = None
    account.console_products = []
    db.commit()
    db.refresh(account)
    return account


def _confirm_database() -> bool:
    url = make_url(settings.DATABASE_URL)
    target = f"{url.host}/{url.database}"
    print(f"This will write to the database at {target}.")
    typed = input("Type the host to continue: ").strip()
    return typed == url.host


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.console_admin", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="open a new account with console access")
    create.add_argument("--email", required=True)
    create.add_argument("--first-name", required=True)
    create.add_argument("--last-name", required=True)
    create.add_argument("--role", required=True, help=", ".join(CONSOLE_ROLES))
    create.add_argument("--products", required=True, help="comma separated: " + ", ".join(CONSOLE_PRODUCTS))
    create.add_argument("--phone", required=True)
    create.add_argument("--country-code", required=True, help="dialling code, e.g. +250")
    create.add_argument("--country", required=True)
    create.add_argument("--date-of-birth", required=True, type=date.fromisoformat, help="YYYY-MM-DD")

    grant = sub.add_parser("grant", help="give an existing account console access")
    grant.add_argument("--email", required=True)
    grant.add_argument("--role", required=True, help=", ".join(CONSOLE_ROLES))
    grant.add_argument("--products", required=True, help="comma separated: " + ", ".join(CONSOLE_PRODUCTS))

    revoke = sub.add_parser("revoke", help="take console access away")
    revoke.add_argument("--email", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    password = None
    if args.command == "create":
        password = getpass.getpass("Password for the new account: ")
        if getpass.getpass("Same password again: ") != password:
            print("The two passwords are different. Nothing was written.", file=sys.stderr)
            return 1

    if not _confirm_database():
        print("That is not the host. Nothing was written.", file=sys.stderr)
        return 1

    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            if args.command == "create":
                account = create_console_account(
                    db,
                    email=args.email,
                    password=password,
                    first_name=args.first_name,
                    last_name=args.last_name,
                    role=args.role,
                    products=args.products.split(","),
                    phone=args.phone,
                    country_code=args.country_code,
                    country=args.country,
                    date_of_birth=args.date_of_birth,
                )
            elif args.command == "grant":
                account = grant_console_access(db, email=args.email, role=args.role, products=args.products.split(","))
            else:
                account = revoke_console_access(db, email=args.email)
    except ConsoleAdminError as error:
        print(f"{error}. Nothing was written.", file=sys.stderr)
        return 1

    if account.console_role:
        print(f"{account.email} can sign in to the console as {account.console_role}, for {', '.join(account.console_products)}.")
    else:
        print(f"{account.email} no longer has console access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
