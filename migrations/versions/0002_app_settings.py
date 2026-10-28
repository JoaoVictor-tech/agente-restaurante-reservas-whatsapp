"""app_settings table

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_provider", sa.String(), nullable=False, server_default="anthropic"),
        sa.Column("active_model", sa.String(), nullable=False, server_default="claude-sonnet-5"),
        sa.Column("provider_api_keys_encrypted", sa.Text(), nullable=True),
        sa.Column("system_prompt_static", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
