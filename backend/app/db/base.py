"""
SQLAlchemy declarative base.

All ORM models must inherit from `Base` so that Alembic autogenerate and
`Base.metadata.create_all()` can discover them.
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
