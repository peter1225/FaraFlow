"""add durable code execution journals and ordered events

Revision ID: 20260927_04
Revises: 20260927_03
"""

from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "20260927_04"
down_revision: Optional[str] = "20260927_03"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def _add_run_columns(inspector: Inspector) -> None:
    columns = {item["name"] for item in inspector.get_columns("ff_code_runs")}
    additions = {
        "state_version": sa.Column(
            "state_version", sa.Integer(), nullable=False, server_default="0"
        ),
        "lease_owner": sa.Column("lease_owner", sa.String(length=100), nullable=True),
        "lease_expires_at": sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        "next_turn_ordinal": sa.Column(
            "next_turn_ordinal", sa.Integer(), nullable=False, server_default="2"
        ),
        "active_turn_id": sa.Column("active_turn_id", sa.String(length=64), nullable=True),
    }
    for name, column in additions.items():
        if name not in columns:
            with op.batch_alter_table("ff_code_runs") as batch:
                batch.add_column(column)
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("ff_code_runs")}
    with op.batch_alter_table("ff_code_runs") as batch:
        if "ix_ff_code_runs_lease_owner" not in indexes:
            batch.create_index("ix_ff_code_runs_lease_owner", ["lease_owner"])
        if "ix_ff_code_runs_lease_expires_at" not in indexes:
            batch.create_index("ix_ff_code_runs_lease_expires_at", ["lease_expires_at"])


def _add_tool_columns(inspector: Inspector) -> None:
    columns = {item["name"] for item in inspector.get_columns("ff_tool_calls")}
    additions = {
        "external_call_id": sa.Column(
            "external_call_id", sa.String(length=160), nullable=True
        ),
        "phase": sa.Column(
            "phase", sa.String(length=30), nullable=False, server_default="SUCCEEDED"
        ),
        "request_digest": sa.Column(
            "request_digest", sa.String(length=64), nullable=False, server_default=""
        ),
        "result_digest": sa.Column(
            "result_digest", sa.String(length=64), nullable=False, server_default=""
        ),
        "expected_after_hashes": sa.Column(
            "expected_after_hashes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        "started_at": sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        "finished_at": sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    }
    for name, column in additions.items():
        if name not in columns:
            with op.batch_alter_table("ff_tool_calls") as batch:
                batch.add_column(column)
    inspector = sa.inspect(op.get_bind())
    constraints = {
        tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints("ff_tool_calls")
    }
    if ("code_run_id", "external_call_id") not in constraints:
        with op.batch_alter_table("ff_tool_calls") as batch:
            batch.create_unique_constraint(
                "uq_ff_tool_calls_external_call", ["code_run_id", "external_call_id"]
            )
            batch.create_index("ix_ff_tool_calls_phase", ["phase"])


def _create_apply_journals() -> None:
    op.create_table(
        "ff_code_apply_journals",
        sa.Column("journal_id", sa.String(length=64), primary_key=True),
        sa.Column("code_run_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column("completed_paths", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["code_run_id"], ["ff_code_runs.code_run_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_ff_code_apply_journals_code_run_id",
        "ff_code_apply_journals",
        ["code_run_id"],
    )
    op.create_index(
        "ix_ff_code_apply_journals_workspace_id",
        "ff_code_apply_journals",
        ["workspace_id"],
    )
    op.create_index(
        "ix_ff_code_apply_journals_status", "ff_code_apply_journals", ["status"]
    )


def _upgrade_events(inspector: Inspector) -> None:
    bind = op.get_bind()
    tables = set(inspector.get_table_names())
    if "ff_events" not in tables:
        op.create_table(
            "ff_events",
            sa.Column("event_id", sa.String(length=64), primary_key=True),
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ff_events_session_id", "ff_events", ["session_id"])
        op.create_index("ix_ff_events_event_type", "ff_events", ["event_type"])
        op.create_index(
            "uq_ff_events_session_sequence",
            "ff_events",
            ["session_id", "sequence"],
            unique=True,
        )
    if "ff_event_sequences" not in tables:
        op.create_table(
            "ff_event_sequences",
            sa.Column("session_id", sa.String(length=64), primary_key=True),
            sa.Column("next_sequence", sa.Integer(), nullable=False),
        )
    if "ff_events" not in tables:
        return
    columns = {item["name"] for item in inspector.get_columns("ff_events")}
    if "sequence" not in columns:
        with op.batch_alter_table("ff_events") as batch:
            batch.add_column(
                sa.Column("sequence", sa.Integer(), nullable=False, server_default="0")
            )
    sessions = list(
        bind.execute(sa.text("SELECT DISTINCT session_id FROM ff_events")).scalars()
    )
    for session_id in sessions:
        rows = list(
            bind.execute(
                sa.text(
                    "SELECT event_id FROM ff_events WHERE session_id=:session_id "
                    "ORDER BY created_at, event_id"
                ),
                {"session_id": session_id},
            ).scalars()
        )
        for sequence, event_id in enumerate(rows, start=1):
            bind.execute(
                sa.text("UPDATE ff_events SET sequence=:sequence WHERE event_id=:event_id"),
                {"sequence": sequence, "event_id": event_id},
            )
        bind.execute(
            sa.text(
                "INSERT INTO ff_event_sequences (session_id, next_sequence) "
                "VALUES (:session_id, :sequence)"
            ),
            {"session_id": session_id, "sequence": len(rows)},
        )
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("ff_events")}
    if "uq_ff_events_session_sequence" not in indexes:
        op.create_index(
            "uq_ff_events_session_sequence",
            "ff_events",
            ["session_id", "sequence"],
            unique=True,
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _add_run_columns(inspector)
    _add_tool_columns(sa.inspect(bind))
    if "ff_code_apply_journals" not in set(sa.inspect(bind).get_table_names()):
        _create_apply_journals()
    _upgrade_events(sa.inspect(bind))


def downgrade() -> None:
    op.drop_index("uq_ff_events_session_sequence", table_name="ff_events")
    with op.batch_alter_table("ff_events") as batch:
        batch.drop_column("sequence")
    op.drop_table("ff_event_sequences")
    op.drop_table("ff_code_apply_journals")
    with op.batch_alter_table("ff_tool_calls") as batch:
        batch.drop_index("ix_ff_tool_calls_phase")
        batch.drop_constraint("uq_ff_tool_calls_external_call", type_="unique")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("expected_after_hashes")
        batch.drop_column("result_digest")
        batch.drop_column("request_digest")
        batch.drop_column("phase")
        batch.drop_column("external_call_id")
    with op.batch_alter_table("ff_code_runs") as batch:
        batch.drop_index("ix_ff_code_runs_lease_expires_at")
        batch.drop_index("ix_ff_code_runs_lease_owner")
        batch.drop_column("active_turn_id")
        batch.drop_column("next_turn_ordinal")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
        batch.drop_column("state_version")
