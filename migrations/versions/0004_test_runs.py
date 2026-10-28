"""test_runs (tela Teste Rapido do dashboard)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-29

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("sender_phone", sa.String(), nullable=False),
        sa.Column("receiver_phone", sa.String(), nullable=False),
        sa.Column("activation_code", sa.String(), nullable=False),
        sa.Column("persona_prompt", sa.Text(), nullable=False),
        sa.Column("target_session_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("turns_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("test_runs")
