"""Observabilidade com Langfuse: um trace por atendimento (mensagem do cliente -> resposta).

Desligado por padrao. So liga com LANGFUSE_PUBLIC_KEY e LANGFUSE_SECRET_KEY no ambiente -
sem elas o SDK nem e' importado e nada sai do processo.

DOIS NIVEIS, porque o "deu erro aqui no chat" quase nunca acontece so no LLM:

  "atendimento"   aberto no pipeline (app/pipeline.py:_process_turn), cobre o caminho todo:
                  travas, LLM, envio pela Helena (foto, texto ou audio) e transferencia.
                  Termina com um DESFECHO explicito (respondido, respondido_com_fallback,
                  silencio_intencional, falha_envio, falha_transferencia, falha_interna),
                  gravado como score categorico "desfecho" e, se for falha, como evento
                  `falha_no_atendimento` no painel (funciona com o Langfuse desligado).
  "turno"         aberto em app/llm.py:run_turn; filho do atendimento. O CallbackHandler do
                  LangChain registra cada no do grafo (entender, decidir, redigir, verificar)
                  e cada chamada ao LLM com tokens.

ATENCAO (LGPD): o trace leva a fala do cliente e o prompt da redacao (com o nome dele).
Telefone sai mascarado (`mascarar`, aplicado pelo SDK antes do envio). O Langfuse precisa ser
self-hosted, sob o mesmo controle do banco.

Regra da casa: observabilidade nunca derruba um turno. Toda falha daqui vira log.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
from functools import lru_cache
from typing import Any, Iterator

from app.config import get_settings

logger = logging.getLogger(__name__)

# Telefone brasileiro com ou sem +55/DDD/9, com separadores variados. Exige 10+ digitos
# seguidos de grupos de telefone - medida de pneu ("205/55R16", "205 55 16") e ano nao casam.
_TELEFONE = re.compile(r"(?<![\d/])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}(?![\d/])")


def mascarar(*, data: Any, **_kwargs) -> Any:
    """Funcao `mask` do SDK: troca telefones por [telefone] em qualquer texto do trace."""
    if isinstance(data, str):
        return _TELEFONE.sub("[telefone]", data)
    if isinstance(data, dict):
        return {k: mascarar(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return type(data)(mascarar(data=v) for v in data)
    return data


@lru_cache
def _cliente():
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            base_url=s.langfuse_host,
            mask=mascarar,
        )
    except Exception:
        logger.exception("Langfuse: falha ao iniciar o cliente - seguindo sem observabilidade.")
        return None


def ativo() -> bool:
    return _cliente() is not None


class Turno:
    """Alca do turno em andamento. Sem Langfuse, todos os metodos sao no-op."""

    def __init__(self, observacao: Any = None, callbacks: list | None = None):
        self._obs = observacao
        self.callbacks = callbacks or []

    def concluir(self, resultado) -> None:
        if self._obs is None:
            return
        try:
            custo = resultado.usage_cost
            atualizacao: dict[str, Any] = {
                "output": resultado.text,
                "metadata": {
                    "estado": resultado.estado,
                    "foto": resultado.image_url,
                    "transferido": resultado.transferir,
                    "motivo_transferencia": resultado.motivo_transferencia,
                    "guardrail": resultado.grounding_motivo,
                    "decisao": resultado.trace,
                    "custo_usd": custo.cost_usd if custo else None,
                },
            }
            if not resultado.grounding_valido:
                atualizacao["level"] = "WARNING"
                atualizacao["status_message"] = f"verificacao: {resultado.grounding_motivo}"
            self._obs.update(**atualizacao)
        except Exception:
            logger.exception("Langfuse: falha ao registrar o fim do turno.")

    def falhou(self, erro: BaseException) -> None:
        if self._obs is None:
            return
        with contextlib.suppress(Exception):
            self._obs.update(level="ERROR", status_message=f"{type(erro).__name__}: {erro}"[:500])


@contextlib.contextmanager
def turno(
    *,
    session_id: str | None,
    contact_id: str | None,
    environment: str | None,
    user_message: str,
) -> Iterator[Turno]:
    cliente = _cliente()
    if cliente is None:
        yield Turno()
        return

    pilha = contextlib.ExitStack()
    try:
        from langfuse import propagate_attributes

        # Dentro de um atendimento os atributos do trace ja foram propagados por ele - aqui
        # o turno e' so um filho. Sem atendimento (testes), o turno e' a raiz.
        if _atendimento_atual.get() is None:
            pilha.enter_context(propagate_attributes(
                trace_name="turno",
                session_id=session_id,
                user_id=contact_id,
                environment=environment,
                tags=["cliente"],
            ))
        observacao = pilha.enter_context(cliente.start_as_current_observation(
            name="turno", as_type="agent", input=user_message,
        ))
        from langfuse.langchain import CallbackHandler

        alca = Turno(observacao, [CallbackHandler()])
    except Exception:
        logger.exception("Langfuse: falha ao abrir o trace do turno - seguindo sem ele.")
        pilha.close()
        yield Turno()
        return

    with pilha:
        try:
            yield alca
        except BaseException as erro:
            alca.falhou(erro)
            raise


# --- Atendimento: do webhook ao envio --------------------------------------------------------

RESPONDIDO = "respondido"
RESPONDIDO_COM_FALLBACK = "respondido_com_fallback"   # o cliente recebeu "tive um probleminha, pode repetir?"
SILENCIO_INTENCIONAL = "silencio_intencional"         # atendente humano assumiu pelo painel
FALHA_ENVIO = "falha_envio"                           # a resposta existia mas nao chegou
FALHA_TRANSFERENCIA = "falha_transferencia"           # o lead nao chegou na fila humana
FALHA_INTERNA = "falha_interna"                       # excecao nao tratada no pipeline

FALHAS = frozenset({RESPONDIDO_COM_FALLBACK, FALHA_ENVIO, FALHA_TRANSFERENCIA, FALHA_INTERNA})
_NIVEL = {RESPONDIDO_COM_FALLBACK: "WARNING", FALHA_ENVIO: "ERROR", FALHA_TRANSFERENCIA: "ERROR", FALHA_INTERNA: "ERROR"}

_atendimento_atual: contextvars.ContextVar["Atendimento | None"] = contextvars.ContextVar("atendimento", default=None)


def _url_publica() -> str:
    s = get_settings()
    return (s.langfuse_public_url or s.langfuse_host).rstrip("/")


@lru_cache
def _id_do_projeto() -> str | None:
    cliente = _cliente()
    if cliente is None:
        return None
    try:
        return cliente._get_project_id()
    except Exception:
        logger.exception("Langfuse: nao consegui descobrir o id do projeto para montar links.")
        return None


async def url_da_sessao(session_id: str) -> str | None:
    """Link para a sessao inteira no Langfuse (todas as mensagens da conversa)."""
    if _cliente() is None:
        return None
    projeto = await asyncio.to_thread(_id_do_projeto)
    return f"{_url_publica()}/project/{projeto}/sessions/{session_id}" if projeto else None


class Atendimento:
    """Alca do atendimento em andamento. Funciona igual com o Langfuse desligado - o
    desfecho continua sendo classificado (o pipeline grava no painel); so nao ha trace."""

    def __init__(self, observacao: Any = None, cliente: Any = None, environment: str | None = None):
        self._obs, self._cliente = observacao, cliente
        # O score NAO herda o ambiente do trace: sem passar explicito ele cai em "default" e o
        # filtro "so producao" do painel do Langfuse mistura teste com producao.
        self.environment = environment
        self.desfecho: str | None = None
        self.detalhe: str | None = None
        self.avisos: list[str] = []
        self.resposta: str | None = None   # o que o cliente recebeu (preenchido pelo pipeline)
        self.trace_id: str | None = getattr(observacao, "trace_id", None)

    def marcar(self, desfecho: str, detalhe: str | None = None) -> None:
        """Falha nunca e' apagada por sucesso: se o envio falhou, um "respondido" depois nao
        pode esconder isso."""
        if self.desfecho in FALHAS and desfecho not in FALHAS:
            return
        self.desfecho, self.detalhe = desfecho, detalhe

    def avisar(self, texto: str) -> None:
        """Algo saiu degradado mas o cliente foi atendido (verificacao barrou, foto falhou...)."""
        self.avisos.append(texto)

    @property
    def falhou(self) -> bool:
        return self.desfecho in FALHAS

    @contextlib.contextmanager
    def etapa(self, nome: str, **dados: Any) -> Iterator[None]:
        """Etapa filha do trace (foto, envio, transferencia). Excecao marca a etapa como ERROR e
        segue subindo - quem decide o desfecho e' o pipeline."""
        if self._cliente is None:
            yield
            return
        try:
            cm = self._cliente.start_as_current_observation(name=nome, as_type="span", input=dados or None)
            obs = cm.__enter__()
        except Exception:
            logger.exception("Langfuse: falha ao abrir a etapa %s.", nome)
            yield
            return
        try:
            yield
        except BaseException as erro:
            with contextlib.suppress(Exception):
                obs.update(level="ERROR", status_message=f"{type(erro).__name__}: {erro}"[:500])
            cm.__exit__(type(erro), erro, erro.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)

    def registrar_voz(self, modelo: str, custo_usd: float, caracteres: int) -> None:
        """Etapa "voz:elevenlabs" com o custo da resposta em audio. A ElevenLabs nao passa pelo
        LLM, entao sem isto o custo de voz so aparece na tela de Custos do painel."""
        if self._cliente is None:
            return
        try:
            obs = self._cliente.start_observation(
                name="voz:elevenlabs", as_type="generation", model=modelo,
                usage_details={"input": caracteres}, cost_details={"total": custo_usd},
                metadata={"unidade_de_uso": "caracteres"},
            )
            obs.end()
        except Exception:
            logger.exception("Langfuse: falha ao registrar o custo de voz.")

    async def url(self) -> str | None:
        if self._cliente is None or not self.trace_id:
            return None
        projeto = await asyncio.to_thread(_id_do_projeto)
        return f"{_url_publica()}/project/{projeto}/traces/{self.trace_id}" if projeto else None

    def _finalizar(self) -> None:
        if self._obs is None:
            return
        try:
            nivel = _NIVEL.get(self.desfecho or "")
            if nivel is None and self.avisos:
                nivel = "WARNING"
            self._obs.update(
                output=self.resposta,
                metadata={"desfecho": self.desfecho, "detalhe": self.detalhe, "avisos": self.avisos},
                level=nivel or "DEFAULT",
                status_message=f"{self.desfecho}: {self.detalhe}"[:500] if self.detalhe else self.desfecho,
            )
            if self.trace_id and self.desfecho:
                # Score categorico: no Langfuse vira filtro e grafico ("% de falha_envio por dia").
                self._cliente.create_score(
                    name="desfecho", value=self.desfecho, data_type="CATEGORICAL",
                    trace_id=self.trace_id, comment=self.detalhe, environment=self.environment,
                )
        except Exception:
            logger.exception("Langfuse: falha ao registrar o desfecho do atendimento.")


