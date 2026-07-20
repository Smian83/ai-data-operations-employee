"""SQLAlchemy ORM models. Import all models here so Base.metadata sees them
(required for Alembic autogenerate and Base.metadata.create_all())."""
from app.models.organization import Organization  # noqa: F401
from app.models.user import User  # noqa: F401
