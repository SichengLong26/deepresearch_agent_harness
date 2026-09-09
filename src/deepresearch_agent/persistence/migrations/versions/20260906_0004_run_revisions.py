"""Durable partial edit commands and revision snapshots."""
from alembic import op
from deepresearch_agent.persistence.models import RunEditModel, RunRevisionModel

revision = "20260906_0004"
down_revision = "20260828_0003"
branch_labels = None
depends_on = None


def upgrade():
    RunEditModel.__table__.create(op.get_bind(), checkfirst=True)
    RunRevisionModel.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    RunRevisionModel.__table__.drop(op.get_bind(), checkfirst=True)
    RunEditModel.__table__.drop(op.get_bind(), checkfirst=True)
