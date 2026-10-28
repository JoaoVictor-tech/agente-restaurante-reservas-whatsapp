"""Motor v2 do turno de conversa do Bistrô Aurora, como grafo LangGraph:

    START -> entender -> decidir -> redigir -> verificar -> END
                                       ^           |
                                       +-----------+  (barrou: 1 reescrita com o motivo)

  entender  (app/entendimento.py) LLM com saida estruturada: o que o cliente disse (nome,
            intencao, itens do cardapio, dados de reserva, sim/nao). Validado em codigo
            contra o texto do cliente, o cardapio e o calendario.
  decidir   (app/politica.py) funcao pura: estado + entendimento -> acao. Quando a acao e'
            uma operacao de agenda, este no a executa (app/reservas.py) e a politica decide
            a acao final com o resultado. O LLM nunca chama a agenda.
  redigir   (app/redacao.py) LLM sem ferramentas: escreve a acao ja decidida.
  verificar (app/response_grounding.py + acao_nao_cumprida) barrou: volta UMA vez para a
            redacao com o motivo; barrou de novo: texto pronto montado em codigo.

Chamado por `llm.run_turn` (que abre o trace do Langfuse).
"""

from __future__ import annotations

import datetime
import logging
import operator
import re
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app import cardapio, entendimento, politica, redacao, reservas, response_grounding
from app.business_hours import TZ_SAO_PAULO
from app.config import get_settings
from app.llm_providers.chat_models import criar_modelo, somar_uso, system_message, uso
from app.pricing_llm import NormalizedUsage, UsageCost, calculate_cost
from app.settings_service import ActiveLLMSettings

logger = logging.getLogger(__name__)

MAX_REESCRITAS = 1


@dataclass
class TurnResult:
    text: str
    image_url: str | None
    transferir: bool
    resumo_interno: str | None
    usage_cost: UsageCost
    estado: dict = field(default_factory=dict)
    motivo_transferencia: str | None = None
    grounding_valido: bool = True
    grounding_motivo: str | None = None
    trace: dict = field(default_factory=dict)


@dataclass
class Turno:
    """Entrada do turno e dependencias - fixas durante a execucao do grafo."""

    modelo: BaseChatModel
    provider: str
    persona: str
    itens_cardapio: list[cardapio.Item]
    agenda: bool
    estado: dict
    history: list[dict]
    user_message: str
    client_will_hear_audio: bool = False
    client_phone: str | None = None
    primeira_resposta: bool = True
    hoje: datetime.date = field(default_factory=lambda: datetime.datetime.now(TZ_SAO_PAULO).date())


class Estado(TypedDict, total=False):
    """O que muda de no para no."""

    entendido: entendimento.Entendimento
    descartado: dict
    acao: politica.Acao
    estado: dict
    operacoes: list
    item: cardapio.Item | None
    com_foto: bool
    texto: str
    cortada: bool
    reescritas: int
    correcao: str | None
    refazer: bool
    primeiro_bloqueio: str | None
    bloqueio_final: str | None
    usou_fallback: bool
    usage: Annotated[NormalizedUsage, somar_uso]
    chamadas_llm: Annotated[int, operator.add]


# --- Nos ------------------------------------------------------------------------------------


async def entender(estado: Estado, runtime: Runtime[Turno]) -> Estado:
    t = runtime.context
    entendido, bruto = await entendimento.entender(
        modelo=t.modelo, provider=t.provider, history=t.history, user_message=t.user_message,
        estado=t.estado, itens_cardapio=t.itens_cardapio, hoje=t.hoje,
    )
    # Fonte de verdade da validacao: so a fala REAL do cliente.
    textos_cliente = [*(m.get("content") or "" for m in t.history if m.get("role") == "user"), t.user_message]
    descartado = entendimento.validar(
        entendido, textos_cliente=textos_cliente, itens_cardapio=t.itens_cardapio, hoje=t.hoje,
    )
    if descartado:
        logger.info("Descartado na validacao: %s", descartado)
    return {"entendido": entendido, "descartado": descartado, "usage": uso(bruto), "chamadas_llm": 1}


