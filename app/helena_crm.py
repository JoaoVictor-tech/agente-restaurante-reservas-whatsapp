"""Integracao com o CRM/Kanban nativo da plataforma Helena, conforme a documentacao
publica dela (helena.readme.io / docs.helena.app).

IMPORTANTE - configuracao obrigatoria por ambiente (ver .env.example):
- HELENA_{PROD,TEST}_PANEL_ID: id do painel (kanban) de vendas.
- HELENA_{PROD,TEST}_STEP_ID_NOVO_LEAD: id da etapa onde um card novo deve entrar.
- HELENA_{PROD,TEST}_STEP_ID_EM_ATENDIMENTO: id da etapa para onde mover um card ja existente.
Use `python scripts/list_helena_panels.py teste|producao` para descobrir esses ids.

NOTA sobre autenticacao: a documentacao oficial especifica o formato
`Authorization: Bearer {token}` para a API de CRM - diferente dos endpoints de
chat/tag (token puro, sem "Bearer"), ver app/helena_client.py.
"""

from __future__ import annotations

import logging

import httpx

from app.config import EnvironmentCredentials, get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CRM_BASE_URL = "https://api.helena.run/crm"


def _auth_headers(credentials: EnvironmentCredentials) -> dict:
    return {
        "Authorization": f"Bearer {credentials.message_token}",
        "Content-Type": "application/json",
    }


async def list_panels(credentials: EnvironmentCredentials, include_steps: bool = True) -> list[dict]:
    """Lista os paineis disponiveis - usado pelo script de descoberta de IDs."""
    params = {"IncludeDetails": "Steps"} if include_steps else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{CRM_BASE_URL}/v2/panel", headers=_auth_headers(credentials), params=params)
        response.raise_for_status()
        data = response.json()
        return data.get("items") or data.get("data") or data.get("results") or data


async def find_open_card(*, credentials: EnvironmentCredentials, contact_id: str, panel_id: str) -> dict | None:
    """Retorna o card OPEN mais recente do contato neste painel, se existir."""
    params = {
        "PanelId": panel_id,
        "ContactId": contact_id,
        "Statuses": "OPEN",
        "PageSize": 1,
        "OrderBy": "CreatedAt",
        "OrderDirection": "DESCENDING",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{CRM_BASE_URL}/v2/panel/card", headers=_auth_headers(credentials), params=params)
        response.raise_for_status()
        data = response.json()
        items = data.get("items") or data.get("data") or data.get("results") or []
        return items[0] if items else None


async def create_card(
    *,
    credentials: EnvironmentCredentials,
    panel_id: str,
    step_id: str,
    title: str,
    contact_id: str,
    session_id: str | None,
    tag_names: list[str],
    metadata: dict | None = None,
    description: str | None = None,
) -> dict:
    payload = {
        "stepId": step_id,
        "title": title,
        "contactIds": [contact_id],
        "tagNames": tag_names,
        "metadata": metadata or {},
    }
    if session_id:
        payload["sessionId"] = session_id
    if description:
        payload["description"] = description

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{CRM_BASE_URL}/v2/panel/card", headers=_auth_headers(credentials), json=payload)
        response.raise_for_status()
        return response.json()


async def move_card_to_step(
    *, credentials: EnvironmentCredentials, card_id: str, step_id: str, description: str | None = None
) -> dict:
    """ATENCAO: o v3 desta API exige uma mascara `fields` listando quais propriedades devem
    ser aplicadas - so mandar `stepId` no corpo (sem a mascara) retorna 500 'Nenhum campo
    informado para atualizacao' (padrao confirmado no projeto irmao/Furtil). Formato
    correto: {"fields": ["StepId", ...], "stepId": ..., "description": ...}."""
    fields = ["StepId"]
    payload: dict = {"fields": fields, "stepId": step_id}
    if description:
        # anexa o resumo mais recente tambem quando o atendimento reabre (card ja existia)
        fields.append("Description")
        payload["description"] = description
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(
            f"{CRM_BASE_URL}/v3/panel/card/{card_id}",
            headers=_auth_headers(credentials),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def upsert_lead_card(
    *,
    credentials: EnvironmentCredentials,
    contact_id: str,
    contact_name: str | None,
    session_id: str | None,
    store: str | None,
    tag_names: list[str],
    resumo: str | None = None,
) -> dict:
    """Cria um card novo para o lead, ou move um card ja existente para 'em
    atendimento'. `resumo` vira a descricao/nota do card com os dados coletados
    na conversa, para o time humano nao comecar do zero.

    `tag_names` vem PRONTO de `transfer_to_store` (a mesma lista aplicada como tag do
    contato) em vez de recalculado aqui a partir dos campos crus, para as duas listas
    nunca ficarem dessincronizadas."""

    if not credentials.panel_id or not credentials.step_id_novo_lead or not credentials.step_id_em_atendimento:
        raise RuntimeError(
            f"Painel/etapas de CRM nao configurados para o ambiente '{credentials.environment}' - "
            "rode scripts/list_helena_panels.py e preencha o .env."
        )

    existing = await find_open_card(credentials=credentials, contact_id=contact_id, panel_id=credentials.panel_id)

    if existing:
        card_id = existing.get("id")
        await move_card_to_step(
            credentials=credentials, card_id=card_id, step_id=credentials.step_id_em_atendimento, description=resumo
        )
        logger.info("Card existente %s do contato %s movido para 'em atendimento'", card_id, contact_id)
        return {"action": "moved", "card_id": card_id}

    title = f"{contact_name or 'Cliente'} - {store or 'unidade nao informada'}"
    created = await create_card(
        credentials=credentials,
        panel_id=credentials.panel_id,
        step_id=credentials.step_id_novo_lead,
        title=title,
        contact_id=contact_id,
        session_id=session_id,
        tag_names=tag_names,
        metadata={"unidade": store} if store else {},
        description=resumo,
    )
    logger.info("Card criado para o contato %s: %s", contact_id, created.get("id"))
    return {"action": "created", "card_id": created.get("id")}
