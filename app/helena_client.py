"""Wrappers para os endpoints da plataforma Helena (api.helena.run / api.wts.chat).
Toda funcao recebe um `EnvironmentCredentials` (ver app/config.py) para poder operar
tanto no ambiente de teste quanto no de producao com as credenciais corretas de cada um.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app import helena_crm
from app.config import EnvironmentCredentials, get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

BUBBLE_DELAY_SECONDS = 2.0  # intervalo entre bolhas, pra parecer digitacao humana no WhatsApp


async def send_text_bubbles(*, credentials: EnvironmentCredentials, session_id: str, bubbles: list[str]) -> None:
    """Envia cada bolha como uma mensagem separada, com um pequeno intervalo entre elas."""
    url = f"{settings.helena_base_url}/chat/v1/session/{session_id}/message"
    headers = {"Authorization": credentials.message_token}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for index, bubble in enumerate(bubbles):
            if index > 0:
                await asyncio.sleep(BUBBLE_DELAY_SECONDS)
            # a API exige JSON aqui - "data=" (form-urlencoded) retorna 415.
            response = await client.post(url, headers=headers, json={"text": bubble})
            response.raise_for_status()


async def send_canned_text(*, credentials: EnvironmentCredentials, session_id: str, text: str) -> None:
    await send_text_bubbles(credentials=credentials, session_id=session_id, bubbles=[text])


async def send_audio_reply(*, credentials: EnvironmentCredentials, session_id: str, audio_bytes: bytes) -> None:
    """Replica o fluxo de resposta em audio: upload do mp3 -> registra o arquivo -> envia como
    mensagem de audio. Fluxo confirmado contra a documentacao oficial (helena.readme.io,
    2026-07-30) apos a 1a tentativa (POST no passo de init) ter retornado 415/500 - o passo de
    obter a URL de upload e GET, nao POST (POST nesse mesmo path e o passo seguinte, de
    confirmar o arquivo ja enviado - por isso vinha "ENTITY_NOT_FOUND" ao chamar como POST
    direto, sem nunca ter feito upload nenhum antes). O envio final usa o endpoint dedicado
    de audio (POST /chat/v1/send/audio), nao o endpoint generico de texto."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        upload_init = await client.get(
            f"{settings.helena_base_url}/core/v2/file",
            # "MimeType" e opcional na doc, mas sem ele o upload seguinte (PUT no S3) falha com
            # SignatureDoesNotMatch: a URL pre-assinada exige um Content-Type EXATO igual ao que
            # foi usado pra gerar a assinatura, e sem MimeType aqui o backend assina para um
            # content-type que o cliente nao tem como adivinhar. Confirmado ao vivo (2026-07-30).
            params={"Type": "AUDIO", "Name": "data.mp3", "MimeType": "audio/mpeg"},
            headers={"accept": "application/json", "Authorization": credentials.file_token},
        )
        upload_init.raise_for_status()
        init_data = upload_init.json()
        upload_url = init_data["urlUpload"]
        temp_file_id = init_data["tempFileId"]

        put_response = await client.put(upload_url, content=audio_bytes, headers={"Content-Type": "audio/mpeg"})
        put_response.raise_for_status()

        register_response = await client.post(
            f"{settings.helena_base_url}/core/v2/file",
            headers={"Authorization": credentials.file_token, "Content-Type": "application/json"},
            json={"tempFileId": temp_file_id},
        )
        register_response.raise_for_status()
        file_id = register_response.json()["id"]

        message_response = await client.post(
            f"{settings.helena_base_url}/chat/v1/send/audio",
            headers={"Authorization": credentials.message_token, "Content-Type": "application/json"},
            json={"fileIdOrUrl": file_id, "sessionId": session_id},
        )
        message_response.raise_for_status()


