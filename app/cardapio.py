"""Leitura do cardapio que vive na base de conhecimento (prompt editavel no painel).

O cardapio continua sendo editado pelo restaurante em /dashboard/base-conhecimento, no mesmo
formato de sempre (`| Nome | Preco | Descricao | URL_FOTO |`). Este modulo so le essas linhas
para que o CODIGO possa:

- escolher a foto do item (a URL vem do cardapio, nunca do modelo - antes o LLM copiava a
  URL e podia inventar uma);
- dar a redacao o preco exato do item apresentado;
- conferir, na verificacao, que todo "R$" da resposta existe no cardapio (preco inventado e'
  barrado - regra "proibido inventar precos" do prompt antigo).

Funcoes puras, sem I/O - testadas em tests/test_motor_v2.py.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_LINHA = re.compile(r"^\s*\|\s*(?P<nome>[^|]+?)\s*\|\s*(?P<preco>R\$\s*[\d.,]+)\s*\|(?P<resto>.*)$")
_URL = re.compile(r"https?://\S+?\.(?:jpe?g|png|webp|gif)\b", re.IGNORECASE)
_VALOR = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)", re.IGNORECASE)


@dataclass(frozen=True)
class Item:
    nome: str
    preco: str          # como esta no cardapio: "R$ 84,90"
    descricao: str
    foto_url: str | None
    secao: str = ""


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


def ler(texto: str) -> list[Item]:
    """Itens do cardapio na base de conhecimento. Linha fora do formato e' ignorada."""
    itens: list[Item] = []
    secao = ""
    for linha in (texto or "").splitlines():
        if linha.startswith("### "):
            secao = linha[4:].strip()
            continue
        achou = _LINHA.match(linha)
        if not achou:
            continue
        colunas = [c.strip() for c in achou.group("resto").split("|")]
        descricao = colunas[0] if colunas else ""
        foto = _URL.search(achou.group("resto"))
        itens.append(Item(
            nome=achou.group("nome").strip(),
            preco=re.sub(r"\s+", " ", achou.group("preco").strip()),
            descricao=descricao,
            foto_url=foto.group(0) if foto else None,
            secao=secao,
        ))
    return itens


def buscar(itens: list[Item], nome: str) -> Item | None:
    """Item do cardapio pelo nome que o entendimento devolveu. Nome exato (sem acento/caixa)
    primeiro; senao, o item cujo nome contem todas as palavras pedidas - so se for UM. Na
    duvida, None: melhor nao mandar foto do que mandar a foto errada."""
    alvo = normalizar(nome)
    if not alvo:
        return None
    for item in itens:
        if normalizar(item.nome) == alvo:
            return item
    palavras = set(alvo.split())
    candidatos = [i for i in itens if palavras <= set(normalizar(i.nome).split())]
    return candidatos[0] if len(candidatos) == 1 else None


def valor(texto_preco: str) -> str | None:
    """'R$ 84,90' / 'R$84,9' / 'R$ 84' -> '84,90' (forma canonica para comparar)."""
    achou = _VALOR.search(texto_preco or "")
    if not achou:
        return None
    bruto = achou.group(1).replace(".", "")
    inteiro, _, centavos = bruto.partition(",")
    return f"{int(inteiro)},{(centavos + '00')[:2]}"


def precos(itens: list[Item]) -> set[str]:
    return {v for v in (valor(i.preco) for i in itens) if v}


def precos_citados(texto: str) -> list[tuple[str, str]]:
    """[(trecho, valor canonico)] de cada "R$ ..." do texto."""
    return [(m.group(0), valor(m.group(0)) or "") for m in _VALOR.finditer(texto or "")]


def nomes(itens: list[Item]) -> list[str]:
    return [i.nome for i in itens]
