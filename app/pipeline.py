"""Orquestra o processamento de uma mensagem recebida do webhook, do debounce ate a resposta
enviada de volta para a plataforma e a gravacao em banco.

O turno em si (entender -> decidir -> redigir -> verificar) e' o motor v2 (app/llm.py ->
app/motor_v2.py): ele devolve o texto, a foto escolhida em codigo, se transfere e a nota
interna. Este arquivo so orquestra I/O (banco, Helena, STT/TTS) em cima dessa decisao.

Cada atendimento roda dentro de um trace do Langfuse (app/observabilidade.py) e termina com
um DESFECHO explicito; se for falha, vira o evento `falha_no_atendimento` no painel.
"""

from __future__ import annotations

import asyncio
import logging

from app import falhas_simuladas, helena_client, llm, observabilidade, repository, stt, tts
from app.config import get_environment_credentials, get_settings
from app.db import async_session
from app.debounce import enqueue_message
from app.models import Conversation
from app.settings_service import get_active_llm_settings, get_active_tts_model, get_audio_replies_enabled
from app.webhook_schemas import HelenaWebhookPayload

logger = logging.getLogger(__name__)
settings = get_settings()

FALLBACK_REPLY = "Desculpa, tive um probleminha aqui. Pode repetir, por favor? 🙏"


async def handle_incoming_webhook(payload: HelenaWebhookPayload, environment: str) -> None:
    session_id = payload.sessionId
    message_type = payload.message_type

    text = payload.aggregated_text
    stt_cost_usd = 0.0
    client_sent_audio = message_type == "AUDIO"
    if client_sent_audio:
        file = payload.lastMessage.file
        if file and file.url:
            try:
                falhas_simuladas.provocar("audio", environment)
                audio_bytes = await stt.download_audio(file.url)
                text, stt_cost_usd = await asyncio.to_thread(stt.transcribe, audio_bytes)
            except Exception:
                logger.exception("Falha ao transcrever audio da sessao %s", session_id)
                text = payload.aggregated_text or ""

    context = {
        "environment": environment,
        "contact_id": payload.contact.id,
        "phone": payload.contact.display_phonenumber or payload.contact.phonenumber,
        "name": payload.contact.first_name or payload.contact.name,
        "message_type": message_type,
        "client_sent_audio": client_sent_audio,
        # Somado (nao sobrescrito) em enqueue_message se mais de uma mensagem de audio for
        # agregada no mesmo debounce (ver app/debounce.py).
        "stt_cost_usd": stt_cost_usd,
    }

    await enqueue_message(session_id, text, context, on_flush=_process_turn)


async def _process_turn(session_id: str, aggregated_text: str, context: dict) -> None:
    """Um atendimento: da mensagem do cliente ate a resposta (ou o silencio) - inteiro dentro
    de um trace do Langfuse, com desfecho explicito. Desfecho de falha vira o evento
    `falha_no_atendimento` no painel, com o link do trace (sem o link se o Langfuse estiver
    desligado)."""
    atendimento = None
    try:
        with observabilidade.atendimento(
            session_id=session_id,
            contact_id=context.get("contact_id"),
            environment=context["environment"],
            entrada=aggregated_text,
            tipo_mensagem=context.get("message_type"),
        ) as atendimento:
            await _atender(session_id, aggregated_text, context, atendimento)
    finally:
        if atendimento is not None and atendimento.falhou:
            await _registrar_falha_no_painel(session_id, atendimento)


async def _registrar_falha_no_painel(session_id: str, atendimento: observabilidade.Atendimento) -> None:
    logger.warning("Sessao %s: atendimento terminou em %s (%s).", session_id, atendimento.desfecho, atendimento.detalhe)
    try:
        payload = {
            "desfecho": atendimento.desfecho,
            "detalhe": atendimento.detalhe,
            "avisos": atendimento.avisos,
            "trace": await atendimento.url(),
        }
        async with async_session() as db:
            await repository.save_event(db, session_id=session_id, type_="falha_no_atendimento", payload=payload)
            await db.commit()
    except Exception:
        # Ex.: o banco caiu (provavel causa da propria falha). O log acima ja registrou.
        logger.exception("Sessao %s: nao consegui gravar a falha no painel.", session_id)