async def decidir(estado: Estado, runtime: Runtime[Turno]) -> Estado:
    t = runtime.context
    entendido = estado["entendido"]
    acao, novo = politica.decidir(t.estado, entendido, agenda=t.agenda)
    operacoes = []
    while acao.tipo == politica.OPERACAO:
        resultado = await reservas.executar(acao, telefone=t.client_phone)
        operacoes.append({"operacao": acao.operacao, "sucesso": resultado.get("sucesso"), "erro": resultado.get("erro")})
        acao, novo = politica.depois_da_agenda(novo, acao, resultado, entendido)
    logger.info("Decisao do turno: %s", acao.para_trace())

    item = cardapio.buscar(t.itens_cardapio, acao.item) if acao.item else None
    return {
        "acao": acao,
        "estado": novo,
        "operacoes": operacoes,
        "item": item,
        "com_foto": bool(item and item.foto_url),
    }


async def redigir(estado: Estado, runtime: Runtime[Turno]) -> Estado:
    t = runtime.context
    bloco = redacao.montar_bloco(
        acao=estado["acao"],
        entendido=estado.get("entendido"),
        estado=estado.get("estado") or {},
        item=estado.get("item"),
        com_foto=estado.get("com_foto", False),
        canal_resposta="ÁUDIO" if t.client_will_hear_audio else "TEXTO",
        primeira_resposta=t.primeira_resposta,
        correcao=estado.get("correcao"),
    )
    resposta = await t.modelo.ainvoke([
        system_message(t.provider, t.persona, bloco),
        *t.history,
        HumanMessage(content=t.user_message),
    ])
    return {
        "texto": (resposta.text or "").strip(),
        "cortada": parou_por_limite(resposta),
        "usage": uso(resposta),
        "chamadas_llm": 1,
    }


# Raciocinio do modelo escrito no texto da resposta (visto no projeto de referencia com o
# gemini-3-flash-preview: '*Double check:* ... *Final Text:* Boa noite!'). Marcadores
# explicitos OU palavras que so existem em ingles - o cliente fala portugues.
_MARCADOR_DE_RASCUNHO = re.compile(
    r"\*(?:final text|double check|draft|check|plan|note)s?:?\*|final text:|double check", re.IGNORECASE
)
_SO_EM_INGLES = {
    "the", "and", "let's", "okay", "within", "should", "draft", "check", "used", "i'll", "i",
    "it's", "is", "of", "to", "my", "business", "hours", "go", "final", "text", "need",
}
_LIXO_NO_COMECO = re.compile(r"""^\s*["'*:]{2,}""")


def texto_vazado(texto: str) -> bool:
    if _MARCADOR_DE_RASCUNHO.search(texto) or _LIXO_NO_COMECO.search(texto):
        return True
    palavras = re.findall(r"[a-z']+", texto.lower())
    return sum(p in _SO_EM_INGLES for p in palavras) >= 3


_PAROU_POR_LIMITE = {"MAX_TOKENS", "length", "max_tokens"}


def parou_por_limite(resposta) -> bool:
    """A redacao bateu no teto de tokens? Entao o texto esta cortado no meio."""
    meta = getattr(resposta, "response_metadata", None) or {}
    return any(meta.get(chave) in _PAROU_POR_LIMITE for chave in ("finish_reason", "stop_reason"))


_AVISA_TRANSFERENCIA = re.compile(
    r"equipe|atendente|colega|gerente|pessoa|humano|encaminh|transfer", re.IGNORECASE
)


