"""add linked read-only and isolated writer code agents

Revision ID: 20260929_06
Revises: 20260928_05
"""

from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_06"
down_revision: Optional[str] = "20260928_05"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("ff_code_runs")}
    with op.batch_alter_table("ff_code_runs") as batch:
        if "parent_code_run_id" not in columns:
            batch.add_column(sa.Column("parent_code_run_id", sa.String(64), nullable=True))
            batch.create_foreign_key(
                "fk_ff_code_runs_parent",
                "ff_code_runs",
                ["parent_code_run_id"],
                ["code_run_id"],
                ondelete="CASCADE",
            )
            batch.create_index("ix_ff_code_runs_parent_code_run_id", ["parent_code_run_id"])
        if "agent_mode" not in columns:
            batch.add_column(sa.Column("agent_mode", sa.String(30), nullable=True))
            batch.create_index("ix_ff_code_runs_agent_mode", ["agent_mode"])


def downgrade() -> None:
    with op.batch_alter_table("ff_code_runs") as batch:
        batch.drop_index("ix_ff_code_runs_agent_mode")
        batch.drop_column("agent_mode")
        batch.drop_index("ix_ff_code_runs_parent_code_run_id")
        batch.drop_constraint("fk_ff_code_runs_parent", type_="foreignkey")
        batch.drop_column("parent_code_run_id")
