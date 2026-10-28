"""Motor v2 sem rede: a decisao (funcoes puras), a validacao do entendimento e o grafo
LangGraph inteiro com um chat model falso no lugar do LLM e uma agenda falsa."""

from __future__ import annotations

import asyncio
import datetime

import pytest
from langchain_core.messages import AIMessage

from app import cardapio, motor_v2, politica, redacao, reservas
from app import entendimento as nlu
from app.llm_providers.chat_models import _thinking_config, criar_modelo, uso
from app.pricing_llm import NormalizedUsage
from app.settings_service import ActiveLLMSettings

E = nlu.Entendimento
A = politica.Acao
COM_NOME = {"nome": "Maria", "nome_pedido": True}
# Data sempre no futuro e dentro da janela de 1 ano aceita pela leitura (app/entendimento.py).
DATA = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
RESERVA = {"data": DATA, "horario": "20:00", "num_pessoas": 4}


# --- Decisao em codigo (app/politica.py) -------------------------------------------------

@pytest.mark.parametrize("estado, entendido, agenda, tipo, extra", [
    # Abertura: o nome antes de tudo, uma unica vez.
    ({}, E(), False, politica.PEDIR_NOME, {}),
    ({}, E(itens=["Petit Gateau com Sorvete"]), False, politica.PEDIR_NOME, {}),
    ({}, E(nome_cliente="Maria"), False, politica.RESPONDER, {}),
    ({"nome_pedido": True}, E(), False, politica.RESPONDER, {}),   # ignorou a pergunta: segue
    # Transferencias imediatas (vencem ate a abertura).
    ({}, E(intencao=nlu.PEDIU_HUMANO), False, politica.TRANSFERIR, {"motivo": "pediu_humano"}),
    (COM_NOME, E(intencao=nlu.RECLAMACAO), False, politica.TRANSFERIR, {"motivo": "reclamacao"}),
    (COM_NOME, E(intencao=nlu.PAGAMENTO_OU_NOTA), False, politica.TRANSFERIR, {"motivo": "pagamento_ou_nota_fiscal"}),
    (COM_NOME, E(intencao=nlu.DELIVERY_OU_EVENTO), False, politica.TRANSFERIR, {"motivo": "delivery_encomenda_ou_evento"}),
    (COM_NOME, E(intencao=nlu.PRECISA_CONFIRMAR), False, politica.TRANSFERIR, {"motivo": "duvida_para_cozinha_ou_gerente"}),
    # Reserva: pede um dado por vez, na ordem.
    (COM_NOME, E(intencao=nlu.FAZER_RESERVA), False, politica.PERGUNTAR_RESERVA, {"slot": "data"}),
    (COM_NOME, E(intencao=nlu.FAZER_RESERVA, reserva={"data": DATA}), False, politica.PERGUNTAR_RESERVA, {"slot": "horario"}),
    ({**COM_NOME, "fluxo": "criar", "reserva": {"data": DATA, "horario": "20:00"}}, E(), False, politica.PERGUNTAR_RESERVA, {"slot": "num_pessoas"}),
    ({"nome_pedido": True, "fluxo": "criar", "reserva": RESERVA}, E(), False, politica.PERGUNTAR_RESERVA, {"slot": "nome"}),
    # Reserva completa: sem agenda vai para a equipe; com agenda, consulta a vaga.
    (COM_NOME, E(intencao=nlu.FAZER_RESERVA, reserva=RESERVA), False, politica.TRANSFERIR, {"motivo": "reserva"}),
    (COM_NOME, E(intencao=nlu.FAZER_RESERVA, reserva=RESERVA), True, politica.OPERACAO, {"operacao": "verificar"}),
    # Alterar/cancelar: sem agenda, equipe; com agenda, busca a reserva.
    (COM_NOME, E(intencao=nlu.CANCELAR_RESERVA), False, politica.TRANSFERIR, {"motivo": "alterar_ou_cancelar_reserva"}),
    (COM_NOME, E(intencao=nlu.CANCELAR_RESERVA), True, politica.OPERACAO, {"operacao": "buscar"}),
    (COM_NOME, E(intencao=nlu.ALTERAR_RESERVA), True, politica.OPERACAO, {"operacao": "buscar"}),
    # Despedida.
    (COM_NOME, E(intencao=nlu.ENCERRANDO), False, politica.DESPEDIR, {}),
], ids=[
    "1a-mensagem-pede-nome", "pergunta-de-prato-antes-do-nome", "disse-o-nome", "insistiu-sem-nome",
    "pediu-humano-vence-abertura", "reclamacao", "pagamento", "evento", "duvida-cozinha",
    "reserva-pede-data", "reserva-pede-horario", "reserva-pede-pessoas", "reserva-pede-nome",
    "reserva-completa-sem-agenda-transfere", "reserva-completa-com-agenda-verifica",
    "cancelar-sem-agenda", "cancelar-com-agenda-busca", "alterar-com-agenda-busca", "despedida",
])
def test_decidir(estado, entendido, agenda, tipo, extra):
    acao, _ = politica.decidir(estado, entendido, agenda=agenda)
    assert acao.tipo == tipo
    for chave, valor in extra.items():
        assert getattr(acao, chave) == valor


