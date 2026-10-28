"""Etapa DECIDIR do motor v2: toda regra de fluxo e de negocio do Bistrô Aurora mora aqui.

Funcoes puras: recebem o estado da conversa e o que o cliente disse (ja validado) e devolvem a
acao do turno e o estado novo. Sem LLM, sem banco, sem agenda, sem I/O - testaveis em
tests/test_motor_v2.py.

No motor antigo essas regras viviam so no prompt (secoes "Abertura do atendimento", "Quando
transferir para humano", descricoes das ferramentas de reserva) e cada turno dependia de o
modelo lembrar delas. Aqui elas sao codigo; o prompt da redacao nao sabe nenhuma, so recebe a
acao pronta (app/redacao.py).

As operacoes de agenda (verificar, criar, buscar, cancelar, editar) tambem sao decididas aqui:
`decidir` pode devolver uma acao OPERACAO; o motor executa a operacao na Google Agenda
(app/reservas.py) e chama `depois_da_agenda` com o resultado, que decide a acao final. O LLM
nunca chama a agenda.

ESTADO (persistido em conversations.slots, JSON):
    nome          nome que o cliente disse
    nome_pedido   o atendente ja pediu o nome (a pergunta nunca se repete)
    fluxo         None | "criar" | "alterar" | "cancelar" - fluxo de reserva em andamento
    reserva       dados da reserva nova em coleta: data, horario, num_pessoas, observacoes
    pendente      operacao esperando o "sim" do cliente: {operacao, dados}
    alvo          reserva existente escolhida para alterar/cancelar
    buscas        quantas buscas sem resultado neste fluxo
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from app import entendimento as nlu

# --- Acoes que chegam na redacao -------------------------------------------------------------
PEDIR_NOME = "PEDIR_NOME"                   # abertura: pedir o nome (uma unica vez na conversa)
RESPONDER = "RESPONDER"                     # cardapio, precos, horario, endereco, duvidas
PERGUNTAR_RESERVA = "PERGUNTAR_RESERVA"     # pedir o proximo dado da reserva (slot)
TRANSFERIR = "TRANSFERIR"                   # passar para a equipe humana (motivo)
DESPEDIR = "DESPEDIR"                       # o cliente encerrou
CONFIRMAR_RESERVA = "CONFIRMAR_RESERVA"     # ha vaga: pedir o "sim" explicito antes de gravar
OFERECER_HORARIOS = "OFERECER_HORARIOS"     # sem vaga no horario: oferecer os alternativos
RESERVA_INDISPONIVEL = "RESERVA_INDISPONIVEL"  # dia lotado / fora da janela / ocupado na hora
RESERVA_CRIADA = "RESERVA_CRIADA"
RESERVA_NAO_ENCONTRADA = "RESERVA_NAO_ENCONTRADA"
ESCOLHER_RESERVA = "ESCOLHER_RESERVA"       # mais de uma reserva: perguntar qual
CONFIRMAR_CANCELAMENTO = "CONFIRMAR_CANCELAMENTO"
RESERVA_CANCELADA = "RESERVA_CANCELADA"
PERGUNTAR_ALTERACAO = "PERGUNTAR_ALTERACAO"  # o que ele quer mudar
CONFIRMAR_ALTERACAO = "CONFIRMAR_ALTERACAO"
RESERVA_ALTERADA = "RESERVA_ALTERADA"
RESERVA_MANTIDA = "RESERVA_MANTIDA"         # desistiu de cancelar/alterar

# --- Operacao na agenda (executada pelo motor, nunca chega na redacao) ----------------------
OPERACAO = "OPERACAO"
OP_VERIFICAR = "verificar"
OP_CRIAR = "criar"
OP_BUSCAR = "buscar"
OP_CANCELAR = "cancelar"
OP_EDITAR = "editar"

# Acoes que terminam com uma pergunta ao cliente (conferido na verificacao).
TERMINAM_COM_PERGUNTA = frozenset({
    PEDIR_NOME, PERGUNTAR_RESERVA, CONFIRMAR_RESERVA, OFERECER_HORARIOS, RESERVA_INDISPONIVEL,
    RESERVA_NAO_ENCONTRADA, ESCOLHER_RESERVA, CONFIRMAR_CANCELAMENTO, PERGUNTAR_ALTERACAO,
    CONFIRMAR_ALTERACAO,
})

# Intencoes que vao para a equipe humana na hora (secao "Quando transferir para humano" do
# prompt antigo), com o motivo de cada uma.
_TRANSFERE_JA = {
    nlu.PEDIU_HUMANO: "pediu_humano",
    nlu.RECLAMACAO: "reclamacao",
    nlu.PAGAMENTO_OU_NOTA: "pagamento_ou_nota_fiscal",
    nlu.DELIVERY_OU_EVENTO: "delivery_encomenda_ou_evento",
    nlu.PRECISA_CONFIRMAR: "duvida_para_cozinha_ou_gerente",
}
MOTIVO_RESERVA = "reserva"                         # reserva completa, sem agenda automatica
MOTIVO_ALTERAR_CANCELAR = "alterar_ou_cancelar_reserva"
MOTIVO_FALHA_AGENDA = "falha_na_agenda"            # a Google Agenda falhou: humano assume

# Dados da reserva nova, na ordem em que sao pedidos (o nome vem antes, se faltar).
SLOTS_RESERVA = ("data", "horario", "num_pessoas")

# Buscas sem achar a reserva antes de passar para a equipe.
MAX_BUSCAS = 2

# Acoes em que o item do cardapio citado (e a foto dele) entra na mensagem.
_COM_ITEM = frozenset({RESPONDER, PERGUNTAR_RESERVA})


@dataclass(frozen=True)
class Acao:
    tipo: str
    slot: str | None = None        # PERGUNTAR_RESERVA: qual dado pedir
    motivo: str | None = None      # TRANSFERIR
    operacao: str | None = None    # OPERACAO: qual operacao de agenda
    item: str | None = None        # item do cardapio a apresentar (nome oficial)
    dados: dict = field(default_factory=dict, compare=True)

    def para_trace(self) -> dict:
        return {k: v for k, v in {
            "tipo": self.tipo, "slot": self.slot, "motivo": self.motivo,
            "operacao": self.operacao, "item": self.item, "dados": self.dados or None,
        }.items() if v}


def _com_item(acao: Acao, entendido: nlu.Entendimento) -> Acao:
    if acao.tipo in _COM_ITEM and entendido.itens:
        # Dois itens: o primeiro vai na foto, o outro so no texto (regra do prompt antigo).
        return Acao(acao.tipo, slot=acao.slot, motivo=acao.motivo, item=entendido.itens[0], dados=acao.dados)
    return acao


def _limpar_fluxo(est: dict) -> None:
    for chave in ("fluxo", "pendente", "alvo", "buscas"):
        est.pop(chave, None)


def decidir(estado: dict | None, entendido: nlu.Entendimento, *, agenda: bool) -> tuple[Acao, dict]:
    """`agenda`: reservas automaticas pela Google Agenda ligadas (ver redacao.reservas_pela_agenda).
    Sem agenda, a reserva e' coletada e passada para a equipe fechar."""
    acao, est = _decidir(copy.deepcopy(estado or {}), entendido, agenda=agenda)
    return _com_item(acao, entendido), est


