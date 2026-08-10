"""add local code workspaces

Revision ID: 20260808_01
Revises:
Create Date: 2026-08-08
"""
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_01"
down_revision: Optional[str] = None
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def _create_workspaces() -> None:
    op.create_table(
        "ff_workspaces",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("repository_kind", sa.String(length=20), nullable=False),
        sa.Column("git_root", sa.Text(), nullable=True),
        sa.Column("branch", sa.String(length=200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "root_path"),
    )
    op.create_index("ix_ff_workspaces_tenant_id", "ff_workspaces", ["tenant_id"])
    op.create_index("ix_ff_workspaces_active", "ff_workspaces", ["active"])


def _create_code_runs() -> None:
    op.create_table(
        "ff_code_runs",
        sa.Column("code_run_id", sa.String(length=64), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("isolation_kind", sa.String(length=20), nullable=True),
        sa.Column("isolated_path", sa.Text(), nullable=True),
        sa.Column("base_revision", sa.String(length=200), nullable=True),
        sa.Column("baseline_manifest", sa.JSON(), nullable=False),
        sa.Column("applied_manifest", sa.JSON(), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("changed_paths", sa.JSON(), nullable=False),
        sa.Column("diff_ref", sa.Text(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["ff_workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["ff_chat_threads.chat_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index("ix_ff_code_runs_workspace_id", "ff_code_runs", ["workspace_id"])
    op.create_index("ix_ff_code_runs_session_id", "ff_code_runs", ["session_id"])
    op.create_index("ix_ff_code_runs_status", "ff_code_runs", ["status"])


def _create_tool_calls() -> None:
    op.create_table(
        "ff_tool_calls",
        sa.Column("tool_call_id", sa.String(length=64), primary_key=True),
        sa.Column("code_run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=60), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("result_excerpt", sa.Text(), nullable=False),
        sa.Column("affected_paths", sa.JSON(), nullable=False),
        sa.Column("diff_summary", sa.JSON(), nullable=False),
        sa.Column("before_hashes", sa.JSON(), nullable=False),
        sa.Column("after_hashes", sa.JSON(), nullable=False),
        sa.Column("unified_diff", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["code_run_id"], ["ff_code_runs.code_run_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("code_run_id", "step_no"),
    )
    op.create_index("ix_ff_tool_calls_code_run_id", "ff_tool_calls", ["code_run_id"])
    op.create_index("ix_ff_tool_calls_session_id", "ff_tool_calls", ["session_id"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Older releases used SQLAlchemy ``create_all`` without Alembic. A fresh
    # database can install the full current schema in one revision.
    if "ff_chat_threads" not in tables:
        from faraflow.infra.database import Base

        Base.metadata.create_all(bind=bind)
        return

    # The checks also recover if the new application was started once before
    # this migration: create_all may have made the new tables but cannot add
    # the two columns to existing chat tables.
    if "ff_workspaces" not in tables:
        _create_workspaces()
    if "ff_code_runs" not in tables:
        _create_code_runs()
    if "ff_tool_calls" not in tables:
        _create_tool_calls()

    inspector = sa.inspect(bind)
    tool_columns = {item["name"] for item in inspector.get_columns("ff_tool_calls")}
    missing_tool_columns = {
        "before_hashes": sa.Column(
            "before_hashes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        "after_hashes": sa.Column(
            "after_hashes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        "unified_diff": sa.Column(
            "unified_diff", sa.Text(), nullable=False, server_default=""
        ),
    }
    for name, column in missing_tool_columns.items():
        if name not in tool_columns:
            with op.batch_alter_table("ff_tool_calls") as batch:
                batch.add_column(column)

    inspector = sa.inspect(bind)
    chat_columns = {item["name"] for item in inspector.get_columns("ff_chat_threads")}
    if "workspace_id" not in chat_columns:
        with op.batch_alter_table("ff_chat_threads") as batch:
            batch.add_column(sa.Column("workspace_id", sa.String(length=64), nullable=True))
            batch.create_foreign_key(
                "fk_ff_chat_threads_workspace_id",
                "ff_workspaces",
                ["workspace_id"],
                ["workspace_id"],
                ondelete="SET NULL",
            )

    inspector = sa.inspect(bind)
    message_columns = {item["name"] for item in inspector.get_columns("ff_chat_messages")}
    if "code_run_id" not in message_columns:
        with op.batch_alter_table("ff_chat_messages") as batch:
            batch.add_column(sa.Column("code_run_id", sa.String(length=64), nullable=True))
            batch.create_foreign_key(
                "fk_ff_chat_messages_code_run_id",
                "ff_code_runs",
                ["code_run_id"],
                ["code_run_id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_constraint("fk_ff_chat_messages_code_run_id", type_="foreignkey")
        batch.drop_column("code_run_id")
    with op.batch_alter_table("ff_chat_threads") as batch:
        batch.drop_constraint("fk_ff_chat_threads_workspace_id", type_="foreignkey")
        batch.drop_column("workspace_id")
    op.drop_table("ff_tool_calls")
    op.drop_table("ff_code_runs")
    op.drop_table("ff_workspaces")
