"""Etapa REDIGIR do motor v2: monta o briefing do turno a partir da acao ja decidida
(app/politica.py) e pede ao LLM so o texto - sem ferramentas e sem regra de fluxo.

O prompt estatico (base de conhecimento ativa no painel, padrao app/prompts/system_static.md)
diz QUEM e' o atendente, COMO ele escreve e O CARDAPIO. O bloco montado aqui diz O QUE
escrever nesta mensagem. Nenhuma regra de fluxo aparece no prompt estatico.
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path

from app import cardapio, politica
from app import entendimento as nlu
from app import google_calendar_client as gcal
from app.business_hours import HORARIO_ATENDIMENTO, TZ_SAO_PAULO, dentro_horario_atendimento

logger = logging.getLogger(__name__)

PERSONA = (Path(__file__).parent / "prompts" / "system_static.md").read_text(encoding="utf-8")

# Abertura do atendimento (template do prompt antigo, mantido palavra por palavra).
ABERTURA = (
    "Olá! Bem-vindo ao *Bistrô Aurora* — seu refúgio de almoço sofisticado em "
    "São Paulo. Sou o atendente virtual."
)
PERGUNTA_NOME = "Para começarmos, qual seu nome por favor?"

# --- Base de conhecimento do painel -----------------------------------------------------------

# Marcas do prompt do motor antigo: ele mandava preencher a ferramenta `responder_cliente`
# (bolhas/imageUrl/transferir), que nao existe mais. A redacao receberia instrucoes
# contraditorias - entao usa a persona do repositorio com o CARDAPIO da versao do painel.
_PROMPT_DO_V1 = ("responder_cliente", "dados_extraidos")
_INICIO_CARDAPIO = re.compile(r"^##\s+CARD[ÁA]PIO", re.IGNORECASE | re.MULTILINE)

# Reservas automaticas pela Google Agenda: ligadas quando a base ativa cita a marca abaixo.
# Mesmo gatilho do motor antigo ("o prompt ativo menciona verificar_disponibilidade"), para o
# restaurante continuar ligando/desligando pela troca de versao em /dashboard/base-conhecimento.
MARCAS_AGENDA = ("[reservas-pela-agenda]", "verificar_disponibilidade")


def reservas_pela_agenda(prompt_do_painel: str | None) -> bool:
    return any(marca in (prompt_do_painel or "") for marca in MARCAS_AGENDA)


def _sem_cardapio(texto: str) -> str:
    achou = _INICIO_CARDAPIO.search(texto)
    return texto[: achou.start()].rstrip() if achou else texto.rstrip()


def _so_cardapio(texto: str) -> str | None:
    achou = _INICIO_CARDAPIO.search(texto)
    return texto[achou.start():].strip() if achou else None


def persona_ativa(prompt_do_painel: str | None) -> str:
    """O que vale e' a versao ATIVA no painel (base de conhecimento com historico e reversao).
    Se ela ainda for do motor antigo, usa a persona do repositorio + o cardapio do painel
    (o restaurante pode ter editado precos/itens por la) e avisa no log - ate alguem
    publicar a base no formato novo (scripts/sync_prompt.py)."""
    if not prompt_do_painel or not prompt_do_painel.strip():
        return PERSONA
    if any(marca in prompt_do_painel for marca in _PROMPT_DO_V1):
        logger.error(
            "A base de conhecimento ativa no painel ainda e' a do motor antigo (cita responder_cliente). "
            "Usando a persona de app/prompts/system_static.md com o cardapio do painel - rode "
            "scripts/sync_prompt.py para publicar a base no formato novo."
        )
        cardapio_do_painel = _so_cardapio(prompt_do_painel)
        if not cardapio_do_painel:
            return PERSONA
        return f"{_sem_cardapio(PERSONA)}\n\n{cardapio_do_painel}\n"
    return prompt_do_painel


# --- Formatacao ---------------------------------------------------------------------------

_DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")


def data_extenso(data_iso: str | None) -> str:
    try:
        dia = datetime.date.fromisoformat(data_iso or "")
    except ValueError:
        return data_iso or "?"
    return f"{_DIAS[dia.weekday()]}, {dia.strftime('%d/%m')}"


def descrever_reserva(dados: dict) -> str:
    partes = []
    if dados.get("nome") or dados.get("nome_cliente"):
        partes.append(f"nome {dados.get('nome') or dados.get('nome_cliente')}")
    if dados.get("data"):
        partes.append(data_extenso(dados["data"]))
    if dados.get("horario"):
        fim = dados.get("horario_fim")
        partes.append(f"às {dados['horario']}" + (f" (até {fim})" if fim else ""))
    if dados.get("num_pessoas"):
        partes.append(f"{dados['num_pessoas']} pessoa(s)")
    if dados.get("observacoes"):
        partes.append(f"observação: {dados['observacoes']}")
    return ", ".join(partes)


def janela_de_reservas() -> str:
    ultimo = (
        datetime.datetime.combine(datetime.date(2000, 1, 1), gcal.RESERVA_FECHAMENTO)
        - datetime.timedelta(hours=gcal.RESERVA_DURACAO_HORAS)
    ).strftime("%H:%M")
    return (
        f"das {gcal.RESERVA_ABERTURA.strftime('%H:%M')} às {gcal.RESERVA_FECHAMENTO.strftime('%H:%M')}, "
        f"com {gcal.RESERVA_DURACAO_HORAS}h por reserva (último início às {ultimo})"
    )


# --- Bloco do turno ---------------------------------------------------------------------------


def _contexto(*, canal_resposta: str, primeira_resposta: bool, agora: datetime.datetime) -> list[str]:
    if primeira_resposta:
        saudacao = "É a SUA PRIMEIRA MENSAGEM nesta conversa."
    else:
        saudacao = (
            "Você JÁ fez a abertura nesta conversa: não comece com Olá/Bem-vindo e não repita a "
            "apresentação do Bistrô Aurora. Vá direto ao assunto."
        )
    linhas = [
        "## CONTEXTO (calculado pelo sistema — nunca recalcule)",
        f"Agora é: {_DIAS[agora.weekday()]}, {agora.strftime('%d/%m/%Y %H:%M')} (horário de São Paulo).",
        f"Status agora: {'DENTRO' if dentro_horario_atendimento(agora) else 'FORA'} do horário de funcionamento "
        f"({HORARIO_ATENDIMENTO}). Use este status se ele perguntar se estamos abertos.",
        saudacao,
        f"Canal desta resposta: {canal_resposta}.",
    ]
    if canal_resposta == "ÁUDIO":
        linhas.append("A mensagem será LIDA EM VOZ ALTA: siga a seção de respostas em áudio.")
    return linhas


_OBJETIVO_SLOT = {
    "nome": "o nome para a reserva",
    "data": "para qual dia é a reserva",
    "horario": "o horário desejado",
    "num_pessoas": "para quantas pessoas é a reserva",
}

_TRANSFERENCIA = {
    "pediu_humano": "Ele pediu para falar com uma pessoa. Avise em uma frase curta que está passando para a equipe agora.",
    "reclamacao": "É uma reclamação. Acolha em uma frase, sem se defender e sem prometer nada, e avise que está passando para a equipe resolver.",
    "pagamento_ou_nota_fiscal": "É assunto de pagamento ou nota fiscal. Avise em uma frase que a equipe cuida disso e já continua por aqui.",
    "delivery_encomenda_ou_evento": "É delivery, encomenda especial ou evento. Diga em uma frase que a equipe vai cuidar disso com ele.",
    "duvida_para_cozinha_ou_gerente": "É uma dúvida que precisa de confirmação da cozinha/gerente. Diga em uma frase que vai confirmar com a equipe, que continua por aqui.",
    politica.MOTIVO_RESERVA: "A reserva está com todos os dados. Diga que anotou ({reserva}) e que a equipe vai confirmar a reserva com ele por aqui.",
    politica.MOTIVO_ALTERAR_CANCELAR: "Ele quer alterar ou cancelar uma reserva. Avise em uma frase que a equipe vai cuidar disso com ele por aqui.",
    politica.MOTIVO_FALHA_AGENDA: "Não foi possível concluir a reserva pelo sistema agora. Sem citar erro técnico, diga que a equipe vai finalizar com ele por aqui.",
}


def _tarefa(
    acao: politica.Acao,
    entendido: nlu.Entendimento | None,
    *,
    estado: dict,
    item: cardapio.Item | None,
    com_foto: bool,
    primeira_resposta: bool,
) -> list[str]:
    e = entendido or nlu.Entendimento()
    d = acao.dados or {}
    linhas = ["", "## SUA TAREFA NESTA MENSAGEM"]
    nome = estado.get("nome")
    if nome:
        linhas.append(f"- O nome do cliente é {nome}.")

    if acao.tipo == politica.PEDIR_NOME:
        if primeira_resposta:
            linhas.append(f'- Comece exatamente com a abertura: "{ABERTURA}"')
        if e.pergunta_do_cliente:
            linhas.append(
                f'- Ele perguntou: "{e.pergunta_do_cliente}". Reconheça o interesse em UMA frase curta, '
                "sem preço, sem foto e sem detalhar prato ainda."
            )
        linhas.append(f'- Termine exatamente com: "{PERGUNTA_NOME}"')
        return linhas

    transferindo = acao.tipo == politica.TRANSFERIR
    if e.pergunta_do_cliente and not transferindo:
        linhas.append(
            f'- Ele perguntou: "{e.pergunta_do_cliente}". Responda isso primeiro, com base nos dados '
            "da empresa e no cardápio do seu prompt."
        )
    if item is not None and acao.tipo in (politica.RESPONDER, politica.PERGUNTAR_RESERVA):
        if com_foto:
            linhas.append(f'- Apresente o item "{item.nome}". A foto dele já vai antes da sua mensagem: comece com "Segue a foto do *{item.nome}*:"')
        else:
            linhas.append(f'- Apresente o item "{item.nome}" (sem foto disponível; não fale de foto).')
        linhas.append(f"- Preço exato: 💰 Preço: {item.preco}. Descrição do cardápio: {item.descricao or '-'}; seção: {item.secao or '-'}.")

    if transferindo:
        modelo = _TRANSFERENCIA.get(acao.motivo or "", _TRANSFERENCIA["pediu_humano"])
        linhas.append(f"- {modelo.format(reserva=descrever_reserva(d))}")
        linhas.append("- Não peça permissão, não faça pergunta e não mande foto.")
    elif acao.tipo == politica.RESPONDER:
        if not e.pergunta_do_cliente and item is None:
            linhas.append(
                "- Ele não fez pergunta. Responda ao que ele disse de forma breve e coloque-se à "
                "disposição para o cardápio, horários ou reservas."
            )
    elif acao.tipo == politica.PERGUNTAR_RESERVA:
        ja = descrever_reserva({**(estado.get("reserva") or {})})
        if ja:
            linhas.append(f"- Dados da reserva até agora: {ja}. Não pergunte de novo o que já tem.")
        linhas.append(f"- Termine perguntando, com suas palavras, {_OBJETIVO_SLOT[acao.slot or 'data']}. Só isso.")
    elif acao.tipo == politica.DESPEDIR:
        linhas.append("- Ele está encerrando. Despeça-se em uma frase cordial. NÃO faça pergunta.")
    elif acao.tipo == politica.CONFIRMAR_RESERVA:
        linhas.append(f"- Há vaga. Recapitule a reserva ({descrever_reserva(d)}) e peça que ele confirme com um sim. Termine com essa pergunta.")
    elif acao.tipo == politica.OFERECER_HORARIOS:
        linhas.append(
            f"- Não há vaga às {d.get('horario')} de {data_extenso(d.get('data'))}. Ofereça estes horários do mesmo dia: "
            f"{', '.join(d.get('alternativos') or [])}. Termine perguntando qual ele prefere."
        )
    elif acao.tipo == politica.RESERVA_INDISPONIVEL:
        if d.get("dia_lotado"):
            linhas.append(f"- {data_extenso(d.get('data'))} está sem vagas. Diga isso com cordialidade e pergunte outra data.")
        elif d.get("alteracao"):
            linhas.append("- O novo horário não tem vaga; a reserva original continua valendo. Pergunte se ele quer tentar outro horário.")
        elif d.get("ocupado_agora"):
            linhas.append("- O horário acabou de ser ocupado. Peça desculpas em poucas palavras e pergunte outro horário.")
        else:
            linhas.append(f"- Esse horário não pode ser reservado: as reservas são {janela_de_reservas()}. Pergunte outro horário dentro disso.")
    elif acao.tipo == politica.RESERVA_CRIADA:
        linhas.append(f"- A reserva está FEITA: {descrever_reserva(d)}. Confirme isso de forma calorosa. Sem pergunta.")
    elif acao.tipo == politica.RESERVA_NAO_ENCONTRADA:
        linhas.append(
            "- Não achei reserva no telefone deste WhatsApp. Pergunte a data da reserva ou o nome "
            "em que ela foi feita."
        )
    elif acao.tipo == politica.ESCOLHER_RESERVA:
        opcoes = "; ".join(descrever_reserva(r) for r in d.get("reservas") or [])
        linhas.append(f"- Ele tem mais de uma reserva: {opcoes}. Liste-as e pergunte de qual ele está falando.")
    elif acao.tipo == politica.CONFIRMAR_CANCELAMENTO:
        linhas.append(f"- Encontrei a reserva: {descrever_reserva(d)}. Pergunte se ele confirma o cancelamento dela.")
    elif acao.tipo == politica.RESERVA_CANCELADA:
        linhas.append(f"- A reserva ({descrever_reserva(d)}) foi CANCELADA. Confirme isso com cordialidade. Sem pergunta.")
    elif acao.tipo == politica.PERGUNTAR_ALTERACAO:
        atual = descrever_reserva(d) if d else ""
        linhas.append(
            (f"- A reserva atual é: {atual}. " if atual else "- ")
            + "Pergunte o que ele quer mudar (data, horário ou número de pessoas)."
        )
    elif acao.tipo == politica.CONFIRMAR_ALTERACAO:
        linhas.append(
            f"- Reserva atual: {descrever_reserva(d.get('atual') or {})}. Mudança pedida: "
            f"{descrever_reserva(d.get('mudancas') or {})}. Recapitule e pergunte se ele confirma a alteração."
        )
    elif acao.tipo == politica.RESERVA_ALTERADA:
        linhas.append(f"- A reserva foi ALTERADA e agora está assim: {descrever_reserva(d)}. Confirme isso. Sem pergunta.")
    elif acao.tipo == politica.RESERVA_MANTIDA:
        linhas.append("- Ele desistiu da mudança: diga que a reserva continua como estava. Sem pergunta.")
    return linhas


def montar_bloco(
    *,
    acao: politica.Acao,
    entendido: nlu.Entendimento | None,
    estado: dict,
    item: cardapio.Item | None,
    com_foto: bool,
    canal_resposta: str,
    primeira_resposta: bool,
    correcao: str | None = None,
    agora: datetime.datetime | None = None,
) -> str:
    partes = _contexto(
        canal_resposta=canal_resposta,
        primeira_resposta=primeira_resposta,
        agora=(agora or datetime.datetime.now(TZ_SAO_PAULO)).astimezone(TZ_SAO_PAULO),
    )
    partes += _tarefa(acao, entendido, estado=estado, item=item, com_foto=com_foto, primeira_resposta=primeira_resposta)
    if correcao:
        partes += [
            "",
            "## CORREÇÃO",
            f"Sua versão anterior desta mensagem foi bloqueada porque: {correcao}. "
            "Reescreva cumprindo a mesma tarefa, sem isso.",
        ]
    return "\n".join(partes)