async def _atender(
    session_id: str, aggregated_text: str, context: dict, atendimento: observabilidade.Atendimento
) -> None:
    environment = context["environment"]
    credentials = get_environment_credentials(environment)
    stt_cost_usd = context.get("stt_cost_usd", 0.0)
    stt_provider = "openai_whisper" if stt_cost_usd else None
    client_sent_audio = bool(context.get("client_sent_audio"))

    async with async_session() as db:
        conversation = await repository.get_or_create_conversation(
            db,
            session_id=session_id,
            environment=environment,
            contact_id=context["contact_id"],
            phone=context.get("phone"),
            name=context.get("name"),
        )

        if conversation.paused_by_human:
            await repository.save_message(
                db, session_id=session_id, role="user", content=aggregated_text,
                audio_cost_usd=stt_cost_usd, audio_provider=stt_provider,
            )
            await db.commit()
            # um humano assumiu esta conversa pelo painel - o bot fica em silencio
            atendimento.marcar(observabilidade.SILENCIO_INTENCIONAL, "humano_assumiu_pelo_painel")
            return

        history = await repository.get_recent_history(db, session_id=session_id)
        estado = dict(conversation.slots or {})
        llm_settings = await get_active_llm_settings(db)
        tts_model_id = await get_active_tts_model(db)
        audio_replies_enabled = await get_audio_replies_enabled(db)

        await repository.save_message(
            db, session_id=session_id, role="user", content=aggregated_text,
            audio_cost_usd=stt_cost_usd, audio_provider=stt_provider,
        )
        await db.commit()

    client_will_hear_audio = client_sent_audio and audio_replies_enabled
    try:
        falhas_simuladas.provocar("llm", environment)
        result = await llm.run_turn(
            history=history,
            user_message=aggregated_text,
            llm_settings=llm_settings,
            estado=estado,
            client_will_hear_audio=client_will_hear_audio,
            client_phone=context.get("phone"),
            contact_name=context.get("name"),
            session_id=session_id,
            contact_id=context.get("contact_id"),
            environment=environment,
        )
        reply_text = result.text or FALLBACK_REPLY
        image_url = result.image_url
        transferir = result.transferir
        resumo_interno = result.resumo_interno
        usage_cost = result.usage_cost
        novo_estado = result.estado
        if not result.grounding_valido:
            atendimento.avisar(f"verificacao: {result.grounding_motivo}")
    except Exception as erro:
        logger.exception("Falha no turno do LLM na sessao %s - respondendo fallback enlatado", session_id)
        atendimento.marcar(observabilidade.RESPONDIDO_COM_FALLBACK, f"{type(erro).__name__}: {erro}"[:300])
        reply_text = FALLBACK_REPLY
        image_url = None
        transferir = False
        resumo_interno = None
        usage_cost = None
        novo_estado = estado

    async with async_session() as db:
        assistant_message = await repository.save_message(
            db, session_id=session_id, role="assistant", content=reply_text,
            model=llm_settings.model if usage_cost else None,
            input_tokens=usage_cost.input_tokens if usage_cost else 0,
            output_tokens=usage_cost.output_tokens if usage_cost else 0,
            cache_read_tokens=usage_cost.cache_read_tokens if usage_cost else 0,
            cache_write_tokens=usage_cost.cache_write_tokens if usage_cost else 0,
            cost_usd=usage_cost.cost_usd if usage_cost else 0.0,
        )
        conversation = await db.get(Conversation, session_id)
        if conversation is not None:
            conversation.slots = novo_estado  # estado do motor v2 (nome, reserva em andamento...)
        await db.commit()

    # 1) foto primeiro (se houver) - regra de negocio do Bistrô Aurora: no WhatsApp o cliente sempre
    # ve a foto antes do texto. O POST so confirma que a Helena ACEITOU a fileUrl - ela repassa
    # a imagem de forma assincrona, entao sem a pausa o texto chegaria antes (confirmado ao
    # vivo em 2026-07-31). Falha na foto nao impede o texto.
    if image_url:
        try:
            with atendimento.etapa("foto", url=image_url):
                falhas_simuladas.provocar("foto", environment)
                await helena_client.send_image_reply(credentials=credentials, session_id=session_id, image_url=image_url)
            await asyncio.sleep(helena_client.BUBBLE_DELAY_SECONDS)
        except Exception as erro:
            logger.exception("Falha ao enviar foto (imageUrl=%s) na sessao %s", image_url, session_id)
            atendimento.avisar(f"foto_nao_enviada: {type(erro).__name__}")

    # 2) resposta em audio (se o cliente mandou audio e a funcionalidade esta ligada) ou texto
    try:
        with atendimento.etapa("envio", canal="audio" if client_will_hear_audio else "texto"):
            falhas_simuladas.provocar("envio", environment)
            if client_will_hear_audio and reply_text.strip():
                try:
                    audio_bytes, tts_cost_usd = await tts.synthesize(reply_text, model_id=tts_model_id)
                    await helena_client.send_audio_reply(credentials=credentials, session_id=session_id, audio_bytes=audio_bytes)
                    atendimento.registrar_voz(tts_model_id, tts_cost_usd, len(reply_text))
                    async with async_session() as db:
                        await repository.set_message_audio_cost(
                            db, message_id=assistant_message.id, cost_usd=tts_cost_usd,
                            provider=f"elevenlabs_tts:{tts_model_id}",
                        )
                        await db.commit()
                except Exception as erro_audio:
                    logger.exception("Falha ao responder em audio na sessao %s - enviando texto", session_id)
                    atendimento.avisar(f"audio_falhou_enviado_como_texto: {type(erro_audio).__name__}")
                    await helena_client.send_text_bubbles(credentials=credentials, session_id=session_id, bubbles=[reply_text])
            else:
                await helena_client.send_text_bubbles(credentials=credentials, session_id=session_id, bubbles=[reply_text])
    except Exception as erro:
        logger.exception("Falha ao enviar a resposta na sessao %s", session_id)
        atendimento.marcar(observabilidade.FALHA_ENVIO, f"{type(erro).__name__}: {erro}"[:300])
        return

    atendimento.resposta = reply_text
    atendimento.marcar(observabilidade.RESPONDIDO)

    # 3) transferencia para a equipe humana - decidida em codigo pelo motor v2
    # (app/politica.py); a nota interna tambem e' montada em codigo (motor_v2.resumo_interno).
    if transferir:
        with atendimento.etapa("transferencia"):
            falhou = await _run_transfer(
                credentials=credentials,
                session_id=session_id,
                contact_name=context.get("name"),
                resumo_interno=resumo_interno,
            )
        if falhou:
            atendimento.marcar(observabilidade.FALHA_TRANSFERENCIA, falhou)


