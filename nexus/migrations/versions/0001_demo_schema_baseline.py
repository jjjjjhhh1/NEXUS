"""Create the complete Nexus demo schema baseline.

Revision ID: 0001_demo_schema_baseline
Revises: None
"""
from alembic import op

from backend.core.database import Base
from backend.core.models import register_all_models

revision = "0001_demo_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    register_all_models()
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    register_all_models()
    Base.metadata.drop_all(bind=op.get_bind())