def test_pedir_nome_so_uma_vez():
    acao, estado = politica.decidir({}, E(), agenda=False)
    assert acao.tipo == politica.PEDIR_NOME and estado["nome_pedido"]
    acao, _ = politica.decidir(estado, E(), agenda=False)
    assert acao.tipo == politica.RESPONDER


def test_reserva_pedida_antes_do_nome_continua_depois():
    acao, estado = politica.decidir({}, E(intencao=nlu.FAZER_RESERVA, reserva={"num_pessoas": 2}), agenda=False)
    assert acao.tipo == politica.PEDIR_NOME
    acao, estado = politica.decidir(estado, E(nome_cliente="Ana"), agenda=False)
    assert (acao.tipo, acao.slot) == (politica.PERGUNTAR_RESERVA, "data")
    assert estado["reserva"] == {"num_pessoas": 2}


def test_reserva_sem_agenda_leva_os_dados_e_limpa_o_fluxo():
    acao, estado = politica.decidir(COM_NOME, E(intencao=nlu.FAZER_RESERVA, reserva=RESERVA), agenda=False)
    assert acao.dados == {**RESERVA, "nome": "Maria"}
    assert "fluxo" not in estado and "reserva" not in estado


def test_item_do_cardapio_vai_na_resposta_mas_nao_na_transferencia():
    acao, _ = politica.decidir(COM_NOME, E(itens=["Pudim de Leite Condensado", "Petit Gateau com Sorvete"]), agenda=False)
    assert acao.item == "Pudim de Leite Condensado"   # o primeiro vai na foto
    acao, _ = politica.decidir(COM_NOME, E(intencao=nlu.PEDIU_HUMANO, itens=["Pudim de Leite Condensado"]), agenda=False)
    assert acao.item is None


# --- Confirmacao pendente -------------------------------------------------------------------

PENDENTE_CRIAR = {**COM_NOME, "fluxo": "criar", "reserva": RESERVA,
                  "pendente": {"operacao": "criar", "dados": {**RESERVA, "nome": "Maria"}}}


def test_sim_executa_a_operacao_pendente():
    acao, estado = politica.decidir(PENDENTE_CRIAR, E(confirmou=True), agenda=True)
    assert (acao.tipo, acao.operacao) == (politica.OPERACAO, politica.OP_CRIAR)
    assert "pendente" not in estado


def test_nao_pergunta_o_que_ajustar():
    acao, estado = politica.decidir(PENDENTE_CRIAR, E(recusou=True), agenda=True)
    assert acao.tipo == politica.PERGUNTAR_ALTERACAO and "pendente" not in estado


def test_dado_novo_na_confirmacao_verifica_de_novo():
    acao, _ = politica.decidir(PENDENTE_CRIAR, E(recusou=True, reserva={"horario": "21:00"}), agenda=True)
    assert (acao.tipo, acao.operacao, acao.dados["horario"]) == (politica.OPERACAO, politica.OP_VERIFICAR, "21:00")


def test_mudar_de_assunto_mantem_a_confirmacao():
    acao, estado = politica.decidir(PENDENTE_CRIAR, E(pergunta_do_cliente="tem estacionamento?"), agenda=True)
    assert acao.tipo == politica.RESPONDER and estado["pendente"]


