"""knowledge_base_versions table + app_settings.active_kb_version_id (historico/reverter
do cardapio-prompt editado em /dashboard/base-conhecimento)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-05

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_base_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_knowledge_base_versions_created_at", "knowledge_base_versions", ["created_at"]
    )
    op.add_column(
        "app_settings",
        sa.Column(
            "active_kb_version_id",
            sa.Integer(),
            sa.ForeignKey("knowledge_base_versions.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "active_kb_version_id")
    op.drop_index("ix_knowledge_base_versions_created_at", table_name="knowledge_base_versions")
    op.drop_table("knowledge_base_versions")