async def send_image_reply(*, credentials: EnvironmentCredentials, session_id: str, image_url: str) -> None:
    """Envia uma foto de produto ao cliente, referenciando diretamente a URL publica do
    catalogo (ex: images.tcdn.com.br/...) - diferente do audio, a Helena aceita a URL
    externa direto no campo "fileUrl" do endpoint generico de mensagem, sem precisar do
    fluxo de upload em 3 passos (confirmado contra o fluxo n8n original que este projeto
    substitui - a Helena mesma busca e baixa a imagem a partir da URL)."""
    url = f"{settings.helena_base_url}/chat/v1/session/{session_id}/message"
    headers = {"Authorization": credentials.message_token}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json={"fileUrl": image_url})
        response.raise_for_status()


async def list_channels(*, credentials: EnvironmentCredentials) -> list[dict]:
    """Lista os canais/numeros cadastrados na conta Helena (usado na tela de Teste Rapido do
    dashboard, para escolher canal remetente/receptor numa lista real em vez de digitar a mao).

    ATENCAO: este endpoint usa `Authorization: Bearer {token}` - diferente da convencao sem
    prefixo usada pelas demais funcoes deste arquivo (que operam sobre uma sessao ja existente).
    Confirmado ao vivo (2026-07-29) contra a conta de teste compartilhada."""
    url = f"{settings.helena_base_url}/chat/v1/channel"
    headers = {"Authorization": f"Bearer {credentials.message_token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, list) else []


async def send_as_customer(
    *, credentials: EnvironmentCredentials, from_phone: str, to_phone: str, text: str
) -> dict:
    """Envia uma mensagem de `from_phone` (canal simulando o cliente) para `to_phone` (canal do
    agente sendo testado), via `POST /chat/v1/message/send-sync` - usado pela tela de Teste
    Rapido para simular uma conversa real inteiramente dentro da Helena, sem precisar do
    WhatsApp. Numeros no formato aceito pela Helena (ex: "+55|11900000001").

    CONFIRMADO AO VIVO (2026-07-29): from=canal "cliente simulado", to=canal do agente-alvo -
    a entrega tem lag perceptivel (pode levar ate alguns minutos ate o webhook do agente-alvo
    ser chamado), entao quem chama esta funcao nao deve esperar resposta sincrona aqui."""
    url = f"{settings.helena_base_url}/chat/v1/message/send-sync"
    headers = {
        "Authorization": f"Bearer {credentials.message_token}",
        "Content-Type": "application/json",
    }
    body = {"from": from_phone, "to": to_phone, "body": {"text": text}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        return response.json()


async def tag_contact(*, credentials: EnvironmentCredentials, contact_id: str, tag_names: list[str]) -> None:
    if not tag_names:
        return
    url = f"{settings.helena_base_url}/core/v1/contact/{contact_id}/tags"
    headers = {
        "Authorization": credentials.tag_token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            headers=headers,
            json={"tagNames": tag_names, "operation": "InsertIfNotExists"},
        )
        response.raise_for_status()


async def complete_session(*, credentials: EnvironmentCredentials, session_id: str) -> None:
    """IMPORTANTE - isto NAO e o mecanismo de handoff para um time: 'concluir' move o
    atendimento para a pasta 'Concluidos' e ele sai da fila ativa - ninguem fica
    responsavel. Use `transfer_session_to_department` para encaminhar a um time humano.
    Esta funcao serve so para o caso legitimo de encerrar de vez (ex: filtro de
    integridade - cliente de ma-fe, nao ha o que transferir)."""
    url = f"{settings.helena_wts_base_url}/chat/v1/session/{session_id}/complete"
    headers = {"Authorization": credentials.message_token}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(url, headers=headers, json={"reactivateOnNewMessage": True})
        response.raise_for_status()


async def add_session_note(*, credentials: EnvironmentCredentials, session_id: str, text: str) -> None:
    """Cria uma nota interna dentro da conversa (visivel para quem assume o atendimento),
    endpoint 'Save Internal Note' (helena.readme.io/reference/post_v1-session-id-note)."""
    url = f"{settings.helena_base_url}/chat/v1/session/{session_id}/note"
    headers = {"Authorization": credentials.message_token}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json={"text": text})
        response.raise_for_status()


async def transfer_session_to_department(
    *, credentials: EnvironmentCredentials, session_id: str, department_id: str
) -> None:
    """Encaminha o atendimento para uma equipe humana (endpoint 'transferir atendimento' da
    Helena). Diferente de `complete_session`: aqui a conversa continua na fila ativa, so
    muda de equipe/departamento responsavel."""
    url = f"{settings.helena_base_url}/chat/v1/session/{session_id}/transfer"
    headers = {"Authorization": credentials.message_token}
    # "stopBotInExecution": garante que o bot pare de responder assim que a transferencia
    # e' feita (evita o agente responder de novo por cima da consultora humana caso o
    # cliente mande outra mensagem antes dela assumir). Campo extra e' ignorado pela API
    # caso nao seja suportado nesta versao do endpoint - nao quebra o comportamento atual.
    body = {"departmentId": department_id, "options": {"stopBotInExecution": True}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(url, headers=headers, json=body)
        response.raise_for_status()


def _valido(valor: str | None) -> bool:
    # [LEGADO - usado so por transfer_to_store/tag_contact abaixo, nao usados pelo
    # pipeline deste projeto] "nao identificado" era o
    # placeholder usado por uma versao anterior do fluxo quando o cliente nao
    # respondia apos varias tentativas - nao vira tag/metadata, so aparecia no
    # resumo/nota interna para o time humano saber que faltava perguntar isso.
    return bool(valor) and valor.strip().lower() not in ("nenhuma", "nenhum", "não identificado", "nao identificado")


async def transfer_to_store(
    *,
    credentials: EnvironmentCredentials,
    session_id: str,
    contact_id: str,
    contact_name: str | None,
    store: str,
    model_tag: str | None,
    reason_tag: str | None,
    resumo: str | None = None,
    nota_interna: str | None = None,
) -> dict:
    """Transfere o atendimento: etiqueta o contato (unidade/modelo/motivo do gatilho, quando
    aplicavel), cria/atualiza o card no CRM/Kanban com uma nota-resumo (ver app/helena_crm.py)
    e encaminha a conversa para a equipe humana configurada (`department_id`).

    - a unidade escolhida (`store`) vai como tag do contato e como metadata do card, ja que
      uma empresa com varias unidades pode atender por um unico WhatsApp central - sem isso o time nao sabe
      qual loja o lead escolheu.
    - o card e sempre criado/atualizado, com um resumo dos dados coletados na descricao.
    - verificamos se ja existe um card OPEN do contato e movemos para "em atendimento" em
      vez de criar outro (evita card duplicado quando o cliente volta a escrever depois).
    """
    tags = [store] if _valido(store) else []
    if _valido(model_tag):
        tags.append(model_tag)
    if _valido(reason_tag):
        tags.append(reason_tag)

    if tags:
        await tag_contact(credentials=credentials, contact_id=contact_id, tag_names=tags)

    session_note_failed = False
    if nota_interna:
        try:
            await add_session_note(credentials=credentials, session_id=session_id, text=nota_interna)
        except Exception:
            logger.exception("Falha ao criar nota interna na sessao %s", session_id)
            session_note_failed = True

    if credentials.panel_id:
        result = await helena_crm.upsert_lead_card(
            credentials=credentials,
            contact_id=contact_id,
            contact_name=contact_name,
            session_id=session_id,
            store=store if _valido(store) else None,
            tag_names=tags,
            resumo=resumo,
        )
    else:
        logger.info(
            "Ambiente '%s' sem painel de CRM configurado - pulando criacao de card para contato %s",
            credentials.environment,
            contact_id,
        )
        result = {"action": "skipped_no_panel"}

    if session_note_failed:
        result["session_note_failed"] = True

    if credentials.department_id:
        try:
            await transfer_session_to_department(
                credentials=credentials, session_id=session_id, department_id=credentials.department_id
            )
        except Exception:
            logger.exception("Falha ao encaminhar a sessao %s para a equipe", session_id)
            result["transfer_to_department_failed"] = True
    else:
        logger.warning(
            "Ambiente '%s' sem department_id configurado - o atendimento NAO foi encaminhado "
            "a nenhuma equipe (ficou so com tag/card). Configure HELENA_%s_DEPARTMENT_ID.",
            credentials.environment,
            credentials.environment.upper(),
        )
        result["transfer_to_department_skipped_no_department"] = True

    return result