def test_despedida_derruba_a_confirmacao():
    acao, estado = politica.decidir(PENDENTE_CRIAR, E(intencao=nlu.ENCERRANDO), agenda=True)
    assert acao.tipo == politica.DESPEDIR and "pendente" not in estado


# --- Depois da agenda -------------------------------------------------------------------------

VERIFICAR = A(politica.OPERACAO, operacao=politica.OP_VERIFICAR, dados={**RESERVA, "nome": "Maria"})
EM_CRIACAO = {**COM_NOME, "fluxo": "criar", "reserva": dict(RESERVA)}


def test_vaga_livre_pede_confirmacao():
    acao, estado = politica.depois_da_agenda(EM_CRIACAO, VERIFICAR, {"sucesso": True, "disponivel": True}, E())
    assert acao.tipo == politica.CONFIRMAR_RESERVA
    assert estado["pendente"]["operacao"] == politica.OP_CRIAR


def test_sem_vaga_oferece_alternativas():
    acao, estado = politica.depois_da_agenda(
        EM_CRIACAO, VERIFICAR, {"sucesso": True, "disponivel": False, "horarios_alternativos": ["19:00", "21:30"]}, E()
    )
    assert acao.tipo == politica.OFERECER_HORARIOS and acao.dados["alternativos"] == ["19:00", "21:30"]
    assert "horario" not in estado["reserva"]


def test_dia_lotado_pede_outra_data():
    acao, estado = politica.depois_da_agenda(
        EM_CRIACAO, VERIFICAR, {"sucesso": True, "disponivel": False, "horarios_alternativos": [], "dia_lotado": True}, E()
    )
    assert acao.tipo == politica.RESERVA_INDISPONIVEL and acao.dados["dia_lotado"]
    assert "data" not in estado["reserva"]


def test_fora_da_janela_pede_outro_horario():
    acao, _ = politica.depois_da_agenda(EM_CRIACAO, VERIFICAR, {"sucesso": False, "tecnico": False, "erro": "fora da janela"}, E())
    assert acao.tipo == politica.RESERVA_INDISPONIVEL


def test_agenda_fora_do_ar_passa_para_a_equipe():
    acao, estado = politica.depois_da_agenda(EM_CRIACAO, VERIFICAR, {"sucesso": False, "tecnico": True, "erro": "sem credencial"}, E())
    assert (acao.tipo, acao.motivo) == (politica.TRANSFERIR, politica.MOTIVO_FALHA_AGENDA)
    assert acao.dados["num_pessoas"] == 4 and "fluxo" not in estado


def test_reserva_criada_limpa_o_fluxo():
    criar = A(politica.OPERACAO, operacao=politica.OP_CRIAR, dados={**RESERVA, "nome": "Maria"})
    acao, estado = politica.depois_da_agenda(EM_CRIACAO, criar, {"sucesso": True, "horario_fim": "22:00"}, E())
    assert acao.tipo == politica.RESERVA_CRIADA and acao.dados["horario_fim"] == "22:00"
    assert "fluxo" not in estado and "reserva" not in estado


ACHADA = {"event_id": "ev1", "nome_cliente": "Maria", "data": DATA, "horario": "20:00", "num_pessoas": 4}
BUSCAR = A(politica.OPERACAO, operacao=politica.OP_BUSCAR, dados={})


def test_cancelar_acha_uma_e_pede_confirmacao():
    acao, estado = politica.depois_da_agenda({**COM_NOME, "fluxo": "cancelar"}, BUSCAR, {"sucesso": True, "reservas": [ACHADA]}, E())
    assert acao.tipo == politica.CONFIRMAR_CANCELAMENTO
    assert estado["pendente"] == {"operacao": politica.OP_CANCELAR, "dados": {"event_id": "ev1"}}