def _decidir(est: dict, e: nlu.Entendimento, *, agenda: bool) -> tuple[Acao, dict]:
    if e.nome_cliente:
        est["nome"] = e.nome_cliente
        est["nome_pedido"] = True

    motivo = _TRANSFERE_JA.get(e.intencao)
    if motivo:
        _limpar_fluxo(est)
        return Acao(TRANSFERIR, motivo=motivo), est

    # Troca de fluxo de reserva pedida pelo cliente.
    novo_fluxo = {nlu.FAZER_RESERVA: "criar", nlu.ALTERAR_RESERVA: "alterar", nlu.CANCELAR_RESERVA: "cancelar"}.get(e.intencao)
    if novo_fluxo and est.get("fluxo") != novo_fluxo:
        _limpar_fluxo(est)
        est["fluxo"] = novo_fluxo

    pendente = est.get("pendente")
    if pendente:
        if e.confirmou and not e.reserva and not e.recusou:
            est.pop("pendente")
            return Acao(OPERACAO, operacao=pendente["operacao"], dados=pendente["dados"]), est
        if e.recusou or e.reserva:
            est.pop("pendente")
            if not e.reserva:
                if est.get("fluxo") in ("alterar", "cancelar"):
                    _limpar_fluxo(est)
                    return Acao(RESERVA_MANTIDA), est
                return Acao(PERGUNTAR_ALTERACAO), est
            # Mudou algum dado ("nao, melhor as 20h"): segue o fluxo com o dado novo.
        # Senao o cliente mudou de assunto: responde e a confirmacao continua pendente.

    if e.intencao == nlu.ENCERRANDO:
        # Despedida com uma confirmacao em aberto: a operacao nao foi confirmada, cai.
        if est.get("pendente"):
            _limpar_fluxo(est)
        return Acao(DESPEDIR), est

    # Abertura: o nome vem antes de cardapio, preco, foto ou reserva - pedido UMA vez. Se ele
    # ignorar a pergunta, segue sem o nome (ele "insistiu"; a pergunta nunca se repete).
    if not est.get("nome") and not est.get("nome_pedido"):
        est["nome_pedido"] = True
        if e.reserva and not est.get("fluxo"):
            est["fluxo"] = "criar"
        if est.get("fluxo") == "criar":
            est["reserva"] = {**(est.get("reserva") or {}), **e.reserva}
        return Acao(PEDIR_NOME), est

    fluxo = est.get("fluxo")
    if fluxo is None and e.reserva and not est.get("pendente"):
        fluxo = est["fluxo"] = "criar"

    if fluxo == "criar" and not est.get("pendente"):
        reserva = {**(est.get("reserva") or {}), **e.reserva}
        est["reserva"] = reserva
        faltando = [s for s in SLOTS_RESERVA if not reserva.get(s)]
        if not est.get("nome"):
            faltando.insert(0, "nome")
        if faltando:
            return Acao(PERGUNTAR_RESERVA, slot=faltando[0]), est
        if not agenda:
            # Sem agenda automatica: reserva completa vai para a equipe fechar.
            dados = {**reserva, "nome": est["nome"]}
            _limpar_fluxo(est)
            est.pop("reserva", None)
            return Acao(TRANSFERIR, motivo=MOTIVO_RESERVA, dados=dados), est
        return Acao(OPERACAO, operacao=OP_VERIFICAR, dados={**reserva, "nome": est["nome"]}), est

    if fluxo in ("alterar", "cancelar") and not est.get("pendente"):
        if not agenda:
            _limpar_fluxo(est)
            return Acao(TRANSFERIR, motivo=MOTIVO_ALTERAR_CANCELAR), est
        if not est.get("alvo"):
            filtro = {k: e.reserva[k] for k in ("data", "horario") if e.reserva.get(k)}
            return Acao(OPERACAO, operacao=OP_BUSCAR, dados={**filtro, "nome": est.get("nome")}), est
        return _com_alvo(est, e)

    return Acao(RESPONDER), est


