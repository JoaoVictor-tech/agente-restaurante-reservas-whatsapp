"""app_settings.tts_model_id (modelo de voz ElevenLabs ativo, trocavel no painel)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("tts_model_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "tts_model_id")