def test_alterar_com_horario_novo_pede_confirmacao_da_mudanca():
    entendido = E(intencao=nlu.ALTERAR_RESERVA, reserva={"data": DATA, "horario": "21:00"})
    acao, estado = politica.depois_da_agenda({**COM_NOME, "fluxo": "alterar"}, BUSCAR, {"sucesso": True, "reservas": [ACHADA]}, entendido)
    assert acao.tipo == politica.CONFIRMAR_ALTERACAO
    assert acao.dados["mudancas"] == {"horario": "21:00"}   # a data igual era so o filtro
    assert estado["pendente"]["dados"] == {"event_id": "ev1", "horario": "21:00"}


def test_alterar_sem_dizer_o_que_pergunta():
    acao, _ = politica.depois_da_agenda({**COM_NOME, "fluxo": "alterar"}, BUSCAR, {"sucesso": True, "reservas": [ACHADA]}, E())
    assert acao.tipo == politica.PERGUNTAR_ALTERACAO


def test_varias_reservas_pergunta_qual():
    outra = {**ACHADA, "event_id": "ev2", "data": "2030-01-12"}
    acao, _ = politica.depois_da_agenda({**COM_NOME, "fluxo": "cancelar"}, BUSCAR, {"sucesso": True, "reservas": [ACHADA, outra]}, E())
    assert acao.tipo == politica.ESCOLHER_RESERVA and len(acao.dados["reservas"]) == 2


def test_nao_achou_duas_vezes_passa_para_a_equipe():
    acao, estado = politica.depois_da_agenda({**COM_NOME, "fluxo": "cancelar"}, BUSCAR, {"sucesso": True, "reservas": []}, E())
    assert acao.tipo == politica.RESERVA_NAO_ENCONTRADA
    acao, _ = politica.depois_da_agenda(estado, BUSCAR, {"sucesso": True, "reservas": []}, E())
    assert (acao.tipo, acao.motivo) == (politica.TRANSFERIR, politica.MOTIVO_ALTERAR_CANCELAR)


def test_cancelada():
    cancelar = A(politica.OPERACAO, operacao=politica.OP_CANCELAR, dados={"event_id": "ev1"})
    acao, estado = politica.depois_da_agenda({**COM_NOME, "fluxo": "cancelar", "alvo": ACHADA}, cancelar, {"sucesso": True}, E())
    assert acao.tipo == politica.RESERVA_CANCELADA and "alvo" not in estado


def test_desistiu_de_cancelar_mantem_a_reserva():
    estado = {**COM_NOME, "fluxo": "cancelar", "alvo": ACHADA, "pendente": {"operacao": "cancelar", "dados": {"event_id": "ev1"}}}
    acao, estado = politica.decidir(estado, E(recusou=True), agenda=True)
    assert acao.tipo == politica.RESERVA_MANTIDA and "fluxo" not in estado


# --- Entendimento: leitura tolerante e validacao em codigo -----------------------------------

ITENS = cardapio.ler(redacao.PERSONA)
HOJE = datetime.date(2030, 1, 1)


def test_interpretar_json_torto():
    e = nlu.interpretar({"nome_cliente": " Maria ", "reserva_pessoas": 4.0, "intencao": "inventada", "itens_do_cardapio": "x"})
    assert (e.nome_cliente, e.reserva, e.intencao, e.itens) == ("Maria", {"num_pessoas": 4}, nlu.CONVERSA, [])
    assert nlu.interpretar({"reserva_pessoas": True}).reserva == {}
    assert nlu.interpretar(None).intencao == nlu.CONVERSA


@pytest.mark.parametrize("vazio", ["-", " - ", "—", "n/a", "nenhum", "Nenhuma.", "null", "não informado", "."])
def test_preenchimento_do_modelo_vale_como_vazio(vazio):
    e = nlu.interpretar({"pergunta_do_cliente": vazio, "nome_cliente": vazio, "reserva_observacoes": vazio})
    assert (e.pergunta_do_cliente, e.nome_cliente, e.reserva) == ("", "", {})


def test_validar_descarta_o_que_o_cliente_nao_disse():
    e = E(nome_cliente="Carla", itens=["parmegiana de frango", "Lasanha"],
          reserva={"data": "2029-12-31", "horario": "8:30", "num_pessoas": 6})
    descartado = nlu.validar(e, textos_cliente=["quero reservar para 4 pessoas"], itens_cardapio=ITENS, hoje=HOJE)
    assert e.nome_cliente == "" and descartado["nome_cliente"] == "Carla"     # nome inventado
    assert e.itens == ["Parmegiana de Frango"] and descartado["itens"] == ["Lasanha"]
    assert "data" not in e.reserva                                              # data no passado
    assert e.reserva["horario"] == "08:30"
    assert "num_pessoas" not in e.reserva                                       # disse 4, nao 6


