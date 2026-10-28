"""messages.audio_cost_usd/audio_provider (custo de transcricao/sintese de audio)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("audio_cost_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("messages", sa.Column("audio_provider", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "audio_provider")
    op.drop_column("messages", "audio_cost_usd")
