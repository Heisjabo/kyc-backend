"""Declarative base and the column types every table repeats.

Constraint names are generated from a naming convention so that Alembic can
refer to an index or a foreign key by name in a downgrade; Postgres names them
itself otherwise, and those names differ between databases.
"""

from datetime import datetime
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Plain counting numbers. Whole ids get read out, compared by eye and typed into
# support tickets, and a 36-character uuid is miserable for all three.
#
# They are enumerable, so nothing a client or a browser sees is one of these:
# a verification travels as its random `reference`, a person as their
# `customer_id`, and a session as its `session_token`. The integers stay inside
# the database.
IntPk = Annotated[int, mapped_column(BigInteger, primary_key=True, autoincrement=True)]

# now() is the database's clock, not the application server's: rows written by
# different processes have to be comparable.
CreatedAt = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False),
]
UpdatedAt = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
]
Timestamp = Annotated[datetime, mapped_column(DateTime(timezone=True))]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