@contextlib.contextmanager
def atendimento(
    *, session_id: str, contact_id: str | None, environment: str, entrada: str, tipo_mensagem: str | None
) -> Iterator[Atendimento]:
    """Abre o trace do atendimento. Excecao nao tratada dentro dele vira FALHA_INTERNA e
    continua subindo. O desfecho fica em `alca.desfecho` mesmo com o Langfuse desligado."""
    cliente = _cliente()
    pilha = contextlib.ExitStack()
    alca = Atendimento()
    if cliente is not None:
        try:
            from langfuse import propagate_attributes

            pilha.enter_context(propagate_attributes(
                trace_name="atendimento",
                session_id=session_id,
                user_id=contact_id,
                environment=environment,
                tags=["cliente", f"entrada-{(tipo_mensagem or 'texto').lower()}"],
            ))
            obs = pilha.enter_context(cliente.start_as_current_observation(name="atendimento", as_type="agent", input=entrada))
            alca = Atendimento(obs, cliente, environment)
        except Exception:
            logger.exception("Langfuse: falha ao abrir o trace do atendimento - seguindo sem ele.")
            pilha.close()
            pilha = contextlib.ExitStack()
            alca = Atendimento()

    token = _atendimento_atual.set(alca)
    with pilha:
        try:
            yield alca
        except BaseException as erro:
            if not alca.falhou:
                alca.marcar(FALHA_INTERNA, f"{type(erro).__name__}: {erro}"[:300])
            raise
        finally:
            _atendimento_atual.reset(token)
            alca._finalizar()


def encerrar() -> None:
    """Envia o que ainda estiver no buffer. Chamado no shutdown da API."""
    cliente = _cliente()
    if cliente is not None:
        with contextlib.suppress(Exception):
            cliente.shutdown()