def acao_nao_cumprida(acao: politica.Acao, texto: str, *, com_foto: bool) -> str | None:
    """A redacao recebeu uma acao; o texto cumpriu?"""
    if acao.tipo in politica.TERMINAM_COM_PERGUNTA and "?" not in texto:
        return "a mensagem precisa terminar com a pergunta pedida na tarefa, e ela ficou sem pergunta"
    if acao.tipo == politica.PEDIR_NOME and "nome" not in texto.lower():
        return "a mensagem precisa pedir o nome do cliente"
    if acao.tipo == politica.TRANSFERIR and not _AVISA_TRANSFERENCIA.search(texto):
        return "a mensagem precisa avisar que a equipe (uma pessoa) vai continuar o atendimento"
    if com_foto and not texto.lstrip().lower().startswith("segue a foto"):
        return 'a foto vai antes da mensagem: o texto precisa começar com "Segue a foto do *[nome do prato]*:"'
    return None


def verificar(estado: Estado, runtime: Runtime[Turno]) -> Estado:
    t = runtime.context
    acao, texto = estado["acao"], estado.get("texto") or ""
    resultado = response_grounding.validar(
        texto, acao=acao, itens_cardapio=t.itens_cardapio, primeira_resposta=t.primeira_resposta,
    )
    if resultado.valido:
        if estado.get("cortada"):
            motivo = "a mensagem saiu cortada no meio (limite de tokens); escreva uma versão mais curta"
        elif texto_vazado(texto):
            motivo = "a mensagem trouxe rascunho/raciocínio interno; escreva só o texto final ao cliente, em português"
        else:
            motivo = acao_nao_cumprida(acao, texto, com_foto=estado.get("com_foto", False))
        if motivo:
            resultado = response_grounding.GroundingResult(valido=False, motivo=motivo)

    reescritas = estado.get("reescritas", 0)
    saida: Estado = {"bloqueio_final": resultado.motivo, "refazer": False}
    if reescritas == 0:
        saida["primeiro_bloqueio"] = resultado.motivo

    if not resultado.valido and reescritas < MAX_REESCRITAS:
        # A redacao nao tem ferramenta nem decide nada: refazer com o motivo e' so uma
        # correcao de TEXTO.
        logger.warning("Verificacao barrou (%s) - redigindo de novo. Trechos: %s", resultado.motivo, resultado.trechos)
        return {**saida, "refazer": True, "reescritas": reescritas + 1, "correcao": resultado.motivo}

    if not resultado.valido:
        logger.warning("Verificacao barrou de novo (%s) - usando o texto pronto.", resultado.motivo)
        saida["texto"] = response_grounding.fallback(
            acao, item=estado.get("item"), com_foto=estado.get("com_foto", False),
            primeira_resposta=t.primeira_resposta,
        )
        saida["usou_fallback"] = True
    return saida


def _depois_de_verificar(estado: Estado) -> str:
    return "redigir" if estado.get("refazer") else END


def _montar_grafo():
    grafo = StateGraph(Estado, context_schema=Turno)
    grafo.add_node("entender", entender)
    grafo.add_node("decidir", decidir)
    grafo.add_node("redigir", redigir)
    grafo.add_node("verificar", verificar)
    grafo.add_edge(START, "entender")
    grafo.add_edge("entender", "decidir")
    grafo.add_edge("decidir", "redigir")
    grafo.add_edge("redigir", "verificar")
    grafo.add_conditional_edges("verificar", _depois_de_verificar, ["redigir", END])
    return grafo.compile()


GRAFO = _montar_grafo()


# --- Nota interna da transferencia (montada em codigo) -------------------------------------

_MOTIVO_LEGIVEL = {
    "pediu_humano": "cliente pediu atendimento humano",
    "reclamacao": "reclamação",
    "pagamento_ou_nota_fiscal": "pagamento / nota fiscal",
    "delivery_encomenda_ou_evento": "delivery, encomenda especial ou evento",
    "duvida_para_cozinha_ou_gerente": "dúvida que precisa da cozinha/gerente",
    politica.MOTIVO_RESERVA: "reserva de mesa para confirmar",
    politica.MOTIVO_ALTERAR_CANCELAR: "alterar/cancelar reserva",
    politica.MOTIVO_FALHA_AGENDA: "reserva não concluída pela agenda automática",
}


