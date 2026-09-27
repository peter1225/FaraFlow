"""add multi-turn code collaboration and versioned reviews

Revision ID: 20260927_03
Revises: 20260810_02
"""

from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_03"
down_revision: Optional[str] = "20260810_02"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def _create_turns() -> None:
    op.create_table(
        "ff_code_turns",
        sa.Column("turn_id", sa.String(length=64), primary_key=True),
        sa.Column("code_run_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["code_run_id"], ["ff_code_runs.code_run_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("code_run_id", "ordinal"),
    )
    op.create_index("ix_ff_code_turns_code_run_id", "ff_code_turns", ["code_run_id"])
    op.create_index("ix_ff_code_turns_status", "ff_code_turns", ["status"])


def _create_reviews() -> None:
    op.create_table(
        "ff_code_review_revisions",
        sa.Column("review_id", sa.String(length=64), primary_key=True),
        sa.Column("code_run_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("changed_paths", sa.JSON(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("diff_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["code_run_id"], ["ff_code_runs.code_run_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("code_run_id", "revision"),
    )
    op.create_index(
        "ix_ff_code_review_revisions_code_run_id",
        "ff_code_review_revisions",
        ["code_run_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    run_columns = {item["name"] for item in inspector.get_columns("ff_code_runs")}
    additions = {
        "engine": sa.Column(
            "engine", sa.String(length=30), nullable=False, server_default="native"
        ),
        "model": sa.Column("model", sa.String(length=300), nullable=False, server_default=""),
        "review_revision": sa.Column(
            "review_revision", sa.Integer(), nullable=False, server_default="0"
        ),
        "review_manifest": sa.Column(
            "review_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    }
    for name, column in additions.items():
        if name not in run_columns:
            with op.batch_alter_table("ff_code_runs") as batch:
                batch.add_column(column)

    if "ff_code_turns" not in tables:
        _create_turns()
    if "ff_code_review_revisions" not in tables:
        _create_reviews()

    inspector = sa.inspect(bind)
    tool_columns = {item["name"] for item in inspector.get_columns("ff_tool_calls")}
    if "turn_id" not in tool_columns:
        with op.batch_alter_table("ff_tool_calls") as batch:
            batch.add_column(sa.Column("turn_id", sa.String(length=64), nullable=True))
            batch.create_foreign_key(
                "fk_ff_tool_calls_turn_id",
                "ff_code_turns",
                ["turn_id"],
                ["turn_id"],
                ondelete="SET NULL",
            )
            batch.create_index("ix_ff_tool_calls_turn_id", ["turn_id"])

    # Existing runs become one historical turn without changing their state.
    rows = list(bind.execute(sa.text(
        "SELECT code_run_id, instruction, status, created_at, started_at, finished_at "
        "FROM ff_code_runs WHERE code_run_id NOT IN "
        "(SELECT code_run_id FROM ff_code_turns)"
    )).mappings())
    for row in rows:
        status = "QUEUED" if row["status"] == "CREATED" else "COMPLETED"
        bind.execute(
            sa.text(
                "INSERT INTO ff_code_turns "
                "(turn_id, code_run_id, ordinal, instruction, status, summary, error, "
                "created_at, started_at, finished_at) "
                "VALUES (:turn_id, :run_id, 1, :instruction, :status, NULL, NULL, "
                ":created_at, :started_at, :finished_at)"
            ),
            {
                "turn_id": f"turn_migrated_{row['code_run_id']}",
                "run_id": row["code_run_id"],
                "instruction": row["instruction"],
                "status": status,
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            },
        )


def downgrade() -> None:
    with op.batch_alter_table("ff_tool_calls") as batch:
        batch.drop_index("ix_ff_tool_calls_turn_id")
        batch.drop_constraint("fk_ff_tool_calls_turn_id", type_="foreignkey")
        batch.drop_column("turn_id")
    op.drop_table("ff_code_review_revisions")
    op.drop_table("ff_code_turns")
    with op.batch_alter_table("ff_code_runs") as batch:
        batch.drop_column("review_manifest")
        batch.drop_column("review_revision")
        batch.drop_column("model")
        batch.drop_column("engine")