@pytest.mark.parametrize("n, texto, aceito", [
    (4, "somos 4", True), (4, "seremos quatro", True), (2, "é para um casal", True),
    (2, "eu e minha esposa", True), (2, "duas pessoas", True), (3, "somos 4", False),
])
def test_pessoas_precisam_estar_no_texto(n, texto, aceito):
    e = E(reserva={"num_pessoas": n})
    nlu.validar(e, textos_cliente=[texto], itens_cardapio=ITENS, hoje=HOJE)
    assert ("num_pessoas" in e.reserva) == aceito


def test_nome_dito_pelo_cliente_vale():
    e = E(nome_cliente="José")
    nlu.validar(e, textos_cliente=["meu nome é jose"], itens_cardapio=ITENS, hoje=HOJE)
    assert e.nome_cliente == "José"


# --- Cardapio lido da base de conhecimento ---------------------------------------------------

def test_cardapio_padrao_e_lido_inteiro():
    assert len(ITENS) > 50
    file = cardapio.buscar(ITENS, "Filé Mignon ao Molho Madeira com Champignon")
    assert file.preco == "R$ 84,90" and file.foto_url.endswith(".jpg") and file.secao == "Especialidades da Casa"
    assert cardapio.buscar(ITENS, "agua sem gas").foto_url is None
    assert cardapio.buscar(ITENS, "parmegiana") is None          # ambiguo: 4 parmegianas
    assert cardapio.valor("R$ 1.234,5") == "1234,50"
    assert "84,90" in cardapio.precos(ITENS)


# --- Verificacao (app/response_grounding.py) -------------------------------------------------

from app import response_grounding as rg  # noqa: E402

RESP = A(politica.RESPONDER)


@pytest.mark.parametrize("texto, primeira, acao, barra", [
    ("O Petit Gateau sai por 💰 Preço: R$ 24,90", False, RESP, False),
    ("O Petit Gateau sai por R$ 19,90", False, RESP, True),                       # preco inventado
    ("Veja: https://example.com/fotos/pudim.jpg", False, RESP, True),
    ("Veja ![pudim](https://x.com/p.png)", False, RESP, True),
    ("O cardápio completo está em https://example.com/cardapio", False, RESP, False),
    ("Posso te enviar a foto do pudim?", False, RESP, True),
    ("Quer ver a foto?", False, RESP, True),
    ("Quer ver as sobremesas?", False, RESP, False),
    ("Olá! Bem-vindo ao Bistrô Aurora. Temos risotos.", False, RESP, True),          # abertura repetida
    ("Olá! Bem-vindo ao Bistrô Aurora. Qual seu nome?", True, A(politica.PEDIR_NOME), False),
    ("O petit gateau é R$ 24,90. Qual seu nome?", True, A(politica.PEDIR_NOME), True),
    ("结案。", False, RESP, True),
    ("👍", False, RESP, True),
])
def test_validar_resposta(texto, primeira, acao, barra):
    r = rg.validar(texto, acao=acao, itens_cardapio=ITENS, primeira_resposta=primeira)
    assert r.valido is (not barra), r.motivo


@pytest.mark.parametrize("tipo", sorted(politica.TERMINAM_COM_PERGUNTA))
def test_texto_pronto_das_acoes_com_pergunta_termina_com_pergunta(tipo):
    acao = A(tipo, slot="data", dados={"horario": "20:00", "alternativos": ["19:00"], "reservas": [ACHADA], "atual": ACHADA, "mudancas": {"horario": "21:00"}})
    assert "?" in rg.fallback(acao, item=None, com_foto=False, primeira_resposta=True)


def test_texto_pronto_do_item_usa_o_cardapio():
    item = cardapio.buscar(ITENS, "Pudim de Leite Condensado")
    texto = rg.fallback(RESP, item=item, com_foto=True, primeira_resposta=False)
    assert texto.startswith("Segue a foto do *Pudim de Leite Condensado*:") and "R$ 17,90" in texto


