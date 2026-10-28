"""Porta de entrada de um turno do agente do Bistrô Aurora.

`run_turn` abre o trace do turno no Langfuse (app/observabilidade.py) e roda o motor
(app/motor_v2.py: entender -> decidir -> redigir -> verificar). O pipeline chama daqui;
ninguem chama o motor direto.

Divisao de responsabilidade, que e' o principio central do motor v2:
- CODIGO decide o que e' verdade e o que fazer: data/hora, se o bistro esta aberto, se ja
  pediu o nome, quais dados da reserva faltam, quando consultar/gravar na agenda, quando
  transferir, qual foto mandar e se a resposta cumpriu a tarefa (app/politica.py,
  app/motor_v2.py, app/response_grounding.py).
- LLM entende o que o cliente disse e escreve a mensagem.

Historico: ate 10/2026 havia um motor unico em que o LLM decidia tudo numa chamada (a
ferramenta `responder_cliente` com foto/transferir/resumo, mais um loop de ferramentas de
reserva). Foi removido depois da revisao de paridade das regras de negocio - ver
README.md.
"""

from __future__ import annotations

from app import observabilidade
from app.motor_v2 import TurnResult
from app.settings_service import ActiveLLMSettings

# Cliente da API compativel da DeepSeek (usado pela geracao de persona/base do painel,
# app/persona_llm.py - fora do turno do agente).
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

__all__ = ["TurnResult", "run_turn", "DEEPSEEK_BASE_URL"]


async def run_turn(
    *,
    history: list[dict],
    user_message: str,
    llm_settings: ActiveLLMSettings,
    estado: dict | None = None,
    client_will_hear_audio: bool = False,
    client_phone: str | None = None,
    contact_name: str | None = None,
    session_id: str | None = None,
    contact_id: str | None = None,
    environment: str | None = None,
) -> TurnResult:
    """Executa um turno completo dentro de um trace do Langfuse (no-op sem as chaves).

    `history`: [{"role": "user"|"assistant", "content": str}] ja recortado pelo chamador.
    `estado`: estado da conversa salvo no turno anterior (conversations.slots).
    `client_will_hear_audio` reflete a decisao ja tomada pelo pipeline (cliente mandou audio
    E resposta em audio ligada) - a redacao so adapta o ESTILO."""
    from app import motor_v2

    with observabilidade.turno(
        session_id=session_id, contact_id=contact_id, environment=environment, user_message=user_message,
    ) as trace:
        resultado = await motor_v2.run_turn(
            history=history,
            user_message=user_message,
            llm_settings=llm_settings,
            estado=estado,
            client_will_hear_audio=client_will_hear_audio,
            client_phone=client_phone,
            contact_name=contact_name,
            callbacks=trace.callbacks,
        )
        trace.concluir(resultado)
    return resultado
