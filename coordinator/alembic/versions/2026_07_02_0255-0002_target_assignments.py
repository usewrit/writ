"""target_assignments

Restores the target_assignments table (carved out of the self-host baseline).
The capacity-aware distributor writes one row per (target, agent); the
monitor-dispatch scheduler reads it to (re)build each agent's assign_targets
frame and keep an assignment sticky per target.

Revision ID: 0002_target_assignments
Revises: 0001_selfhost_baseline
Create Date: 2026-07-02 02:55:52

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_target_assignments'
down_revision: Union[str, None] = '0001_selfhost_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'target_assignments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False, comment='Target this assignment covers'),
        sa.Column('agent_id', sa.String(length=255), nullable=False, comment='Agent.agent_id (string) responsible for checking the target'),
        sa.Column('assigned_by', sa.String(length=64), nullable=True, comment='What produced this assignment (e.g. capacity-aware-distributor)'),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=False, comment='When the assignment was (re)written'),
        sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('target_assignments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_target_assignments_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_target_assignments_target_id'), ['target_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_target_assignments_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index('ix_target_assignments_target_agent', ['target_id', 'agent_id'], unique=True)
        batch_op.create_index('ix_target_assignments_agent', ['agent_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('target_assignments', schema=None) as batch_op:
        batch_op.drop_index('ix_target_assignments_agent')
        batch_op.drop_index('ix_target_assignments_target_agent')
        batch_op.drop_index(batch_op.f('ix_target_assignments_agent_id'))
        batch_op.drop_index(batch_op.f('ix_target_assignments_target_id'))
        batch_op.drop_index(batch_op.f('ix_target_assignments_id'))
    op.drop_table('target_assignments')
