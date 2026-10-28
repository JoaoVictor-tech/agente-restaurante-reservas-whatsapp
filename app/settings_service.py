"""Configuracao do agente gerenciada em runtime (via /dashboard/settings), com as
chaves de API criptografadas no banco - nao em texto puro. As chaves do .env
(app/config.py) so servem de fallback de bootstrap, antes de qualquer
configuracao ter sido salva pelo painel.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppSettings, KnowledgeBaseVersion

logger = logging.getLogger(__name__)
settings = get_settings()

_fernet = Fernet(settings.settings_encryption_key.encode())

PROVIDERS = ("anthropic", "openai", "gemini", "deepseek")

_FALLBACK_KEYS = {
    "anthropic": settings.anthropic_api_key,
    "openai": settings.openai_api_key,
    "gemini": settings.gemini_api_key,
    "deepseek": settings.deepseek_api_key,
}

_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_static.md"


def _encrypt_keys(keys: dict[str, str]) -> str:
    return _fernet.encrypt(json.dumps(keys).encode()).decode()


def _decrypt_keys(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    try:
        return json.loads(_fernet.decrypt(token.encode()).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError):
        logger.exception("Nao foi possivel descriptografar as chaves salvas - verifique SETTINGS_ENCRYPTION_KEY")
        return {}


@dataclass
class ActiveLLMSettings:
    provider: str
    model: str
    api_key: str | None
    system_prompt: str


async def _get_or_create_row(db: AsyncSession) -> AppSettings:
    row = await db.get(AppSettings, 1)
    if row is None:
        row = AppSettings(
            id=1,
            active_provider=settings.default_llm_provider,
            active_model=settings.default_llm_model,
        )
        db.add(row)
        await db.flush()
    return row


async def get_active_llm_settings(db: AsyncSession) -> ActiveLLMSettings:
    row = await db.get(AppSettings, 1)

    if row is None:
        provider = settings.default_llm_provider
        model = settings.default_llm_model
        keys: dict[str, str] = {}
        prompt = None
    else:
        provider = row.active_provider
        model = row.active_model
        keys = _decrypt_keys(row.provider_api_keys_encrypted)
        prompt = row.system_prompt_static

    api_key = keys.get(provider) or _FALLBACK_KEYS.get(provider)
    system_prompt = prompt or _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    return ActiveLLMSettings(provider=provider, model=model, api_key=api_key, system_prompt=system_prompt)


async def get_llm_settings_for(db: AsyncSession, *, provider: str, model: str) -> ActiveLLMSettings:
    """Como `get_active_llm_settings`, mas para um provider/model explicito (nao
    necessariamente o "ativo" agora) - usado pelo Teste Rapido, que fixa o provider/model no
    inicio do teste (`TestRun.llm_provider`/`llm_model`) e continua usando o mesmo em todos os
    turnos, mesmo que o usuario troque o modelo ativo do agente no meio do teste."""
    row = await db.get(AppSettings, 1)
    keys = _decrypt_keys(row.provider_api_keys_encrypted) if row else {}
    prompt = row.system_prompt_static if row else None

    api_key = keys.get(provider) or _FALLBACK_KEYS.get(provider)
    system_prompt = prompt or _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    return ActiveLLMSettings(provider=provider, model=model, api_key=api_key, system_prompt=system_prompt)


async def get_active_tts_model(db: AsyncSession) -> str:
    """Modelo de voz (ElevenLabs) ativo agora - so cai no default do .env
    (ELEVENLABS_MODEL_ID) se nada tiver sido salvo ainda no banco."""
    row = await db.get(AppSettings, 1)
    return (row.tts_model_id if row else None) or settings.elevenlabs_model_id


async def get_audio_replies_enabled(db: AsyncSession) -> bool:
    """Se False, o agente so responde em texto, mesmo recebendo audio do cliente - a
    transcricao do audio recebido (entendimento) nao e afetada por esta flag."""
    row = await db.get(AppSettings, 1)
    return row.audio_replies_enabled if row else True


async def get_configured_key_status(db: AsyncSession) -> dict[str, bool]:
    """Quais provedores ja tem chave configurada (no banco OU via fallback do .env) -
    usado so para exibir no formulario, nunca o valor em si."""
    row = await db.get(AppSettings, 1)
    keys = _decrypt_keys(row.provider_api_keys_encrypted) if row else {}
    return {p: bool(keys.get(p) or _FALLBACK_KEYS.get(p)) for p in PROVIDERS}


async def get_raw_settings(db: AsyncSession) -> tuple[str, str, str | None, str, bool]:
    """Retorna (provider, model, prompt_customizado_ou_None, tts_model, audio_replies_enabled) -
    usado pra pre-preencher o formulario."""
    row = await db.get(AppSettings, 1)
    if row is None:
        return settings.default_llm_provider, settings.default_llm_model, None, settings.elevenlabs_model_id, True
    return (
        row.active_provider,
        row.active_model,
        row.system_prompt_static,
        row.tts_model_id or settings.elevenlabs_model_id,
        row.audio_replies_enabled,
    )


async def update_settings(
    db: AsyncSession,
    *,
    provider: str | None = None,
    model: str | None = None,
    new_keys: dict[str, str] | None = None,
    clear_keys: list[str] | None = None,
    system_prompt: str | None = None,
    reset_prompt_to_default: bool = False,
    tts_model: str | None = None,
    audio_replies_enabled: bool | None = None,
) -> AppSettings:
    row = await _get_or_create_row(db)

    if provider:
        row.active_provider = provider
    if model:
        row.active_model = model
    if tts_model:
        row.tts_model_id = tts_model
    if audio_replies_enabled is not None:
        row.audio_replies_enabled = audio_replies_enabled

    if new_keys or clear_keys:
        current = _decrypt_keys(row.provider_api_keys_encrypted)
        for key, value in (new_keys or {}).items():
            if value:  # campo em branco = "nao alterar"
                current[key] = value
        for key in clear_keys or []:
            current.pop(key, None)
        row.provider_api_keys_encrypted = _encrypt_keys(current)

    if reset_prompt_to_default:
        row.system_prompt_static = None
    elif system_prompt is not None:
        row.system_prompt_static = system_prompt

    await db.flush()
    return row


def default_system_prompt() -> str:
    return _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


# --- Base de conhecimento (cardapio/prompt) com historico - ver /dashboard/base-conhecimento ---
# Mantem system_prompt_static (acima) sempre em sincronia com o conteudo da versao ativa, ja
# que get_active_llm_settings/get_llm_settings_for continuam lendo so aquele campo - o efeito
# e imediato na proxima mensagem, sem reiniciar nada (mesmo mecanismo do /dashboard/settings).


async def get_active_kb_version(db: AsyncSession) -> KnowledgeBaseVersion:
    """Versao ativa agora. Se a base de conhecimento nunca foi editada por
    /dashboard/base-conhecimento (active_kb_version_id ainda None), cria uma primeira versao
    a partir do conteudo atual (customizado ou o padrao do arquivo) na hora - assim a tela
    sempre tem pelo menos uma versao no historico, mesmo em instalacoes ja existentes."""
    row = await _get_or_create_row(db)

    if row.active_kb_version_id:
        version = await db.get(KnowledgeBaseVersion, row.active_kb_version_id)
        if version is not None:
            return version

    content = row.system_prompt_static or default_system_prompt()
    version = KnowledgeBaseVersion(content=content, label="Versao inicial (importada automaticamente)")
    db.add(version)
    await db.flush()
    row.active_kb_version_id = version.id
    row.system_prompt_static = content
    await db.flush()
    return version


async def list_kb_versions(db: AsyncSession, limit: int = 50) -> list[KnowledgeBaseVersion]:
    result = await db.execute(
        select(KnowledgeBaseVersion).order_by(KnowledgeBaseVersion.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def save_kb_version(db: AsyncSession, *, content: str, label: str | None) -> KnowledgeBaseVersion:
    """"Confirmar alteracao" - cria uma versao nova (nunca sobrescreve as antigas) e a
    ativa na hora. O historico anterior continua disponivel pra reverter depois."""
    row = await _get_or_create_row(db)
    version = KnowledgeBaseVersion(content=content, label=(label or "").strip() or None)
    db.add(version)
    await db.flush()
    row.active_kb_version_id = version.id
    row.system_prompt_static = content
    await db.flush()
    return version


async def activate_kb_version(db: AsyncSession, version_id: int) -> KnowledgeBaseVersion:
    """"Reverter" - reativa uma versao ja existente do historico (ex: depois que passou o
    feriado e o cardapio precisa voltar a ser o de sempre)."""
    version = await db.get(KnowledgeBaseVersion, version_id)
    if version is None:
        raise ValueError(f"Versao {version_id} nao encontrada")

    row = await _get_or_create_row(db)
    row.active_kb_version_id = version.id
    row.system_prompt_static = version.content
    await db.flush()
    return version