def _com_alvo(est: dict, e: nlu.Entendimento) -> tuple[Acao, dict]:
    """Reserva existente ja escolhida: confirmar cancelamento ou alteracao."""
    alvo = est["alvo"]
    if est.get("fluxo") == "cancelar":
        est["pendente"] = {"operacao": OP_CANCELAR, "dados": {"event_id": alvo["event_id"]}}
        return Acao(CONFIRMAR_CANCELAMENTO, dados=alvo), est
    mudancas = {k: v for k, v in e.reserva.items() if v != alvo.get(k)}
    if not mudancas:
        return Acao(PERGUNTAR_ALTERACAO, dados=alvo), est
    est["pendente"] = {"operacao": OP_EDITAR, "dados": {"event_id": alvo["event_id"], **mudancas}}
    return Acao(CONFIRMAR_ALTERACAO, dados={"atual": alvo, "mudancas": mudancas}), est


def depois_da_agenda(
    estado: dict, operacao: Acao, resultado: dict, entendido: nlu.Entendimento
) -> tuple[Acao, dict]:
    """Decide a acao final a partir do resultado da operacao na agenda (app/reservas.py).
    `resultado`: {"sucesso": bool, "tecnico": bool, "erro": str, ...dados da operacao}."""
    est = copy.deepcopy(estado or {})
    op = operacao.operacao

    if resultado.get("tecnico"):
        # A agenda caiu, sem credencial etc.: a equipe fecha a reserva com o cliente.
        dados = {**(est.get("reserva") or {}), **(est.get("alvo") or {}), "erro": resultado.get("erro")}
        _limpar_fluxo(est)
        est.pop("reserva", None)
        return Acao(TRANSFERIR, motivo=MOTIVO_FALHA_AGENDA, dados=dados), est

    reserva = est.get("reserva") or {}

    if op == OP_VERIFICAR:
        if not resultado.get("sucesso"):
            # Fora da janela de reservas (ou dado invalido): pedir outro horario.
            reserva.pop("horario", None)
            est["reserva"] = reserva
            return Acao(RESERVA_INDISPONIVEL, dados={"erro": resultado.get("erro")}), est
        if resultado.get("disponivel"):
            est["pendente"] = {"operacao": OP_CRIAR, "dados": dict(operacao.dados)}
            return Acao(CONFIRMAR_RESERVA, dados=dict(operacao.dados)), est
        reserva.pop("horario", None)
        if resultado.get("horarios_alternativos"):
            est["reserva"] = reserva
            return Acao(OFERECER_HORARIOS, dados={**operacao.dados, "alternativos": resultado["horarios_alternativos"]}), est
        reserva.pop("data", None)
        est["reserva"] = reserva
        return Acao(RESERVA_INDISPONIVEL, dados={"dia_lotado": True, "data": operacao.dados.get("data")}), est

    if op == OP_CRIAR:
        if resultado.get("sucesso"):
            _limpar_fluxo(est)
            est.pop("reserva", None)
            return Acao(RESERVA_CRIADA, dados={**operacao.dados, **{k: resultado[k] for k in ("horario_inicio", "horario_fim") if k in resultado}}), est
        # Alguem ocupou o horario entre a consulta e a gravacao.
        reserva.pop("horario", None)
        est["reserva"] = reserva
        est["fluxo"] = "criar"
        return Acao(RESERVA_INDISPONIVEL, dados={"erro": resultado.get("erro"), "ocupado_agora": True}), est

    if op == OP_BUSCAR:
        encontradas = resultado.get("reservas") or []
        horario = entendido.reserva.get("horario")
        if horario and len(encontradas) > 1:
            mesmas = [r for r in encontradas if r.get("horario") == horario]
            encontradas = mesmas or encontradas
        if not encontradas:
            est["buscas"] = int(est.get("buscas") or 0) + 1
            if est["buscas"] >= MAX_BUSCAS:
                _limpar_fluxo(est)
                return Acao(TRANSFERIR, motivo=MOTIVO_ALTERAR_CANCELAR, dados={"erro": "reserva nao encontrada"}), est
            return Acao(RESERVA_NAO_ENCONTRADA), est
        if len(encontradas) > 1:
            return Acao(ESCOLHER_RESERVA, dados={"reservas": encontradas}), est
        est["alvo"] = encontradas[0]
        # A data/horario que ele usou para achar a reserva nao e' mudanca.
        sem_filtro = nlu.Entendimento(reserva={
            k: v for k, v in entendido.reserva.items() if v != encontradas[0].get(k)
        })
        return _com_alvo(est, sem_filtro)

    if op == OP_CANCELAR:
        if resultado.get("sucesso"):
            alvo = est.get("alvo") or {}
            _limpar_fluxo(est)
            return Acao(RESERVA_CANCELADA, dados=alvo), est
        _limpar_fluxo(est)
        return Acao(TRANSFERIR, motivo=MOTIVO_FALHA_AGENDA, dados={"erro": resultado.get("erro")}), est

    if op == OP_EDITAR:
        if resultado.get("sucesso"):
            alvo = est.get("alvo") or {}
            _limpar_fluxo(est)
            return Acao(RESERVA_ALTERADA, dados={**alvo, **{k: v for k, v in operacao.dados.items() if k != "event_id"}}), est
        # O horario novo nao tem vaga: a reserva original continua valendo.
        return Acao(RESERVA_INDISPONIVEL, dados={"erro": resultado.get("erro"), "alteracao": True}), est

    raise ValueError(f"Operacao de agenda desconhecida: {op!r}")
