"""add local desktop control runs and approvals

Revision ID: 20260810_02
Revises: 20260808_01
"""
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_02"
down_revision: Optional[str] = "20260808_01"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def _create_desktop_tables() -> None:
    op.create_table(
        "ff_desktop_runs",
        sa.Column("desktop_run_id", sa.String(length=64), primary_key=True),
        sa.Column("chat_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("target_window_id", sa.Integer(), nullable=True),
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("target_process", sa.String(length=260), nullable=True),
        sa.Column("target_rect", sa.JSON(), nullable=False),
        sa.Column("pending_action", sa.JSON(), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("last_screenshot_ref", sa.Text(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["ff_chat_threads.chat_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index("ix_ff_desktop_runs_session_id", "ff_desktop_runs", ["session_id"])
    op.create_index("ix_ff_desktop_runs_status", "ff_desktop_runs", ["status"])

    op.create_table(
        "ff_desktop_actions",
        sa.Column("action_id", sa.String(length=64), primary_key=True),
        sa.Column("desktop_run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("action_name", sa.String(length=60), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("screenshot_before", sa.Text(), nullable=True),
        sa.Column("screenshot_after", sa.Text(), nullable=True),
        sa.Column("execution_result", sa.JSON(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["desktop_run_id"], ["ff_desktop_runs.desktop_run_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_ff_desktop_actions_desktop_run_id",
        "ff_desktop_actions",
        ["desktop_run_id"],
    )
    op.create_index("ix_ff_desktop_actions_session_id", "ff_desktop_actions", ["session_id"])

    op.create_table(
        "ff_desktop_approvals",
        sa.Column("approval_id", sa.String(length=64), primary_key=True),
        sa.Column("desktop_run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("action_summary", sa.Text(), nullable=False),
        sa.Column("risk_description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pending_payload", sa.JSON(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["desktop_run_id"], ["ff_desktop_runs.desktop_run_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_ff_desktop_approvals_desktop_run_id",
        "ff_desktop_approvals",
        ["desktop_run_id"],
    )
    op.create_index(
        "ix_ff_desktop_approvals_session_id",
        "ff_desktop_approvals",
        ["session_id"],
    )
    op.create_index("ix_ff_desktop_approvals_status", "ff_desktop_approvals", ["status"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "ff_desktop_runs" not in tables:
        _create_desktop_tables()

    inspector = sa.inspect(bind)
    message_columns = {item["name"] for item in inspector.get_columns("ff_chat_messages")}
    if "desktop_run_id" not in message_columns:
        with op.batch_alter_table("ff_chat_messages") as batch:
            batch.add_column(sa.Column("desktop_run_id", sa.String(length=64), nullable=True))
            batch.create_foreign_key(
                "fk_ff_chat_messages_desktop_run_id",
                "ff_desktop_runs",
                ["desktop_run_id"],
                ["desktop_run_id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_constraint("fk_ff_chat_messages_desktop_run_id", type_="foreignkey")
        batch.drop_column("desktop_run_id")
    op.drop_table("ff_desktop_approvals")
    op.drop_table("ff_desktop_actions")
    op.drop_table("ff_desktop_runs")
