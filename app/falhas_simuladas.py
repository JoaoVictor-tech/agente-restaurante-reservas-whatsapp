"""Falhas provocadas de proposito, para testar o diagnostico de "deu erro aqui no chat".

Com SIMULAR_FALHA=envio (ou llm, transferencia, audio, foto - separados por virgula), o ponto
correspondente do pipeline levanta `FalhaSimulada` como se a dependencia real tivesse
falhado. Serve para conferir, ponta a ponta, o que o cliente recebe, o que o trace do
Langfuse mostra e o que o painel registra - sem esperar a falha acontecer de verdade.

SO VALE NO AMBIENTE "teste". Em "producao" a variavel e' ignorada: um cliente real nunca
recebe uma falha simulada, mesmo que a variavel fique esquecida no .env.
"""

from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)

PONTOS = ("llm", "envio", "transferencia", "audio", "foto")


class FalhaSimulada(RuntimeError):
    pass


def ativas() -> set[str]:
    return {p.strip().lower() for p in get_settings().simular_falha.split(",") if p.strip()}


def provocar(ponto: str, environment: str) -> None:
    """Levanta FalhaSimulada se `ponto` estiver ligado E o ambiente for "teste"."""
    if environment != "teste" or ponto not in ativas():
        return
    logger.warning("SIMULAR_FALHA: provocando falha em '%s' (ambiente teste).", ponto)
    raise FalhaSimulada(f"falha simulada em '{ponto}' (SIMULAR_FALHA)")
