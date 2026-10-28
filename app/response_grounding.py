"""Etapa VERIFICAR do motor v2: confere, em codigo, a mensagem que a redacao escreveu antes de
ela chegar ao cliente. Barrou: o motor redige de novo UMA vez com o motivo; barrou de novo, sai
o texto pronto de `fallback` (montado em codigo a partir da acao e do cardapio).

As regras sao as proibicoes do prompt antigo do Bistrô Aurora que dao para conferir em codigo:

0. TEXTO LEGIVEL - resposta vazia, so simbolo ou em alfabeto estrangeiro (modelo degenerou).
1. PRECO INVENTADO - todo "R$" da mensagem precisa existir no cardapio ("proibido inventar
   precos - use somente o cardapio oficial").
2. URL DE FOTO NO TEXTO - "a URL da foto vai somente no campo da foto - nunca no texto;
   proibido colar link ou markdown de imagem" (o link do cardapio online continua liberado).
3. PEDIR PERMISSAO PARA ENVIAR FOTO - "proibido: Posso enviar a foto? / Quer ver?".
4. ABERTURA REPETIDA - "depois da abertura feita uma vez, proibido repetir Ola, Bem-vindo ou a
   apresentacao completa do Bistrô Aurora".
5. PRECO ANTES DO NOME - na mensagem que pede o nome, "so apresente item com preco/foto depois
   que souber o nome".

O que depende da acao do turno (terminar com pergunta, avisar a transferencia, comecar com
"Segue a foto") fica em motor_v2.acao_nao_cumprida.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import cardapio, politica, redacao


@dataclass
class GroundingResult:
    valido: bool
    motivo: str | None = None
    trechos: list[str] = field(default_factory=list)


# --- Regra 0 -------------------------------------------------------------------------------
_ESCRITA_ESTRANGEIRA = re.compile(
    "[Ͱ-ϿЀ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿"
    "぀-ヿ㐀-䶿一-鿿가-힯＀-￯]"
)
_LETRA_LATINA = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
MIN_LETRAS = 4

# --- Regra 2 -------------------------------------------------------------------------------
_URL_DE_FOTO = re.compile(
    r"!\[[^\]]*\]\([^)]*\)|https?://\S+\.(?:jpe?g|png|webp|gif)\b|https?://\S*/assets/\S*", re.IGNORECASE
)

# --- Regra 3 -------------------------------------------------------------------------------
_PEDE_PERMISSAO_FOTO = re.compile(
    r"\b(?:posso|poderia|quer que eu|gostaria que eu|prefere que eu|deseja que eu)\b[^.?!\n]{0,30}"
    r"\b(?:envi|mand|mostr|compartilh)\w*[^.?!\n]{0,25}\bfotos?\b"
    r"|\bquer(?:ia)? ver (?:a |uma )?fotos?\b"
    r"|\bgostaria de ver (?:a |uma )?fotos?\b",
    re.IGNORECASE,
)

# --- Regra 4 -------------------------------------------------------------------------------
_ABERTURA = re.compile(r"^\W*(?:ol[áa]|bem[- ]vind[oa])\b|sou o atendente virtual", re.IGNORECASE)


def _ilegivel(texto: str) -> GroundingResult | None:
    estrangeira = _ESCRITA_ESTRANGEIRA.search(texto)
    if estrangeira:
        return GroundingResult(False, "resposta em alfabeto estrangeiro (modelo degenerou)", [texto.strip()[:40]])
    letras = len(_LETRA_LATINA.findall(texto))
    if letras < MIN_LETRAS:
        return GroundingResult(False, f"resposta sem texto legível ({letras} letras)", [repr(texto.strip()[:40])])
    return None


def validar(
    texto: str,
    *,
    acao: politica.Acao,
    itens_cardapio: list[cardapio.Item],
    primeira_resposta: bool,
) -> GroundingResult:
    if not (texto or "").strip():
        return GroundingResult(False, "resposta vazia")
    ilegivel = _ilegivel(texto)
    if ilegivel is not None:
        return ilegivel

    validos = cardapio.precos(itens_cardapio)
    if validos:
        inventados = [trecho for trecho, valor in cardapio.precos_citados(texto) if valor not in validos]
        if inventados:
            return GroundingResult(False, "preço que não existe no cardápio (proibido inventar preço)", inventados)

    url = _URL_DE_FOTO.search(texto)
    if url:
        return GroundingResult(False, "link ou markdown de foto no texto (a foto vai separada, antes do texto)", [url.group(0)])

    permissao = _PEDE_PERMISSAO_FOTO.search(texto)
    if permissao:
        return GroundingResult(False, "pediu permissão para enviar foto (a foto vai direto, sem perguntar)", [permissao.group(0)])

    if not primeira_resposta:
        abertura = _ABERTURA.search(texto)
        if abertura:
            return GroundingResult(False, "repetiu a abertura (Olá/Bem-vindo/apresentação) depois da primeira mensagem", [abertura.group(0)])

    if acao.tipo == politica.PEDIR_NOME and cardapio.precos_citados(texto):
        return GroundingResult(False, "apresentou preço antes de saber o nome do cliente", [cardapio.precos_citados(texto)[0][0]])

    return GroundingResult(True)


# --- Texto pronto (ultimo recurso) -------------------------------------------------------------

FALLBACK_GENERICO = (
    "Posso te ajudar com o cardápio, horários ou reservas. 🍽️\n\n"
    "O cardápio completo está em https://example.com/cardapio — me conta o que você procura?"
)

_PERGUNTA_SLOT = {
    "nome": "Para seguir com a reserva, qual o seu nome?",
    "data": "Para qual dia você gostaria de reservar?",
    "horario": "Qual horário você prefere?",
    "num_pessoas": "Para quantas pessoas seria a reserva?",
}


def fallback(
    acao: politica.Acao,
    *,
    item: cardapio.Item | None,
    com_foto: bool,
    primeira_resposta: bool,
) -> str:
    """Texto enviado quando a redacao foi barrada duas vezes. Montado em codigo a partir da
    acao (e do cardapio, se havia item): nunca e' um "desculpa, tive um problema" generico, e
    nunca deixa uma confirmacao de reserva sem a pergunta."""
    d = acao.dados or {}
    t = acao.tipo
    if t == politica.PEDIR_NOME:
        return (f"{redacao.ABERTURA}\n\n" if primeira_resposta else "") + redacao.PERGUNTA_NOME
    if t == politica.TRANSFERIR:
        return "Perfeito! Vou te encaminhar agora para a nossa equipe, que continua seu atendimento por aqui. 🙏"
    if t == politica.DESPEDIR:
        return "Combinado! Quando quiser, é só chamar por aqui. Até breve! ✨"
    if t == politica.PERGUNTAR_RESERVA:
        return _PERGUNTA_SLOT.get(acao.slot or "data", _PERGUNTA_SLOT["data"])
    if t == politica.CONFIRMAR_RESERVA:
        return f"Temos vaga! Reserva: {redacao.descrever_reserva(d)}.\n\nPosso confirmar?"
    if t == politica.OFERECER_HORARIOS:
        return (
            f"Às {d.get('horario')} não temos mais vaga, mas temos: {', '.join(d.get('alternativos') or [])}.\n\n"
            "Qual desses horários fica melhor para você?"
        )
    if t == politica.RESERVA_INDISPONIVEL:
        if d.get("dia_lotado"):
            return "Esse dia já está sem vagas. 😕 Gostaria de tentar outra data?"
        return f"As reservas são {redacao.janela_de_reservas()}. Qual outro horário fica bom para você?"
    if t == politica.RESERVA_CRIADA:
        return f"Reserva confirmada! ✨ {redacao.descrever_reserva(d)}. Até lá!"
    if t == politica.RESERVA_NAO_ENCONTRADA:
        return "Não encontrei reserva neste número. Qual a data da reserva ou o nome em que ela foi feita?"
    if t == politica.ESCOLHER_RESERVA:
        opcoes = "\n".join(f"- {redacao.descrever_reserva(r)}" for r in d.get("reservas") or [])
        return f"Encontrei estas reservas:\n{opcoes}\n\nDe qual delas você está falando?"
    if t == politica.CONFIRMAR_CANCELAMENTO:
        return f"Encontrei a reserva: {redacao.descrever_reserva(d)}.\n\nConfirma o cancelamento?"
    if t == politica.RESERVA_CANCELADA:
        return "Prontinho, a reserva foi cancelada. Quando quiser, é só chamar! ✨"
    if t == politica.PERGUNTAR_ALTERACAO:
        return "O que você gostaria de mudar na reserva: data, horário ou número de pessoas?"
    if t == politica.CONFIRMAR_ALTERACAO:
        return (
            f"A reserva ficaria assim: {redacao.descrever_reserva({**(d.get('atual') or {}), **(d.get('mudancas') or {})})}.\n\n"
            "Confirma a alteração?"
        )
    if t == politica.RESERVA_ALTERADA:
        return f"Prontinho, reserva alterada! ✨ {redacao.descrever_reserva(d)}."
    if t == politica.RESERVA_MANTIDA:
        return "Tudo bem! Sua reserva continua como estava. ✨"
    if item is not None:
        cabeca = f"Segue a foto do *{item.nome}*:\n\n" if com_foto else f"*{item.nome}*\n\n"
        descricao = f"{item.descricao}\n\n" if item.descricao else ""
        return f"{cabeca}{descricao}💰 Preço: {item.preco}"
    return FALLBACK_GENERICO
