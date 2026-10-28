"""Base comum dos testes. Tudo roda com o DeepEval como executor (a partir de ./):

    deepeval test run tests/                 # tudo
    deepeval test run tests/ -m "not llm"    # so o deterministico (sem banco, sem LLM, sem custo)
    deepeval test run tests/ -m llm          # so as conversas contra o LLM real

Os testes deterministicos sao pytest comum (assert): codigo sem LLM sempre da o mesmo
resultado, e uma metrica de juiz LLM so traria custo e oscilacao. Os de conversa
(tests/test_conversas.py) usam os casos e metricas do DeepEval.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

# O modulo de config exige estas variaveis. Valores de brinquedo bastam para o
# deterministico - nada aqui abre conexao com banco, Redis, Helena ou Google Agenda. Chave de
# LLM de verdade NAO entra aqui: a das conversas vem do .env.
for _nome, _valor in {
    "SETTINGS_ENCRYPTION_KEY": "x" * 43 + "=",
    "OPENAI_API_KEY": "teste",
    "ELEVENLABS_API_KEY": "teste",
    "HELENA_PROD_MESSAGE_TOKEN": "teste",
    "HELENA_PROD_TAG_TOKEN": "teste",
    "HELENA_PROD_FILE_TOKEN": "teste",
    "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
}.items():
    os.environ.setdefault(_nome, _valor)

# Langfuse DESLIGADO nos testes, a forca: o plugin do DeepEval e' importado pelo pytest ANTES
# deste arquivo e pode jogar o .env inteiro em os.environ - com as chaves la, toda rodada de
# teste mandaria trace. Para rastrear uma rodada de proposito: TESTES_COM_LANGFUSE=1.
if os.environ.get("TESTES_COM_LANGFUSE") != "1":
    os.environ["LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["LANGFUSE_SECRET_KEY"] = ""
# Nunca simular falha nos testes.
os.environ["SIMULAR_FALHA"] = ""
os.chdir(RAIZ)  # o .env e os prompts sao lidos relativos a raiz
# As conversas de teste carregam texto de atendimento: nada de telemetria para fora.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
