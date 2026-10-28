"""Operacoes de reserva na Google Agenda, executadas pelo motor v2 quando a politica manda.

No motor antigo eram ferramentas que o LLM chamava quando achava certo (verificar_disponibilidade,
criar_reserva, buscar_reservas, cancelar_reserva, editar_reserva). Agora quem decide QUANDO
chamar e' app/politica.py (ex.: criar so depois de verificar a vaga E de o cliente dizer "sim");
este modulo so executa e devolve o resultado num formato unico:

    {"sucesso": bool, "tecnico": bool, "erro": str | None, ...dados}

`tecnico=True` = a agenda falhou (sem credencial, rede, API) - a politica passa para a equipe.
Erro de negocio (horario fora da janela, vaga ocupada no meio do caminho) vem com
`tecnico=False` e vira uma mensagem ao cliente.

As regras da agenda (janela 19h-23h30, blocos de 2h, capacidade por bloco, reconfirmar a vaga
antes de gravar) continuam em app/google_calendar_client.py, sem mudanca.
"""

from __future__ import annotations

import logging

from app import google_calendar_client as gcal
from app import politica

logger = logging.getLogger(__name__)


def _falha(exc: Exception) -> dict:
    texto = str(exc)
    # GoogleCalendarError e' usado tanto para "nao configurado/API fora" quanto para "o
    # horario deixou de estar disponivel" - o segundo e' negocio, nao falha tecnica.
    tecnico = isinstance(exc, gcal.GoogleCalendarError) and "disponivel" not in texto
    if tecnico:
        logger.error("Agenda: falha tecnica: %s", texto)
    return {"sucesso": False, "tecnico": tecnico, "erro": texto}


def _reserva(r: gcal.ReservaEncontrada) -> dict:
    return {
        "event_id": r.event_id,
        "nome_cliente": r.nome_cliente,
        "data": r.data,
        "horario": r.horario_inicio,
        "horario_fim": r.horario_fim,
        "num_pessoas": r.num_pessoas,
        "observacoes": r.observacoes,
    }


async def executar(acao: politica.Acao, *, telefone: str | None) -> dict:
    dados = acao.dados
    try:
        if acao.operacao == politica.OP_VERIFICAR:
            r = await gcal.verificar_disponibilidade(
                data=dados["data"], horario_solicitado=dados["horario"], num_pessoas=dados["num_pessoas"],
            )
            return {
                "sucesso": True, "tecnico": False, "disponivel": r.disponivel,
                "horarios_alternativos": r.horarios_alternativos, "dia_lotado": r.dia_lotado,
            }
        if acao.operacao == politica.OP_CRIAR:
            r = await gcal.criar_reserva(
                nome_cliente=dados["nome"], telefone=telefone or "não informado", data=dados["data"],
                horario=dados["horario"], num_pessoas=dados["num_pessoas"], observacoes=dados.get("observacoes"),
            )
            return {"sucesso": True, "tecnico": False, "event_id": r.event_id,
                    "horario_inicio": r.horario_inicio, "horario_fim": r.horario_fim}
        if acao.operacao == politica.OP_BUSCAR:
            # Telefone do WhatsApp primeiro (e' o que foi gravado na reserva); sem ele, o nome.
            encontradas = await gcal.buscar_reservas(
                telefone=telefone or None,
                nome_cliente=None if telefone else dados.get("nome"),
                data=dados.get("data"),
            )
            return {"sucesso": True, "tecnico": False, "reservas": [_reserva(r) for r in encontradas]}
        if acao.operacao == politica.OP_CANCELAR:
            await gcal.cancelar_reserva(event_id=dados["event_id"])
            return {"sucesso": True, "tecnico": False}
        if acao.operacao == politica.OP_EDITAR:
            r = await gcal.editar_reserva(
                event_id=dados["event_id"], nova_data=dados.get("data"), novo_horario=dados.get("horario"),
                novo_num_pessoas=dados.get("num_pessoas"), novas_observacoes=dados.get("observacoes"),
            )
            return {"sucesso": True, "tecnico": False, "horario_inicio": r.horario_inicio, "horario_fim": r.horario_fim}
    except (gcal.GoogleCalendarError, ValueError, KeyError) as exc:
        return _falha(exc)
    except Exception as exc:  # rede, HttpError nao embrulhado etc.
        logger.exception("Agenda: erro inesperado em %s", acao.operacao)
        return {"sucesso": False, "tecnico": True, "erro": f"{type(exc).__name__}: {exc}"}
    raise ValueError(f"Operacao de agenda desconhecida: {acao.operacao!r}")
