"""local_workflow_source

Add ``local_workflows.source_workflow_id`` — a nullable FK back to
``automation_workflows.id`` recording which coordinator workflow a local handle
was mirrored/moved from (NULL when recorded directly on the agent). It correlates
a deployed LocalWorkflow row with its coordinator origin so the merged workflow
list can derive an ``origin`` marker (cloud / mirrored / local:<agent>) and so a
Move can be reconciled after the agent's catalog re-emit. ``ondelete=SET NULL`` so
deleting the coordinator workflow (e.g. on Move) leaves the local handle intact.

Revision ID: 0003_local_workflow_source
Revises: 0002_target_assignments
Create Date: 2026-07-02 03:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_local_workflow_source'
down_revision: Union[str, None] = '0002_target_assignments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('local_workflows', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'source_workflow_id',
                sa.Integer(),
                nullable=True,
                comment='Coordinator AutomationWorkflow this local handle was '
                        'mirrored/moved from; NULL if recorded directly on the agent.',
            )
        )
        batch_op.create_index(
            batch_op.f('ix_local_workflows_source_workflow_id'),
            ['source_workflow_id'],
            unique=False,
        )
        batch_op.create_foreign_key(
            'fk_local_workflows_source_workflow',
            'automation_workflows',
            ['source_workflow_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('local_workflows', schema=None) as batch_op:
        batch_op.drop_constraint('fk_local_workflows_source_workflow', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_local_workflows_source_workflow_id'))
        batch_op.drop_column('source_workflow_id')
