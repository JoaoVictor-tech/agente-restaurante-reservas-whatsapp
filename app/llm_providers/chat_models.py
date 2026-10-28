"""Chat models do LangChain - o unico ponto onde cada provedor de LLM do agente e' configurado.

Os quatro provedores que o Bistrô Aurora ja usava continuam (trocaveis em runtime pelo painel):
OpenAI, Anthropic, Gemini e DeepSeek. Os ajustes finos que o motor antigo aprendeu em
producao continuam aqui, so que como parametro do LangChain:

- OpenAI: `reasoning_effort="none"` - os modelos GPT-5.x vem com raciocinio ligado, o que e'
  incompativel com funcao forcada (erro 400) e so acrescenta latencia no WhatsApp.
- Anthropic: thinking desligado e cache explicito no prompt estatico (`system_message`).
- DeepSeek: `ChatDeepSeek`, e nao `ChatOpenAI` com base_url - o ChatOpenAI manda
  `max_completion_tokens`, que a API da DeepSeek nao conhece. Thinking desligado (os
  deepseek-v4-* recusam funcao forcada com ele ligado).
- Gemini: o mesmo `thinking_config` calibrado ao vivo no motor antigo (`_thinking_config`).
"""

from __future__ import annotations

from google.genai import types
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from app.pricing_llm import NormalizedUsage
from app.settings_service import ActiveLLMSettings

# Teto de saida por chamada. A redacao do Bistrô Aurora pode listar 3-5 pratos com preco e
# descricao; 1500 cobre isso com folga sem deixar o modelo divagar.
MAX_OUTPUT_TOKENS = 1500


def _thinking_config(model: str) -> types.ThinkingConfig | None:
    """Modelos Gemini vem com "thinking" dinamico ligado por padrao (latencia de 1-12s
    observada). Testado ao vivo no motor antigo: budget 0 alucinava dados; -1 dava picos de
    12s; 1024 fixo manteve ~1-2s sem alucinar. Na familia 3 o equivalente e' o nivel MINIMAL."""
    if model.startswith("gemini-2."):
        return types.ThinkingConfig(thinking_budget=1024)
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    return None


def criar_modelo(llm_settings: ActiveLLMSettings) -> BaseChatModel:
    provider, model, key = llm_settings.provider, llm_settings.model, llm_settings.api_key
    if provider == "openai":
        return ChatOpenAI(model=model, api_key=key, max_tokens=MAX_OUTPUT_TOKENS, reasoning_effort="none")
    if provider == "anthropic":
        # "thinking" desligado: o cliente esta esperando no WhatsApp.
        return ChatAnthropic(model=model, api_key=key, max_tokens=MAX_OUTPUT_TOKENS, thinking={"type": "disabled"})
    if provider == "deepseek":
        return ChatDeepSeek(
            model=model,
            api_key=key,
            max_tokens=MAX_OUTPUT_TOKENS,
            extra_body={"thinking": {"type": "disabled"}},
        )
    if provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=key,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=_thinking_config(model),
        )
    raise ValueError(f"Provedor de LLM desconhecido: {provider}")


def system_message(provider: str, estatico: str, dinamico: str = "") -> SystemMessage:
    """Na Anthropic o bloco estatico (persona + cardapio) vai marcado como cacheavel e o
    dinamico separado, sem marca (marcar o dinamico invalidaria o cache do estatico). Os
    outros provedores fazem cache automatico do prefixo, entao basta o estatico vir primeiro."""
    if provider == "anthropic":
        blocos = [{"type": "text", "text": estatico, "cache_control": {"type": "ephemeral"}}]
        if dinamico:
            blocos.append({"type": "text", "text": dinamico})
        return SystemMessage(content=blocos)
    return SystemMessage(content=f"{estatico}\n\n{dinamico}" if dinamico else estatico)


def uso(mensagem: AIMessage | None) -> NormalizedUsage:
    """`usage_metadata` do LangChain -> o formato de custo do projeto (app/pricing_llm.py).
    No LangChain `input_tokens` ja inclui o cache; no formato do projeto, nao (e' assim que
    a Anthropic reporta e e' o que calculate_cost espera) - por isso a subtracao."""
    meta = getattr(mensagem, "usage_metadata", None) or {}
    detalhes = meta.get("input_token_details") or {}
    lido = detalhes.get("cache_read", 0) or 0
    escrito = detalhes.get("cache_creation", 0) or 0
    return NormalizedUsage(
        input_tokens=max((meta.get("input_tokens", 0) or 0) - lido - escrito, 0),
        output_tokens=meta.get("output_tokens", 0) or 0,
        cache_read_tokens=lido,
        cache_write_tokens=escrito,
    )


def somar_uso(a: NormalizedUsage, b: NormalizedUsage) -> NormalizedUsage:
    """Reducer do LangGraph: cada no devolve o uso da propria chamada e o grafo soma."""
    return NormalizedUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
    )
