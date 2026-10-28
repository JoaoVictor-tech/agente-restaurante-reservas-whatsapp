"""Texto-para-fala via ElevenLabs, usado quando a mensagem recebida foi audio
e a resposta e curta (< 80 palavras) - mesma regra do node 'Responder por Audio?1'
do fluxo N8N original."""

from __future__ import annotations

import re

import httpx
from num2words import num2words

from app.config import get_settings
from app.pricing_tts import get_tts_rate

settings = get_settings()

MAX_PALAVRAS_PARA_AUDIO = 80

# Siglas de versao/modelo (EX, EXL, LX, HS, WR-V, HR-V, CR-V, ZR-V etc.) saem embaralhadas
# quando o ElevenLabs tenta ler como se fossem uma palavra (ex: "EXL" virando algo
# ininteligivel, as vezes ate parecendo um modelo diferente tipo "IXL" que nao existe) -
# confirmado ao vivo pelo cliente (2026-07-30). A 1a tentativa (soletrar com hifen, "E-X-L")
# ainda saia errado as vezes - o nome de cada letra em portugues por extenso ("e xis ele")
# e mais inequivoco pro modelo de voz. Regra geral (nao uma lista fixa dessas siglas
# especificas): qualquer sequencia de 2+ letras MAIUSCULAS (com ou sem hifen no meio, ex:
# "WR-V") tem cada letra trocada pelo nome dela em portugues antes de ir pro TTS. So afeta
# o audio - a bolha de texto exibida/gravada no banco nunca passa por isso.
_SIGLA_RE = re.compile(r"\b[A-Z][A-Z\-]*[A-Z]\b")

_NOME_LETRA_PT = {
    "A": "á", "B": "bê", "C": "cê", "D": "dê", "E": "é", "F": "efe", "G": "gê",
    "H": "agá", "I": "i", "J": "jota", "K": "cá", "L": "éle", "M": "eme", "N": "ene",
    "O": "ó", "P": "pê", "Q": "quê", "R": "erre", "S": "esse", "T": "tê", "U": "u",
    "V": "vê", "W": "dábliu", "X": "xis", "Y": "ípsilon", "Z": "zê",
}


def _soletrar_siglas(texto: str) -> str:
    def _soletrar(match: re.Match[str]) -> str:
        letras = match.group(0).replace("-", "")
        return " ".join(_NOME_LETRA_PT.get(letra, letra) for letra in letras)

    return _SIGLA_RE.sub(_soletrar, texto)


# Valores em reais (ver app.pricing._formatar_preco: "R$ 143.700", ponto como separador de
# milhar) saem com a pronuncia errada no ElevenLabs (confirmado ao vivo pelo cliente,
# 2026-07-30) - o modelo de voz tende a ler o ponto como separador decimal. So afeta o
# audio - a bolha de texto exibida/gravada no banco continua no formato "R$ 143.700".
# Grupo 2 (centavos) e opcional - o cardapio sempre tem centavos (ex:
# "R$ 84,90"), diferente de catalogos com precos redondos (tipo "R$
# 143.700") - sem capturar o ",90" aqui ele sobrava solto no texto e saia
# quebrado/gaguejando no ElevenLabs (confirmado ao vivo pelo cliente, 2026-08-04).
_PRECO_RE = re.compile(r"R\$\s*([\d.]+)(?:,(\d{2}))?")


def _valor_por_extenso(texto: str) -> str:
    def _por_extenso(match: re.Match[str]) -> str:
        try:
            reais = int(match.group(1).replace(".", ""))
        except ValueError:
            return match.group(0)
        centavos = int(match.group(2)) if match.group(2) else 0

        partes = [f"{num2words(reais, lang='pt_BR')} {'real' if reais == 1 else 'reais'}"]
        if centavos:
            partes.append(f"{num2words(centavos, lang='pt_BR')} {'centavo' if centavos == 1 else 'centavos'}")
        return " e ".join(partes)

    return _PRECO_RE.sub(_por_extenso, texto)


