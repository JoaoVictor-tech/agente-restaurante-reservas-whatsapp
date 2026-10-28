"""Catalogo de modelos de voz (ElevenLabs) usados na resposta em audio, com custo por 1000
caracteres. ElevenLabs cobra por CREDITO, nao um preco USD fixo por caractere - o valor real
depende do plano da conta. Os precos abaixo foram calibrados contra uma conta real
(plano "pro"), 2026-07-30:

- eleven_multilingual_v2 / eleven_v3: 1 credito por caractere (confirmado empiricamente via
  GET /v1/user/subscription: current_overage=US$106.11 para 442.115 caracteres alem da cota
  do plano = ~US$0.24/1000 caracteres).
- eleven_flash_v2_5 / eleven_turbo_v2_5: 0.5 credito por caractere (documentado pela propria
  ElevenLabs - "Flash e Turbo custam metade do credito por caractere dos modelos padrao") -
  ou seja, exatamente metade do valor acima: ~US$0.12/1000 caracteres.

Se a conta mudar de plano (tier), estes valores precisam ser recalculados do mesmo jeito
(conferir o overage real apos uso, ou a tabela de precos por tier da ElevenLabs).

AJUSTAR: os valores acima foram calibrados para um plano especifico da ElevenLabs - a sua conta
pode estar em outro plano/tier. Confira o
overage real (GET /v1/user/subscription) apos os primeiros usos e recalibre se necessario.
"""

from __future__ import annotations

TTS_MODEL_CATALOG: dict[str, dict] = {
    "eleven_multilingual_v2": {
        "label": "Multilingual v2 (padrão, alta naturalidade)",
        "usd_per_1000_chars": 0.24,
    },
    "eleven_v3": {
        "label": "v3 (mais expressivo, GA desde mar/2026, latência maior)",
        "usd_per_1000_chars": 0.24,
    },
    "eleven_flash_v2_5": {
        "label": "Flash v2.5 (mais rápido, ~metade do custo)",
        "usd_per_1000_chars": 0.12,
    },
    "eleven_turbo_v2_5": {
        "label": "Turbo v2.5 (rápido, ~metade do custo)",
        "usd_per_1000_chars": 0.12,
    },
}

DEFAULT_TTS_MODEL = "eleven_multilingual_v2"
_TAXA_PADRAO = 0.24


def get_tts_rate(model_id: str) -> float:
    return TTS_MODEL_CATALOG.get(model_id, {}).get("usd_per_1000_chars", _TAXA_PADRAO)