async def _run_transfer(*, credentials, session_id: str, contact_name: str | None, resumo_interno: str | None) -> str | None:
    """Nota interna + encaminhamento para o departamento. Devolve a descricao da falha (ou
    None se deu certo)."""
    nota = resumo_interno or f"Transferencia solicitada pelo agente (cliente: {contact_name or 'não informado'})."
    falhas: list[str] = []

    async with async_session() as db:
        conversation = await db.get(Conversation, session_id)
        try:
            falhas_simuladas.provocar("transferencia", credentials.environment)
            await helena_client.add_session_note(credentials=credentials, session_id=session_id, text=nota)
        except Exception as erro:
            logger.exception("Falha ao criar nota interna na sessao %s", session_id)
            falhas.append(f"nota: {type(erro).__name__}")

        if credentials.department_id:
            try:
                await helena_client.transfer_session_to_department(
                    credentials=credentials, session_id=session_id, department_id=credentials.department_id
                )
            except Exception as erro:
                logger.exception("Falha ao encaminhar a sessao %s para a equipe humana", session_id)
                falhas.append(f"encaminhamento: {type(erro).__name__}")
        else:
            logger.warning(
                "Ambiente '%s' sem department_id configurado - a sessao %s NAO foi encaminhada a "
                "nenhuma equipe (so ficou com a nota interna). Configure HELENA_%s_DEPARTMENT_ID.",
                credentials.environment, session_id, credentials.environment.upper(),
            )
            falhas.append("sem_department_id")

        await repository.save_event(
            db, session_id=session_id, type_="transfer_failed" if falhas else "transferred",
            payload={"resumo_interno": nota},
        )
        if conversation is not None:
            await repository.mark_transferred(db, conversation)
        await db.commit()
    return ", ".join(falhas) or None
