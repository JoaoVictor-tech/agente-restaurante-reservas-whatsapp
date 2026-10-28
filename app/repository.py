from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Event, Message

HISTORY_WINDOW = 20  # mesma janela de contexto do node 'Postgres Chat Memory' original


async def get_or_create_conversation(
    db: AsyncSession,
    *,
    session_id: str,
    environment: str,
    contact_id: str,
    phone: str | None,
    name: str | None,
) -> Conversation:
    conversation = await db.get(Conversation, session_id)
    if conversation is None:
        conversation = Conversation(
            session_id=session_id,
            environment=environment,
            contact_id=contact_id,
            phone=phone,
            name=name,
            slots={},
        )
        db.add(conversation)
        await db.flush()
    else:
        # mantem nome/telefone atualizados caso o contato edite o cadastro na plataforma
        conversation.contact_id = contact_id
        conversation.phone = phone or conversation.phone
        conversation.name = name or conversation.name
    return conversation


async def get_recent_history(db: AsyncSession, *, session_id: str, limit: int = HISTORY_WINDOW) -> list[dict]:
    stmt = (
        select(Message)
        .where(Message.session_id == session_id, Message.role.in_(["user", "assistant"]))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    messages = list(reversed(result.scalars().all()))
    return [{"role": message.role, "content": message.content} for message in messages]


async def save_message(
    db: AsyncSession,
    *,
    session_id: str,
    role: str,
    content: str,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
    audio_cost_usd: float = 0.0,
    audio_provider: str | None = None,
) -> Message:
    message = Message(
        session_id=session_id,
        role=role,
        content=content,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        audio_cost_usd=audio_cost_usd,
        audio_provider=audio_provider,
    )
    db.add(message)
    await db.flush()
    return message


async def set_message_audio_cost(db: AsyncSession, *, message_id: int, cost_usd: float, provider: str) -> None:
    """Atualiza o custo de audio de uma mensagem ja salva - usado pela resposta em audio
    (TTS), cujo custo so e conhecido DEPOIS que a mensagem de texto ja foi gravada (a
    sintese/envio acontecem fora da transacao de banco, ver app/pipeline.py)."""
    message = await db.get(Message, message_id)
    if message is not None:
        message.audio_cost_usd = cost_usd
        message.audio_provider = provider
        await db.flush()


async def save_event(db: AsyncSession, *, session_id: str, type_: str, payload: dict) -> Event:
    event = Event(session_id=session_id, type=type_, payload=payload)
    db.add(event)
    await db.flush()
    return event


async def mark_transferred(db: AsyncSession, conversation: Conversation) -> None:
    """Marca a conversa como transferida (exibicao/filtro no dashboard e fim do Teste Rapido).
    A decisao de transferir e' do motor v2 (app/politica.py); se o cliente escrever de novo
    depois, o agente continua respondendo normalmente, como antes."""
    conversation.transferred_at = datetime.datetime.now(datetime.timezone.utc)
    await db.flush()
