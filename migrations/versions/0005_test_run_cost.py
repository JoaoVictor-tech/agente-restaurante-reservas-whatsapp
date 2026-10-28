"""test_runs.llm_provider/llm_model/total_cost_usd (custo por LLM nos testes)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-29

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("test_runs", sa.Column("llm_provider", sa.String(), nullable=False, server_default=""))
    op.add_column("test_runs", sa.Column("llm_model", sa.String(), nullable=False, server_default=""))
    op.add_column(
        "test_runs", sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("test_runs", "total_cost_usd")
    op.drop_column("test_runs", "llm_model")
    op.drop_column("test_runs", "llm_provider")
