"""Calculo de horario de atendimento do Bistrô Aurora Bistrô & Café.

Horario oficial (somente almoco):
- Terca a sexta: 11h as 15h
- Sabado e domingo: 11h as 16h
- Segunda-feira: fechado

Isso NUNCA e decidido pelo LLM - e um fato de calendario simples, calculado em
codigo e injetado como referencia no bloco da redacao (ver app/redacao.py:_contexto).
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

TZ_SAO_PAULO = ZoneInfo("America/Sao_Paulo")

HORARIO_ATENDIMENTO = (
    "terça a sexta das 11h às 15h; sábado e domingo das 11h às 16h "
    "(somente almoço; fechado às segundas)"
)


def dentro_horario_atendimento(agora: datetime.datetime | None = None) -> bool:
    """True se o bistro esta no horario de almoco agora (America/Sao_Paulo)."""
    momento = (agora or datetime.datetime.now(datetime.timezone.utc)).astimezone(TZ_SAO_PAULO)
    dow = momento.isoweekday()  # 1=segunda .. 7=domingo
    minutos = momento.hour * 60 + momento.minute

    if dow == 1:
        return False  # segunda fechado

    if 2 <= dow <= 5:
        return 660 <= minutos < 900  # ter-sex 11:00-15:00

    # sabado (6) e domingo (7)
    return 660 <= minutos < 960  # 11:00-16:00