# Medidas em metros abreviadas (ex: "2m", "1,80M") saem gaguejando no ElevenLabs
# (confirmado ao vivo pelo cliente, 2026-08-04) - falamos por extenso ("2 metros",
# "1,80 metros"; singular "1 metro" no caso exato de 1). Lookahead nega letra logo depois
# do "m" pra nao casar acidentalmente unidades como "cm" (ex: "45x45cm" no digito
# separado por "x", nunca "digito+m" direto) ou outras palavras. So afeta o audio - a
# bolha de texto exibida/gravada no banco continua abreviada ("2m").
_METRO_RE = re.compile(r"(\d+(?:,\d+)?)\s*[Mm](?![a-zA-Z])")


def _metros_por_extenso(texto: str) -> str:
    def _por_extenso(match: re.Match[str]) -> str:
        valor = match.group(1)
        unidade = "metro" if valor == "1" else "metros"
        return f"{valor} {unidade}"

    return _METRO_RE.sub(_por_extenso, texto)


# Parcelamento (ex: "12x de R$ 141,43") sai como "doze xis" no ElevenLabs em vez de "doze
# vezes" - falamos "vezes" por extenso. O lookahead exige "de R$" logo depois pra so casar
# o "x" de parcelamento (nunca, por exemplo, dimensoes tipo "45x45cm"). Precisa rodar ANTES
# de `_valor_por_extenso` abaixo, que consome o "R$" literal do texto. So afeta o audio - a
# bolha de texto continua com "12x" (mais compacto pra leitura visual no WhatsApp).
_PARCELA_RE = re.compile(r"(\d+)\s*[xX](?=\s*de\s*R\$)")


def _parcelas_por_extenso(texto: str) -> str:
    def _por_extenso(match: re.Match[str]) -> str:
        valor = match.group(1)
        unidade = "vez" if valor == "1" else "vezes"
        return f"{valor} {unidade}"

    return _PARCELA_RE.sub(_por_extenso, texto)


def deve_responder_em_audio(texto: str) -> bool:
    return len(texto.split()) < MAX_PALAVRAS_PARA_AUDIO


async def synthesize(text: str, *, model_id: str | None = None) -> tuple[bytes, float]:
    """`model_id` vem do modelo de voz ATIVO configurado em /dashboard/settings (ver
    app/settings_service.py::get_active_tts_model) - so cai no default do .env
    (ELEVENLABS_MODEL_ID) se nada tiver sido salvo ainda no banco."""
    model_id = model_id or settings.elevenlabs_model_id
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    texto = _metros_por_extenso(text)
    texto = _parcelas_por_extenso(texto)
    texto = _valor_por_extenso(texto)
    if not settings.elevenlabs_pronunciation_dictionary_id:
        # Fallback por regex - so entra quando o dicionario de pronuncia (abaixo) nao esta
        # configurado. Rodar os dois juntos quebraria o dicionario: ele casa pela sigla LITERAL
        # (ex: "EXL") no texto, e o regex ja teria trocado essa sigla pelo nome das letras antes
        # do texto chegar na ElevenLabs, entao a regra do dicionario nunca acharia o que casar.
        texto = _soletrar_siglas(texto)

    payload: dict = {
        "text": texto,
        "voice_id": settings.elevenlabs_voice_id,
        "model_id": model_id,
        "output_format": "mp3_44100_128",
        # Antes nao enviava voice_settings nenhum (ficava no default salvo da voz no painel da
        # ElevenLabs) - valores explicitos aqui pra ter dicção mais consistente (2026-07-30).
        "voice_settings": {
            "stability": settings.elevenlabs_stability,
            "similarity_boost": settings.elevenlabs_similarity_boost,
            "style": settings.elevenlabs_style,
            "use_speaker_boost": settings.elevenlabs_use_speaker_boost,
        },
    }

    # So aplicado quando o dicionario de pronuncia de siglas for criado (precisa da permissao
    # "pronunciation_dictionaries_write" na chave de API, ainda pendente em 2026-07-30) - ate
    # la, o soletrar por regex acima (_soletrar_siglas) continua sendo o fallback.
    if settings.elevenlabs_pronunciation_dictionary_id:
        locator = {"pronunciation_dictionary_id": settings.elevenlabs_pronunciation_dictionary_id}
        if settings.elevenlabs_pronunciation_dictionary_version_id:
            locator["version_id"] = settings.elevenlabs_pronunciation_dictionary_version_id
        payload["pronunciation_dictionary_locators"] = [locator]

    custo_usd = len(texto) / 1000 * get_tts_rate(model_id)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.content, custo_usd
