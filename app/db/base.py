"""Declarative base and the column types every table repeats.

Constraint names are generated from a naming convention so that Alembic can
refer to an index or a foreign key by name in a downgrade; Postgres names them
itself otherwise, and those names differ between databases.
"""

import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Keys are generated in Python so the caller holds the id before the row is
# written, which is what lets a document scan name its images in one flush.
UuidPk = Annotated[uuid.UUID, mapped_column(Uuid, primary_key=True, default=uuid.uuid4)]

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
