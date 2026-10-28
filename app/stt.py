"""Transcricao de audio (Whisper via OpenAI - Claude nao recebe audio).

O fluxo N8N original tinha 8 nos 'Set' fazendo patch manual de mensagens
inteiras mal transcritas como 'sim' (ex.: 'See you', 'Sweet', 'Scene', 'Switch',
'Send', 'Soon') e um patch especifico de dominio ('Luz de acao' -> 'Luz de
injecao'). Replicamos a mesma normalizacao aqui, num unico lugar, em vez de
oito nos separados.
"""

from __future__ import annotations

import httpx
from openai import OpenAI

from app.config import get_settings

settings = get_settings()
_openai_client = OpenAI(api_key=settings.openai_api_key)

# Preco fixo da OpenAI para whisper-1 (nao varia por plano/conta, diferente da ElevenLabs) -
# confirmado na documentacao oficial, 2026-07-30.
_USD_POR_MINUTO_WHISPER = 0.006

_MISHEARD_SIM = {
    "see you",
    "sweet",
    "scene",
    "switch",
    "send",
    "soon",
}

_DOMAIN_CORRECTIONS = {
    "luz de ação": "luz de injeção",
    "luz de acao": "luz de injeção",
}


def _normalizar(texto: str) -> str:
    bruto = texto.strip()
    if bruto.lower().rstrip(".!?") in _MISHEARD_SIM:
        return "sim"

    resultado = bruto
    for errado, correto in _DOMAIN_CORRECTIONS.items():
        if errado in resultado.lower():
            # substituicao simples preservando o restante do texto
            index = resultado.lower().find(errado)
            resultado = resultado[:index] + correto + resultado[index + len(errado):]
    return resultado


async def download_audio(file_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(file_url)
        response.raise_for_status()
        return response.content


def transcribe(audio_bytes: bytes, filename: str = "audio.mp3") -> tuple[str, float]:
    """Retorna (texto_normalizado, custo_usd). response_format="verbose_json" e o unico jeito
    de obter a duracao real do audio (campo "duration", em segundos) - sem isso nao da pra
    calcular o custo, que e cobrado por minuto."""
    transcript = _openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes),
        response_format="verbose_json",
        # Sem isso o Whisper tenta DETECTAR o idioma sozinho a partir do audio - pouco
        # confiavel em audios curtos (poucos segundos), as vezes "ouve" outro idioma e
        # devolve texto sem nexo (ex.: "joguem-se" pra uma pergunta sobre produto em
        # portugues, confirmado ao vivo pelo cliente, 2026-08-04). Forcar portugues aqui
        # remove essa ambiguidade.
        language="pt",
    )
    custo_usd = (transcript.duration / 60) * _USD_POR_MINUTO_WHISPER
    return _normalizar(transcript.text), custo_usd