def resumo_interno(
    acao: politica.Acao, estado: dict, entendido: entendimento.Entendimento | None, contact_name: str | None
) -> str:
    linhas = [
        f"Cliente: {estado.get('nome') or contact_name or 'não informado'}.",
        f"Motivo: {_MOTIVO_LEGIVEL.get(acao.motivo or '', acao.motivo or '-')}.",
    ]
    reserva = {k: v for k, v in (acao.dados or {}).items() if k in ("data", "horario", "num_pessoas", "observacoes") and v}
    if reserva:
        linhas.append(f"Reserva: {redacao.descrever_reserva(reserva)}.")
    if entendido is not None and entendido.resumo_para_equipe:
        linhas.append(f"Resumo: {entendido.resumo_para_equipe}")
    return "\n".join(linhas)


# --- Entrada --------------------------------------------------------------------------------


async def run_turn(
    *,
    history: list[dict],
    user_message: str,
    llm_settings: ActiveLLMSettings,
    estado: dict | None = None,
    client_will_hear_audio: bool = False,
    client_phone: str | None = None,
    contact_name: str | None = None,
    callbacks: list | None = None,
) -> TurnResult:
    """`callbacks`: handlers do LangChain para o grafo (o do Langfuse, quando ligado - ver
    app/observabilidade.py). Cada no e cada chamada ao LLM viram observacoes do trace."""
    if not llm_settings.api_key:
        raise RuntimeError(
            f"Nenhuma chave de API configurada para o provedor '{llm_settings.provider}' - "
            "configure em /dashboard/settings ou no .env de fallback."
        )
    primeira_resposta = not any(m.get("role") == "assistant" for m in history)
    estado = dict(estado or {})
    if not estado and not primeira_resposta:
        # Conversa que comecou no motor antigo (sem estado salvo): a abertura ja aconteceu e
        # a pergunta do nome nunca se repete.
        estado["nome_pedido"] = True

    persona = redacao.persona_ativa(llm_settings.system_prompt)
    final = await GRAFO.ainvoke(
        {"usage": NormalizedUsage(), "chamadas_llm": 0, "reescritas": 0},
        config={
            "callbacks": callbacks or [],
            "run_name": "motor_v2",
            "metadata": {"langfuse_tags": [f"release:{get_settings().app_version}"]},
        },
        context=Turno(
            modelo=criar_modelo(llm_settings),
            provider=llm_settings.provider,
            persona=persona,
            itens_cardapio=cardapio.ler(persona),
            agenda=redacao.reservas_pela_agenda(llm_settings.system_prompt),
            estado=estado,
            history=history,
            user_message=user_message,
            client_will_hear_audio=client_will_hear_audio,
            client_phone=client_phone,
            primeira_resposta=primeira_resposta,
        ),
    )

    acao: politica.Acao = final["acao"]
    entendido = final.get("entendido")
    novo_estado = final.get("estado") or {}
    transferir = acao.tipo == politica.TRANSFERIR
    item = final.get("item")
    primeiro_bloqueio = final.get("primeiro_bloqueio")
    return TurnResult(
        text=final["texto"],
        image_url=item.foto_url if (item and final.get("com_foto")) else None,
        transferir=transferir,
        resumo_interno=resumo_interno(acao, novo_estado, entendido, contact_name) if transferir else None,
        usage_cost=calculate_cost(llm_settings.provider, llm_settings.model, final["usage"]),
        estado=novo_estado,
        motivo_transferencia=acao.motivo if transferir else None,
        # Barrou e a reescrita passou: o bloqueio ainda fica registrado.
        grounding_valido=primeiro_bloqueio is None,
        grounding_motivo=primeiro_bloqueio,
        trace={
            "motor": "v2",
            "entendimento": entendido.para_trace() if entendido else None,
            "descartado_na_validacao": final.get("descartado") or {},
            "acao": acao.para_trace(),
            "agenda": final.get("operacoes") or [],
            "verificacao": primeiro_bloqueio,
            "verificacao_final": final.get("bloqueio_final"),
            "texto_pronto": bool(final.get("usou_fallback")),
        },
    )