# --- Base de conhecimento do painel -------------------------------------------------------------

PROMPT_V1 = (
    "Use a ferramenta `responder_cliente` com bolhas e dados_extraidos...\n\n"
    "## CARDÁPIO OFICIAL\n### Sobremesas\n| Pudim de Leite Condensado | R$ 19,90 | Clássico. | |\n"
)


def test_prompt_do_motor_antigo_usa_a_persona_nova_com_o_cardapio_do_painel():
    persona = redacao.persona_ativa(PROMPT_V1)
    assert "responder_cliente" not in persona
    assert "R$ 19,90" in persona and "R$ 84,90" not in persona       # o cardapio editado no painel vale
    assert "Tom e formato" in persona


@pytest.mark.parametrize("prompt", [None, "", "  "])
def test_painel_vazio_usa_a_persona_do_repositorio(prompt):
    assert redacao.persona_ativa(prompt) == redacao.PERSONA


def test_base_nova_do_painel_vale_como_esta():
    assert redacao.persona_ativa("Você é o atendente do Bistrô Aurora.") == "Você é o atendente do Bistrô Aurora."


@pytest.mark.parametrize("prompt, ligado", [
    (redacao.PERSONA, False),
    ("... [reservas-pela-agenda] ...", True),
    ("chame verificar_disponibilidade antes", True),   # versao especial antiga (ex.: data comemorativa)
])
def test_reservas_pela_agenda(prompt, ligado):
    assert redacao.reservas_pela_agenda(prompt) is ligado


def test_tarefa_de_transferencia_nao_manda_perguntar():
    bloco = redacao.montar_bloco(
        acao=A(politica.TRANSFERIR, motivo="pediu_humano"), entendido=None, estado={}, item=None,
        com_foto=False, canal_resposta="TEXTO", primeira_resposta=False,
    )
    assert "Termine perguntando" not in bloco and "Não peça permissão" in bloco


# --- Grafo inteiro com chat model falso -------------------------------------------------------

class _ModeloFalso:
    """Faz o papel do chat model do LangChain: devolve um entendimento fixo e uma lista de
    textos, e guarda o bloco de sistema que a redacao recebeu em cada chamada."""

    def __init__(self, entendido: dict, textos: list[str]):
        self.entendido, self.textos, self.blocos = entendido, iter(textos), []

    def bind_tools(self, *_a, **_k):
        modelo = self

        class _Extrator:
            async def ainvoke(self, _mensagens):
                return AIMessage(content="", tool_calls=[{"name": nlu.NOME, "args": modelo.entendido, "id": "1"}])

        return _Extrator()

    async def ainvoke(self, mensagens):
        self.blocos.append(mensagens[0].content)
        texto = next(self.textos)
        if texto.startswith("[CORTADA]"):
            return AIMessage(content=texto.removeprefix("[CORTADA]"), response_metadata={"finish_reason": "length"})
        return AIMessage(content=texto)


@pytest.fixture
def rodar_turno(monkeypatch):
    def _rodar(*, entendido: dict, textos: list[str], mensagem: str, estado: dict | None = None,
               history: list[dict] | None = None, prompt: str = "", agenda: dict | None = None):
        falso = _ModeloFalso(entendido, textos)
        monkeypatch.setattr(motor_v2, "criar_modelo", lambda _s: falso)
        chamadas = []

        async def _agenda_falsa(acao, *, telefone):
            chamadas.append((acao.operacao, dict(acao.dados), telefone))
            return (agenda or {})[acao.operacao]

        monkeypatch.setattr(reservas, "executar", _agenda_falsa)
        resultado = asyncio.run(motor_v2.run_turn(
            history=history or [],
            user_message=mensagem,
            llm_settings=ActiveLLMSettings(provider="openai", model="gpt-5.6-luna", api_key="x", system_prompt=prompt),
            estado=estado or {},
            client_phone="+55 19 99999-0000",
            contact_name="Contato WhatsApp",
        ))
        return resultado, falso.blocos, chamadas
    return _rodar


HIST = [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "Olá! ... qual seu nome por favor?"}]


