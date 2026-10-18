"""Allow AI profiles to store a validated custom OpenAI-compatible endpoint."""
from alembic import op
import sqlalchemy as sa

revision = 'b3a1f9c4d2e7'
down_revision = '05b7c8123a90'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ai_profiles', sa.Column('base_url', sa.String(length=2048),
                                           nullable=False, server_default=''))


def downgrade():
    with op.batch_alter_table('ai_profiles') as batch:
        batch.drop_column('base_url')
