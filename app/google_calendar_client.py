"""Integracao com a Google Calendar API para o sistema de reservas automaticas do
Bistrô Aurora - consulta disponibilidade e cria reservas na agenda apontada
por GOOGLE_CALENDAR_ID, autenticando com a conta de servico em GOOGLE_CREDENTIALS_PATH
(ver app/config.py).

Cada reserva criada por este modulo e marcada com uma propriedade privada
(extendedProperties.private.origem=agente_bistro) para que a contagem de capacidade
nunca misture reservas do agente com outros eventos que porventura existam na mesma
agenda (ex: bloqueios manuais do dono) - ver `_contar_reservas`.

Janela de reservas (19h-23h30, blocos fixos de 2h, grade de 30min) e capacidade maxima
por bloco (MAX_CAPACITY_PER_SLOT) sao regras de teste combinadas para este piloto - NAO
confundir com o horario real de atendimento (somente almoco, app/business_hours.py).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.business_hours import TZ_SAO_PAULO
from app.config import get_settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_ORIGEM_KEY = "origem"
_ORIGEM_VALUE = "agente_bistro"

RESERVA_DURACAO_HORAS = 2
RESERVA_ABERTURA = datetime.time(19, 0)
RESERVA_FECHAMENTO = datetime.time(23, 30)
RESERVA_GRANULARIDADE_MINUTOS = 30

# Janela de busca quando a reserva a editar/cancelar nao tem data informada pelo cliente -
# cobre qualquer reserva futura razoavel sem varrer a agenda inteira.
BUSCA_JANELA_DIAS = 60

_DESCRICAO_RE = re.compile(
    r"Cliente:\s*(?P<nome>.*?)\s*\|\s*Tel:\s*(?P<telefone>.*?)\s*\|\s*Pessoas:\s*(?P<pessoas>\d+)\s*\|\s*Notas:\s*(?P<notas>.*)"
)


class GoogleCalendarError(Exception):
    """Erro ao falar com a Google Calendar API (config ausente, auth ou rede/API)."""


@dataclass
class Disponibilidade:
    disponivel: bool
    total_reservas: int
    horarios_alternativos: list[str]
    dia_lotado: bool


@dataclass
class ReservaCriada:
    event_id: str
    horario_inicio: str
    horario_fim: str


@dataclass
class ReservaEncontrada:
    event_id: str
    nome_cliente: str
    telefone: str
    data: str
    horario_inicio: str
    horario_fim: str
    num_pessoas: int
    observacoes: str | None


def _somente_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def _parse_evento(item: dict) -> ReservaEncontrada:
    """Reconstroi os dados da reserva a partir do evento salvo por `criar_reserva`/
    `editar_reserva` - a descricao segue sempre o mesmo formato fixo (ver corpo do evento
    montado abaixo), entao o parse por regex e confiavel para eventos criados pelo agente.
    Eventos manuais na mesma agenda (fora do formato) nao aparecem aqui pois a busca ja
    filtra por extendedProperties.private.origem=agente_bistro."""
    descricao = item.get("description", "")
    match = _DESCRICAO_RE.search(descricao)
    inicio = datetime.datetime.fromisoformat(item["start"]["dateTime"])
    fim = datetime.datetime.fromisoformat(item["end"]["dateTime"])
    notas = match.group("notas").strip() if match else ""

    return ReservaEncontrada(
        event_id=item["id"],
        nome_cliente=match.group("nome").strip() if match else item.get("summary", ""),
        telefone=match.group("telefone").strip() if match else "",
        data=inicio.date().isoformat(),
        horario_inicio=inicio.strftime("%H:%M"),
        horario_fim=fim.strftime("%H:%M"),
        num_pessoas=int(match.group("pessoas")) if match else 0,
        observacoes=notas if notas and notas != "-" else None,
    )


@lru_cache
def _get_service():
    settings = get_settings()
    if not settings.google_calendar_id or not settings.google_credentials_path:
        raise GoogleCalendarError(
            "GOOGLE_CALENDAR_ID/GOOGLE_CREDENTIALS_PATH nao configurados no .env - "
            "reservas automaticas estao desativadas."
        )
    try:
        credentials = service_account.Credentials.from_service_account_file(
            settings.google_credentials_path, scopes=_SCOPES
        )
    except (FileNotFoundError, ValueError) as exc:
        raise GoogleCalendarError(
            f"Nao foi possivel carregar as credenciais da conta de servico em "
            f"'{settings.google_credentials_path}': {exc}"
        ) from exc
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _slots_do_dia() -> list[datetime.time]:
    base = datetime.datetime(2000, 1, 1)
    cursor = base.replace(hour=RESERVA_ABERTURA.hour, minute=RESERVA_ABERTURA.minute)
    fechamento = base.replace(hour=RESERVA_FECHAMENTO.hour, minute=RESERVA_FECHAMENTO.minute)
    ultimo_inicio = fechamento - datetime.timedelta(hours=RESERVA_DURACAO_HORAS)

    slots = []
    while cursor <= ultimo_inicio:
        slots.append(cursor.time())
        cursor += datetime.timedelta(minutes=RESERVA_GRANULARIDADE_MINUTOS)
    return slots


def _parse_janela(data: str, horario: str) -> tuple[datetime.datetime, datetime.datetime]:
    try:
        dia = datetime.date.fromisoformat(data)
        hora = datetime.datetime.strptime(horario, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Data/horario invalidos (esperado AAAA-MM-DD e HH:MM): {exc}") from exc

    inicio = datetime.datetime.combine(dia, hora, tzinfo=TZ_SAO_PAULO)
    fim = inicio + datetime.timedelta(hours=RESERVA_DURACAO_HORAS)
    abertura = datetime.datetime.combine(dia, RESERVA_ABERTURA, tzinfo=TZ_SAO_PAULO)
    fechamento = datetime.datetime.combine(dia, RESERVA_FECHAMENTO, tzinfo=TZ_SAO_PAULO)

    if inicio < abertura or fim > fechamento:
        raise ValueError(
            f"Horario {horario} fora da janela de reservas "
            f"({RESERVA_ABERTURA.strftime('%H:%M')} as {RESERVA_FECHAMENTO.strftime('%H:%M')}, "
            f"considerando {RESERVA_DURACAO_HORAS}h de duracao por reserva)"
        )

    return inicio, fim


async def _contar_reservas(inicio: datetime.datetime, fim: datetime.datetime) -> int:
    service = _get_service()
    settings = get_settings()

    def _list() -> int:
        response = (
            service.events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=inicio.isoformat(),
                timeMax=fim.isoformat(),
                singleEvents=True,
                privateExtendedProperty=f"{_ORIGEM_KEY}={_ORIGEM_VALUE}",
            )
            .execute()
        )
        return len(response.get("items", []))

    try:
        return await asyncio.to_thread(_list)
    except HttpError as exc:
        raise GoogleCalendarError(f"Falha ao consultar a Google Agenda: {exc}") from exc


async def verificar_disponibilidade(data: str, horario_solicitado: str, num_pessoas: int) -> Disponibilidade:
    """Conta reservas ja existentes no bloco de 2h pedido; se lotado, varre os demais
    blocos do mesmo dia em busca de alternativas com vaga. `num_pessoas` nao entra na
    conta (a capacidade e por numero de reservas, nao de lugares - ver docstring do
    modulo) - fica no assinatura so pra manter o mesmo formato de dados de `criar_reserva`."""
    settings = get_settings()
    capacidade = settings.max_capacity_per_slot

    inicio, fim = _parse_janela(data, horario_solicitado)
    total = await _contar_reservas(inicio, fim)

    if total < capacidade:
        return Disponibilidade(disponivel=True, total_reservas=total, horarios_alternativos=[], dia_lotado=False)

    dia = datetime.date.fromisoformat(data)
    alternativos: list[str] = []
    for slot in _slots_do_dia():
        horario_str = slot.strftime("%H:%M")
        if horario_str == horario_solicitado:
            continue
        slot_inicio = datetime.datetime.combine(dia, slot, tzinfo=TZ_SAO_PAULO)
        slot_fim = slot_inicio + datetime.timedelta(hours=RESERVA_DURACAO_HORAS)
        if await _contar_reservas(slot_inicio, slot_fim) < capacidade:
            alternativos.append(horario_str)

    return Disponibilidade(
        disponivel=False,
        total_reservas=total,
        horarios_alternativos=alternativos,
        dia_lotado=not alternativos,
    )


def _montar_evento(
    *,
    inicio: datetime.datetime,
    fim: datetime.datetime,
    nome_cliente: str,
    telefone: str,
    num_pessoas: int,
    observacoes: str | None,
) -> dict:
    return {
        "summary": f"Reserva: {nome_cliente} - {num_pessoas} pessoas",
        "description": (
            f"Cliente: {nome_cliente} | Tel: {telefone} | Pessoas: {num_pessoas} | "
            f"Notas: {observacoes or '-'}"
        ),
        "start": {"dateTime": inicio.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": fim.isoformat(), "timeZone": "America/Sao_Paulo"},
        "extendedProperties": {"private": {_ORIGEM_KEY: _ORIGEM_VALUE}},
    }


async def criar_reserva(
    *,
    nome_cliente: str,
    telefone: str,
    data: str,
    horario: str,
    num_pessoas: int,
    observacoes: str | None = None,
) -> ReservaCriada:
    """Reconfirma a disponibilidade (mitiga corrida entre a checagem que o LLM fez antes e
    esta chamada) e so entao cria o evento na Google Agenda."""
    disponibilidade = await verificar_disponibilidade(data, horario, num_pessoas)
    if not disponibilidade.disponivel:
        raise GoogleCalendarError(
            f"Horario {horario} em {data} nao esta mais disponivel "
            f"(capacidade {disponibilidade.total_reservas}/{get_settings().max_capacity_per_slot})"
        )

    inicio, fim = _parse_janela(data, horario)
    service = _get_service()
    settings = get_settings()
    evento = _montar_evento(
        inicio=inicio, fim=fim, nome_cliente=nome_cliente, telefone=telefone,
        num_pessoas=num_pessoas, observacoes=observacoes,
    )

    def _insert() -> dict:
        return service.events().insert(calendarId=settings.google_calendar_id, body=evento).execute()

    try:
        criado = await asyncio.to_thread(_insert)
    except HttpError as exc:
        raise GoogleCalendarError(f"Falha ao criar reserva na Google Agenda: {exc}") from exc

    return ReservaCriada(
        event_id=criado["id"],
        horario_inicio=inicio.strftime("%H:%M"),
        horario_fim=fim.strftime("%H:%M"),
    )


async def buscar_reservas(
    *,
    nome_cliente: str | None = None,
    telefone: str | None = None,
    data: str | None = None,
) -> list[ReservaEncontrada]:
    """Lista reservas do agente (mesmo filtro de origem usado em `_contar_reservas`) dentro
    da janela de busca, filtrando em Python por nome (substring, case-insensitive) e/ou
    telefone (comparando so os digitos, pra nao quebrar por causa de formatacao tipo
    "+55 11 9...". Se `data` vier preenchida, restringe a busca ao dia exato; senao varre
    os proximos `BUSCA_JANELA_DIAS` dias a partir de agora. Usada pelo LLM para achar o
    `event_id` antes de chamar `editar_reserva`/`cancelar_reserva` - o cliente nunca sabe
    esse id de cor."""
    service = _get_service()
    settings = get_settings()

    if data:
        dia = datetime.date.fromisoformat(data)
        time_min = datetime.datetime.combine(dia, datetime.time.min, tzinfo=TZ_SAO_PAULO)
        time_max = datetime.datetime.combine(dia, datetime.time.max, tzinfo=TZ_SAO_PAULO)
    else:
        agora = datetime.datetime.now(TZ_SAO_PAULO)
        time_min = agora
        time_max = agora + datetime.timedelta(days=BUSCA_JANELA_DIAS)

    def _list() -> list[dict]:
        response = (
            service.events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                privateExtendedProperty=f"{_ORIGEM_KEY}={_ORIGEM_VALUE}",
            )
            .execute()
        )
        return response.get("items", [])

    try:
        items = await asyncio.to_thread(_list)
    except HttpError as exc:
        raise GoogleCalendarError(f"Falha ao buscar reservas na Google Agenda: {exc}") from exc

    resultados = [_parse_evento(item) for item in items]

    if telefone:
        alvo_telefone = _somente_digitos(telefone)
        if alvo_telefone:
            resultados = [r for r in resultados if alvo_telefone in _somente_digitos(r.telefone)]

    if nome_cliente:
        alvo_nome = nome_cliente.strip().casefold()
        resultados = [r for r in resultados if alvo_nome in r.nome_cliente.casefold()]

    return resultados


async def cancelar_reserva(*, event_id: str) -> None:
    """Cancela (deleta) uma reserva ja localizada via `buscar_reservas`."""
    service = _get_service()
    settings = get_settings()

    def _delete() -> None:
        service.events().delete(calendarId=settings.google_calendar_id, eventId=event_id).execute()

    try:
        await asyncio.to_thread(_delete)
    except HttpError as exc:
        raise GoogleCalendarError(f"Falha ao cancelar reserva na Google Agenda: {exc}") from exc


async def editar_reserva(
    *,
    event_id: str,
    nova_data: str | None = None,
    novo_horario: str | None = None,
    novo_num_pessoas: int | None = None,
    novas_observacoes: str | None = None,
) -> ReservaCriada:
    """Le a reserva atual (para reaproveitar nome/telefone, que o cliente nao repete ao
    editar) e sobrescreve so os campos informados. Se data e/ou horario mudarem, reconfirma
    disponibilidade no novo slot antes de gravar - mesma logica de corrida do `criar_reserva`,
    so que aqui a propria reserva sendo editada nao entra na contagem do slot novo (ela ainda
    esta no slot antigo no momento da checagem)."""
    service = _get_service()
    settings = get_settings()

    def _get() -> dict:
        return service.events().get(calendarId=settings.google_calendar_id, eventId=event_id).execute()

    try:
        atual = await asyncio.to_thread(_get)
    except HttpError as exc:
        raise GoogleCalendarError(f"Reserva nao encontrada na Google Agenda ({event_id}): {exc}") from exc

    existente = _parse_evento(atual)
    data_final = nova_data or existente.data
    horario_final = novo_horario or existente.horario_inicio
    pessoas_final = novo_num_pessoas or existente.num_pessoas
    observacoes_final = novas_observacoes if novas_observacoes is not None else existente.observacoes

    if (data_final, horario_final) != (existente.data, existente.horario_inicio):
        disponibilidade = await verificar_disponibilidade(data_final, horario_final, pessoas_final)
        if not disponibilidade.disponivel:
            raise GoogleCalendarError(
                f"Horario {horario_final} em {data_final} nao esta disponivel para a alteracao "
                f"(capacidade {disponibilidade.total_reservas}/{settings.max_capacity_per_slot})"
            )

    inicio, fim = _parse_janela(data_final, horario_final)
    evento = _montar_evento(
        inicio=inicio, fim=fim, nome_cliente=existente.nome_cliente, telefone=existente.telefone,
        num_pessoas=pessoas_final, observacoes=observacoes_final,
    )

    def _update() -> dict:
        return service.events().update(calendarId=settings.google_calendar_id, eventId=event_id, body=evento).execute()

    try:
        atualizado = await asyncio.to_thread(_update)
    except HttpError as exc:
        raise GoogleCalendarError(f"Falha ao atualizar reserva na Google Agenda: {exc}") from exc

    return ReservaCriada(
        event_id=atualizado["id"],
        horario_inicio=inicio.strftime("%H:%M"),
        horario_fim=fim.strftime("%H:%M"),
    )
