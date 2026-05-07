"""add_user_memories_table

Revision ID: 2d4472dd48a1
Revises: b825af502e7b
Create Date: 2026-04-10 00:30:06.573953

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2d4472dd48a1'
down_revision: Union[str, Sequence[str], None] = 'b825af502e7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create user_memories table (owned by kebi repo via Alembic)
    # Note: users and user_settings tables are managed by NestJS/TypeORM, not Alembic
    op.create_table('user_memories',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('memory', sa.Text(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'memory', name='uq_user_memories_user_memory')
    )
    op.create_index(op.f('ix_user_memories_user_id'), 'user_memories', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Drop user_memories table only (users/user_settings managed by NestJS/TypeORM)
    op.drop_index(op.f('ix_user_memories_user_id'), table_name='user_memories')
    op.drop_table('user_memories')
