"""add verification, transactional outbox, and workspace fencing

Revision ID: 20260928_05
Revises: 20260927_04
"""

from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_05"
down_revision: Optional[str] = "20260927_04"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def _create_outbox() -> None:
    op.create_table(
        "ff_event_outbox",
        sa.Column("outbox_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["ff_events.event_id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_ff_event_outbox_session_id", "ff_event_outbox", ["session_id"])
    op.create_index("ix_ff_event_outbox_published_at", "ff_event_outbox", ["published_at"])


def _create_workspace_leases() -> None:
    op.create_table(
        "ff_workspace_mutation_leases",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["ff_workspaces.workspace_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_ff_workspace_mutation_leases_expires_at",
        "ff_workspace_mutation_leases",
        ["expires_at"],
    )


def _create_verifications() -> None:
    op.create_table(
        "ff_code_verifications",
        sa.Column("verification_id", sa.String(length=64), primary_key=True),
        sa.Column("code_run_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=True),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.String(length=100), nullable=False),
        sa.Column("command_digest", sa.String(length=64), nullable=False),
        sa.Column("source_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["code_run_id"], ["ff_code_runs.code_run_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["ff_code_turns.turn_id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_ff_code_verifications_code_run_id",
        "ff_code_verifications",
        ["code_run_id"],
    )
    op.create_index(
        "ix_ff_code_verifications_review_revision",
        "ff_code_verifications",
        ["review_revision"],
    )
    op.create_index(
        "ix_ff_code_verifications_profile_id",
        "ff_code_verifications",
        ["profile_id"],
    )
    op.create_index(
        "ix_ff_code_verifications_status", "ff_code_verifications", ["status"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    run_columns = {item["name"] for item in inspector.get_columns("ff_code_runs")}
    if "lease_token" not in run_columns:
        with op.batch_alter_table("ff_code_runs") as batch:
            batch.add_column(
                sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0")
            )
    journal_columns = {
        item["name"] for item in inspector.get_columns("ff_code_apply_journals")
    }
    if "backup_hashes" not in journal_columns:
        with op.batch_alter_table("ff_code_apply_journals") as batch:
            batch.add_column(
                sa.Column(
                    "backup_hashes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
                )
            )
    if "ff_event_outbox" not in tables:
        _create_outbox()
    if "ff_workspace_mutation_leases" not in tables:
        _create_workspace_leases()
    if "ff_code_verifications" not in tables:
        _create_verifications()


def downgrade() -> None:
    op.drop_table("ff_code_verifications")
    op.drop_table("ff_workspace_mutation_leases")
    op.drop_table("ff_event_outbox")
    with op.batch_alter_table("ff_code_apply_journals") as batch:
        batch.drop_column("backup_hashes")
    with op.batch_alter_table("ff_code_runs") as batch:
        batch.drop_column("lease_token")
