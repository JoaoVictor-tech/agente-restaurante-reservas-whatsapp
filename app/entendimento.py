"""Etapa ENTENDER do motor v2: uma chamada de LLM com saida estruturada que le a mensagem do
cliente e devolve o que ele disse - e mais nada. Ela nao escreve ao cliente e nao decide o
que fazer (quem decide e' app/politica.py).

Por que separar isto da redacao: no motor antigo a mesma chamada escrevia a resposta E decidia
transferir, escolher a foto e chamar a agenda - tudo a criterio do modelo. Aqui a extracao nao
tem a quem responder: a unica saida possivel e' o JSON da funcao forcada.

O que sai daqui NAO e' verdade ainda: `validar` confere cada valor contra o texto do cliente,
o cardapio e o calendario antes de ele virar estado da conversa.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from num2words import num2words

from app import cardapio
from app.llm_providers.chat_models import system_message

logger = logging.getLogger(__name__)

PROMPT = (Path(__file__).parent / "prompts" / "entendimento.md").read_text(encoding="utf-8")

NOME = "registrar_entendimento"
DESCRICAO = "Registra o que o cliente disse na mensagem atual. Sempre chamada, uma vez."

# Intencao PRINCIPAL da mensagem, uma so, na ordem de prioridade do prompt.
PEDIU_HUMANO = "pediu_humano"
RECLAMACAO = "reclamacao"
PAGAMENTO_OU_NOTA = "pagamento_ou_nota_fiscal"
DELIVERY_OU_EVENTO = "delivery_encomenda_ou_evento"
PRECISA_CONFIRMAR = "precisa_confirmar_com_a_casa"
FAZER_RESERVA = "fazer_reserva"
ALTERAR_RESERVA = "alterar_reserva"
CANCELAR_RESERVA = "cancelar_reserva"
ENCERRANDO = "encerrando"
CONVERSA = "conversa"

INTENCOES = (
    PEDIU_HUMANO, RECLAMACAO, PAGAMENTO_OU_NOTA, DELIVERY_OU_EVENTO, PRECISA_CONFIRMAR,
    FAZER_RESERVA, ALTERAR_RESERVA, CANCELAR_RESERVA, ENCERRANDO, CONVERSA,
)

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intencao": {
            "type": "string",
            "enum": list(INTENCOES),
            "description": "A intenção principal desta mensagem (ver regras no prompt).",
        },
        "nome_cliente": {
            "type": "string",
            "description": "Nome que o cliente disse ser o dele (nesta mensagem ou respondendo 'qual seu nome?'). Vazio se não disse.",
        },
        "itens_do_cardapio": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Itens do cardápio sobre os quais ele perguntou ou que quer ver, com o nome EXATO da lista ITENS DO CARDÁPIO. Vazio se ele não citou item específico.",
        },
        "pergunta_do_cliente": {
            "type": "string",
            "description": "Só se ele FEZ uma pergunta na mensagem atual: reescreva-a em uma frase. Vazio se não há.",
        },
        "reserva_data": {
            "type": "string",
            "description": "Data da reserva que ele disse, convertida para AAAA-MM-DD usando HOJE. Vazio se não disse.",
        },
        "reserva_horario": {
            "type": "string",
            "description": "Horário da reserva que ele disse, em HH:MM (24h). Vazio se não disse.",
        },
        "reserva_pessoas": {
            "type": "integer",
            "description": "Quantas pessoas, só se ele disse. 0 se não disse.",
        },
        "reserva_observacoes": {
            "type": "string",
            "description": "Observação da reserva que ele disse (aniversário, restrição alimentar, mesa na janela...). Vazio se nada.",
        },
        "confirmou": {
            "type": "boolean",
            "description": "true só se ele respondeu SIM, de forma explícita, à confirmação que o atendente pediu na última mensagem.",
        },
        "recusou": {
            "type": "boolean",
            "description": "true se ele respondeu NÃO à confirmação que o atendente pediu na última mensagem (ou disse que quer mudar algo).",
        },
        "resumo_para_equipe": {
            "type": "string",
            "description": "Uma ou duas frases para a equipe humana sobre o que o cliente quer.",
        },
    },
    "required": [
        "intencao", "nome_cliente", "itens_do_cardapio", "pergunta_do_cliente", "reserva_data",
        "reserva_horario", "reserva_pessoas", "reserva_observacoes", "confirmou", "recusou",
        "resumo_para_equipe",
    ],
    "additionalProperties": False,
}

# Quantas mensagens do historico a extracao le. Ela precisa da ultima pergunta do atendente
# para entender respostas curtas ("4", "sim", "Maria"); nao precisa da conversa inteira.
JANELA_HISTORICO = 10


@dataclass
class Entendimento:
    intencao: str = CONVERSA
    nome_cliente: str = ""
    itens: list[str] = field(default_factory=list)
    pergunta_do_cliente: str = ""
    reserva: dict = field(default_factory=dict)   # so os campos ditos: data, horario, num_pessoas, observacoes
    confirmou: bool = False
    recusou: bool = False
    resumo_para_equipe: str = ""

    def para_trace(self) -> dict:
        return {
            "intencao": self.intencao,
            "nome_cliente": self.nome_cliente,
            "itens": self.itens,
            "pergunta_do_cliente": self.pergunta_do_cliente,
            "reserva": self.reserva,
            "confirmou": self.confirmou,
            "recusou": self.recusou,
        }


# Campo "vazio" escrito pelo modelo ("-", "n/a"...): no projeto de referencia isso virava
# pergunta do cliente e observacao na nota. Tratado como vazio.
_VAZIO = {"-", "--", "—", "–", ".", "n/a", "na", "nenhum", "nenhuma", "null", "none", "vazio",
          "nao informado", "não informado", "nao", "não", "sem", "nada"}


def interpretar(bruto: dict) -> Entendimento:
    """JSON do modelo -> Entendimento. Tolerante: campo ausente ou com tipo errado vira o
    valor neutro, nunca excecao - um JSON torto nao pode derrubar o turno."""
    bruto = bruto if isinstance(bruto, dict) else {}

    def texto(chave: str) -> str:
        valor = bruto.get(chave)
        valor = valor.strip() if isinstance(valor, str) else ""
        return "" if valor.lower().strip(" .") in _VAZIO or not valor.strip(" .-—–") else valor

    reserva: dict = {}
    for campo, chave in (("data", "reserva_data"), ("horario", "reserva_horario"), ("observacoes", "reserva_observacoes")):
        if texto(chave):
            reserva[campo] = texto(chave)
    pessoas = bruto.get("reserva_pessoas")
    if isinstance(pessoas, float) and pessoas.is_integer():
        pessoas = int(pessoas)
    if isinstance(pessoas, int) and not isinstance(pessoas, bool) and pessoas > 0:
        reserva["num_pessoas"] = pessoas

    itens = bruto.get("itens_do_cardapio")
    itens = [i.strip() for i in itens if isinstance(i, str) and i.strip()] if isinstance(itens, list) else []

    intencao = texto("intencao")
    return Entendimento(
        intencao=intencao if intencao in INTENCOES else CONVERSA,
        nome_cliente=texto("nome_cliente"),
        itens=itens,
        pergunta_do_cliente=texto("pergunta_do_cliente"),
        reserva=reserva,
        confirmou=bruto.get("confirmou") is True,
        recusou=bruto.get("recusou") is True,
        resumo_para_equipe=texto("resumo_para_equipe"),
    )


# --- Validacao em codigo ------------------------------------------------------------------

_PALAVRAS_QUE_NAO_SAO_NOME = {
    "oi", "ola", "bom dia", "boa tarde", "boa noite", "sim", "nao", "cliente", "obrigado", "obrigada",
}
_CASAL = re.compile(r"\b(casal|eu e (?:minha|meu) \w+|n[óo]s dois|n[óo]s duas)\b", re.IGNORECASE)
_HORARIO = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _dito(trecho: str, textos_cliente: list[str]) -> bool:
    return cardapio.normalizar(trecho) in cardapio.normalizar(" ".join(textos_cliente))


def _pessoas_ditas(n: int, textos_cliente: list[str]) -> bool:
    junto = " ".join(textos_cliente)
    if re.search(rf"(?<!\d){n}(?!\d)", junto):
        return True
    if n == 2 and _CASAL.search(junto):
        return True
    normal = f" {cardapio.normalizar(junto)} "
    extensos = {"uma"} if n == 1 else {"duas"} if n == 2 else set()
    try:
        extensos.add(cardapio.normalizar(num2words(n, lang="pt_BR")))
    except Exception:
        pass
    return any(f" {e} " in normal for e in extensos)


def validar(
    entendido: Entendimento,
    *,
    textos_cliente: list[str],
    itens_cardapio: list[cardapio.Item],
    hoje: datetime.date,
) -> dict:
    """Confere o que o entendimento devolveu e DESCARTA o que nao tem apoio. Muda
    `entendido` no lugar e devolve {campo: valor descartado} (vai para o trace).

    - nome: precisa aparecer no que o cliente escreveu (o modelo nao pode "batizar" o cliente);
    - itens: precisam existir no cardapio (viram o nome oficial do item);
    - data: AAAA-MM-DD valida, de hoje em diante e em ate 1 ano;
    - horario: HH:MM valido;
    - pessoas: o numero (ou "casal", ou por extenso) precisa estar no texto do cliente.
    """
    descartado: dict = {}

    nome = entendido.nome_cliente
    if nome and (cardapio.normalizar(nome) in _PALAVRAS_QUE_NAO_SAO_NOME or not _dito(nome, textos_cliente)):
        descartado["nome_cliente"] = nome
        entendido.nome_cliente = ""

    oficiais: list[str] = []
    for pedido in entendido.itens:
        item = cardapio.buscar(itens_cardapio, pedido)
        if item is None:
            descartado.setdefault("itens", []).append(pedido)
        elif item.nome not in oficiais:
            oficiais.append(item.nome)
    entendido.itens = oficiais

    reserva = entendido.reserva
    if "data" in reserva:
        try:
            dia = datetime.date.fromisoformat(reserva["data"])
            if not (hoje <= dia <= hoje + datetime.timedelta(days=366)):
                raise ValueError("fora do intervalo")
            reserva["data"] = dia.isoformat()
        except ValueError:
            descartado["data"] = reserva.pop("data")
    if "horario" in reserva:
        achou = _HORARIO.match(reserva["horario"].strip())
        if achou:
            reserva["horario"] = f"{int(achou.group(1)):02d}:{achou.group(2)}"
        else:
            descartado["horario"] = reserva.pop("horario")
    if "num_pessoas" in reserva and not _pessoas_ditas(reserva["num_pessoas"], textos_cliente):
        descartado["num_pessoas"] = reserva.pop("num_pessoas")
    return descartado


# --- Chamada ao modelo ----------------------------------------------------------------------

_DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")


def montar_entrada(
    *,
    history: list[dict],
    user_message: str,
    estado: dict,
    itens_cardapio: list[cardapio.Item],
    hoje: datetime.date,
) -> str:
    """O que muda a cada turno vai aqui, e nao no system: assim o prompt fica cacheavel."""
    linhas = [f"HOJE É: {_DIAS[hoje.weekday()]}, {hoje.isoformat()}.", "", "JÁ REGISTRADO NESTA CONVERSA:"]
    registrado = []
    if estado.get("nome"):
        registrado.append(f"- nome do cliente: {estado['nome']}")
    for campo, valor in (estado.get("reserva") or {}).items():
        registrado.append(f"- reserva, {campo}: {valor}")
    linhas += registrado or ["- nada"]
    linhas += ["", "ITENS DO CARDÁPIO:", "; ".join(cardapio.nomes(itens_cardapio)) or "(nenhum)"]
    linhas += ["", "HISTÓRICO RECENTE:"]
    recentes = history[-JANELA_HISTORICO:]
    if recentes:
        for m in recentes:
            quem = "CLIENTE" if m.get("role") == "user" else "ATENDENTE"
            linhas.append(f"{quem}: {m.get('content') or ''}")
    else:
        linhas.append("(início da conversa)")
    linhas += ["", "MENSAGEM ATUAL DO CLIENTE:", user_message]
    return "\n".join(linhas)


async def entender(
    *,
    modelo: BaseChatModel,
    provider: str,
    history: list[dict],
    user_message: str,
    estado: dict,
    itens_cardapio: list[cardapio.Item],
    hoje: datetime.date,
) -> tuple[Entendimento, AIMessage | None]:
    """Uma chamada com a funcao `registrar_entendimento` forcada (`bind_tools`). Devolve
    tambem a mensagem crua, de onde sai o consumo de tokens.

    `bind_tools` e nao `with_structured_output`: o resultado e' o mesmo (o argumento da
    funcao), mas o segundo empilha etapas internas do LangChain que viram linhas no trace."""
    extrator = modelo.bind_tools(
        [{"type": "function", "function": {"name": NOME, "description": DESCRICAO, "parameters": SCHEMA}}],
        tool_choice=NOME,
    )
    resposta = await extrator.ainvoke([
        system_message(provider, PROMPT),
        HumanMessage(content=montar_entrada(
            history=history, user_message=user_message, estado=estado,
            itens_cardapio=itens_cardapio, hoje=hoje,
        )),
    ])
    chamadas = getattr(resposta, "tool_calls", None) or []
    bruto = (chamadas[0].get("args") or {}) if chamadas else {}
    if not chamadas:
        logger.warning("Entendimento sem chamada da funcao - tratado como vazio.")
    logger.info("Entendimento: %s", json.dumps(bruto, ensure_ascii=False)[:500])
    return interpretar(bruto), resposta