def test_primeira_mensagem_faz_a_abertura_e_pede_o_nome(rodar_turno):
    r, blocos, _ = rodar_turno(
        entendido={"intencao": "conversa"},
        textos=[f"{redacao.ABERTURA}\n\n{redacao.PERGUNTA_NOME}"],
        mensagem="oi, boa tarde",
    )
    assert redacao.ABERTURA in blocos[0] and redacao.PERGUNTA_NOME in blocos[0]
    assert r.estado["nome_pedido"] is True and r.image_url is None and not r.transferir


def test_item_com_foto_manda_a_foto_do_cardapio(rodar_turno):
    r, blocos, _ = rodar_turno(
        entendido={"intencao": "conversa", "itens_do_cardapio": ["Pudim de Leite Condensado"], "pergunta_do_cliente": "quanto é o pudim?"},
        textos=["Segue a foto do *Pudim de Leite Condensado*:\n\nClássico com calda.\n\n💰 Preço: R$ 17,90"],
        mensagem="quanto é o pudim?", estado=COM_NOME, history=HIST,
    )
    assert r.image_url == "https://example.com/fotos/pudim.jpg"
    assert "R$ 17,90" in blocos[0] and r.grounding_valido


def test_texto_que_nao_comeca_pela_foto_e_redigido_de_novo(rodar_turno):
    r, blocos, _ = rodar_turno(
        entendido={"intencao": "conversa", "itens_do_cardapio": ["Pudim de Leite Condensado"]},
        textos=["O pudim custa R$ 17,90.", "Segue a foto do *Pudim de Leite Condensado*:\n\n💰 Preço: R$ 17,90"],
        mensagem="me mostra o pudim", estado=COM_NOME, history=HIST,
    )
    assert len(blocos) == 2 and "CORREÇÃO" in blocos[1]
    assert r.text.startswith("Segue a foto") and r.grounding_valido is False


def test_preco_inventado_duas_vezes_sai_o_texto_pronto(rodar_turno):
    r, _, _ = rodar_turno(
        entendido={"intencao": "conversa", "itens_do_cardapio": ["Pudim de Leite Condensado"]},
        textos=["Segue a foto do *Pudim de Leite Condensado*: R$ 12,00", "Segue a foto do *Pudim de Leite Condensado*: R$ 10,00"],
        mensagem="quanto é o pudim?", estado=COM_NOME, history=HIST,
    )
    assert "R$ 17,90" in r.text and "R$ 12,00" not in r.text
    assert r.image_url and r.trace["texto_pronto"] is True


def test_resposta_cortada_e_redigida_de_novo(rodar_turno):
    r, blocos, _ = rodar_turno(
        entendido={"intencao": "conversa", "pergunta_do_cliente": "abre domingo?"},
        textos=["[CORTADA]Abrimos sim, das 11h", "Abrimos sim, das 11h às 16h. ✨"],
        mensagem="abre domingo?", estado=COM_NOME, history=HIST,
    )
    assert "cortada" in blocos[1] and r.text == "Abrimos sim, das 11h às 16h. ✨"


def test_pediu_humano_transfere_com_nota_montada_em_codigo(rodar_turno):
    r, _, _ = rodar_turno(
        entendido={"intencao": "pediu_humano", "resumo_para_equipe": "Quer falar com o gerente."},
        textos=["Claro! Vou te passar agora para a nossa equipe. 🙏"],
        mensagem="quero falar com uma pessoa", estado=COM_NOME, history=HIST,
    )
    assert r.transferir and r.motivo_transferencia == "pediu_humano"
    assert "Cliente: Maria." in r.resumo_interno and "Quer falar com o gerente." in r.resumo_interno


def test_transferencia_sem_avisar_e_redigida_de_novo(rodar_turno):
    r, blocos, _ = rodar_turno(
        entendido={"intencao": "reclamacao"},
        textos=["Sinto muito pelo ocorrido.", "Sinto muito! Vou passar para a nossa equipe resolver."],
        mensagem="a comida veio fria", estado=COM_NOME, history=HIST,
    )
    assert len(blocos) == 2 and "equipe" in r.text


def test_conversa_do_motor_antigo_nao_pede_o_nome_de_novo(rodar_turno):
    r, _, _ = rodar_turno(
        entendido={"intencao": "conversa", "pergunta_do_cliente": "tem estacionamento?"},
        textos=["Temos sim, na frente do prédio."],
        mensagem="tem estacionamento?", estado={}, history=HIST,
    )
    assert r.trace["acao"]["tipo"] == politica.RESPONDER


def test_reserva_pela_agenda_ponta_a_ponta(rodar_turno):
    agenda = {
        "verificar": {"sucesso": True, "tecnico": False, "disponivel": True},
        "criar": {"sucesso": True, "tecnico": False, "event_id": "ev9", "horario_inicio": "20:00", "horario_fim": "22:00"},
    }
    # Turno 1: dados completos -> consulta a vaga -> pede confirmacao (nada gravado ainda).
    r1, blocos, chamadas = rodar_turno(
        entendido={"intencao": "fazer_reserva", "reserva_data": DATA, "reserva_horario": "20:00", "reserva_pessoas": 4},
        textos=["Temos vaga para 4 pessoas no sábado, 05/01, às 20:00. Posso confirmar?"],
        mensagem="quero reservar para 4 pessoas dia 05/01/2030 às 20h", estado=COM_NOME, history=HIST,
        prompt="[reservas-pela-agenda]\n" + redacao.PERSONA, agenda=agenda,
    )
    assert [c[0] for c in chamadas] == ["verificar"]
    assert r1.trace["acao"]["tipo"] == politica.CONFIRMAR_RESERVA and r1.estado["pendente"]["operacao"] == "criar"
    # Turno 2: "sim" -> grava com o telefone do WhatsApp.
    r2, _, chamadas = rodar_turno(
        entendido={"intencao": "conversa", "confirmou": True},
        textos=["Reserva confirmada para sábado às 20h! ✨"],
        mensagem="sim", estado=r1.estado, history=HIST,
        prompt="[reservas-pela-agenda]\n" + redacao.PERSONA, agenda=agenda,
    )
    assert chamadas == [("criar", {**RESERVA, "nome": "Maria"}, "+55 19 99999-0000")]
    assert r2.trace["acao"]["tipo"] == politica.RESERVA_CRIADA and "pendente" not in r2.estado


def test_sem_a_marca_da_agenda_a_reserva_vai_para_a_equipe(rodar_turno):
    r, _, chamadas = rodar_turno(
        entendido={"intencao": "fazer_reserva", "reserva_data": DATA, "reserva_horario": "20:00", "reserva_pessoas": 4},
        textos=["Anotei tudo! A nossa equipe confirma a reserva com você por aqui."],
        mensagem="mesa para 4 dia 05/01/2030 às 20h", estado=COM_NOME, history=HIST,
    )
    assert chamadas == [] and r.transferir and r.motivo_transferencia == "reserva"
    assert "Reserva:" in r.resumo_interno and "4 pessoa(s)" in r.resumo_interno


# --- Chat models (LangChain) --------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "deepseek"])
def test_os_quatro_provedores_do_agente_continuam(provider):
    modelos = {"openai": "gpt-5.6-luna", "anthropic": "claude-sonnet-5", "gemini": "gemini-3-flash-preview", "deepseek": "deepseek-v4-flash"}
    modelo = criar_modelo(ActiveLLMSettings(provider=provider, model=modelos[provider], api_key="x", system_prompt=""))
    assert modelo is not None


@pytest.mark.parametrize("model, nivel", [
    ("gemini-3-flash-preview", "MINIMAL"), ("gemini-3.5-flash", "MINIMAL"), ("gemini-2.5-flash", None),
])
def test_thinking_minimo_no_gemini_3(model, nivel):
    config = _thinking_config(model)
    assert (config.thinking_level.value if config and config.thinking_level else None) == nivel


def test_custo_tira_o_cache_da_entrada():
    mensagem = AIMessage(content="", usage_metadata={
        "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
        "input_token_details": {"cache_read": 800, "cache_creation": 100},
    })
    assert uso(mensagem) == NormalizedUsage(input_tokens=100, output_tokens=50, cache_read_tokens=800, cache_write_tokens=100)
