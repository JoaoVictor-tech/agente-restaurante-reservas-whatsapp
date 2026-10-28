# Agente de IA para restaurante no WhatsApp, com reservas na Google Agenda

Atendente virtual de WhatsApp para um restaurante. Ele tira dúvidas sobre o cardápio (e manda a
foto do prato), informa horários e endereço, e **faz, altera e cancela reservas direto na Google
Agenda**, respeitando a lotação de cada horário. Quando o assunto foge do que ele pode resolver,
transfere a conversa para a equipe humana.

Foi desenvolvido durante meu estágio para um restaurante real. Esta é uma versão de portfólio: o
restaurante (**Bistrô Aurora**), o cardápio, os preços e os contatos são fictícios, e não há
nenhuma credencial ou dado de cliente.

## O que ele faz

- **Cardápio:** apresenta pratos por categoria, com preço e descrição, e envia a foto do item antes da mensagem.
- **Reservas na Google Agenda:** consulta a disponibilidade, sugere horários alternativos quando está lotado, pede confirmação e só então grava. Também encontra, altera e cancela reservas existentes.
- **Áudio:** entende mensagens de voz (transcrição) e pode responder em áudio (ElevenLabs), falando pouca informação por vez, como numa ligação.
- **Transferência para humano:** passa a conversa para a equipe com uma nota-resumo e cria ou move o card no kanban do CRM.
- **Painel web:** conversas em tempo real (com opção de assumir o atendimento), custos de IA por conversa, troca do modelo de IA, base de conhecimento versionada e um "teste rápido" que simula conversas.

## Arquitetura

As mensagens chegam pela Helena, plataforma de atendimento e CRM, via webhook:

```
WhatsApp → Helena → POST /webhook/helena/{teste|producao}   (FastAPI)
                         │
                         ▼
                   app/pipeline.py
     agrupa mensagens seguidas (Redis) → transcreve áudio → motor v2
                         │
                         ▼
     envia foto → texto ou áudio → nota interna / transferência (API da Helena)
```

### Motor v2: o código decide, a IA entende e escreve

O ponto central do projeto é **não deixar o modelo de linguagem decidir regras de negócio**.
Cada mensagem passa por um grafo [LangGraph](https://www.langchain.com/langgraph) de quatro etapas:

```
START → entender → decidir → redigir → verificar → END
                                ▲            │
                                └────────────┘  reprovou: 1 reescrita com o motivo
```

| Etapa | Quem faz | O que faz |
| --- | --- | --- |
| **Entender** (`app/entendimento.py`) | LLM com saída estruturada | Extrai o que o cliente disse: intenção, nome, itens do cardápio, data, horário e número de pessoas. Tudo é validado em código (ex.: data entre hoje e 1 ano). |
| **Decidir** (`app/politica.py`) | Código (função pura) | A partir do estado da conversa, decide a próxima ação: pedir um dado que falta, consultar a agenda, pedir confirmação, transferir. É quem chama a agenda, nunca o LLM. |
| **Redigir** (`app/redacao.py`) | LLM sem ferramentas | Escreve a mensagem da ação já decidida, no tom do restaurante. |
| **Verificar** (`app/response_grounding.py`) | Código | Barra resposta com preço que não existe no cardápio, link no texto, pergunta faltando etc. Reprovou: o LLM reescreve uma vez; reprovou de novo, sai um texto pronto montado em código. |

Assim, a IA não consegue inventar preço, prometer uma reserva que não foi gravada nem escolher a
foto errada: preço, foto, horário de funcionamento e vagas vêm sempre do código.

## Decisões de projeto

- **Confirmação antes de gravar:** a reserva só é criada depois do "sim" do cliente; o estado fica pendente entre as mensagens.
- **Lotação por horário:** a agenda conta só os eventos criados pelo agente (marcados com uma propriedade privada) e respeita `MAX_CAPACITY_PER_SLOT`.
- **Falha da agenda não perde o cliente:** se a Google Agenda estiver fora do ar, a conversa vai para a equipe com os dados já coletados.
- **Provedor de IA trocável em tempo real:** OpenAI, Anthropic, Gemini ou DeepSeek, escolhidos no painel, com o custo de cada chamada registrado.
- **Ambientes de teste e produção** separados por webhook e credenciais, com simulação de falhas (LLM, envio, áudio, foto) só no ambiente de teste.
- **Observabilidade:** cada turno vira um trace no [Langfuse](https://langfuse.com), com a etapa, a ação decidida e o custo.

## Testes

111 testes automatizados cobrem a política (todas as transições de reserva), a leitura do
cardápio, a verificação das respostas e o grafo de ponta a ponta com LLM e agenda simulados.
Rodam sem banco, sem Redis e sem chamar nenhuma API:

```bash
pip install -r requirements-dev.txt
pytest tests -m "not llm"
```

## Como rodar

```bash
cp .env.example .env          # preencha as chaves (LLM, ElevenLabs, Helena, Google)
mkdir credenciais             # coloque aqui a chave JSON da conta de serviço do Google
docker compose up -d --build
```

Sobe a API em `127.0.0.1:8023`, com PostgreSQL e Redis. As migrações (Alembic) rodam
automaticamente antes da API.

- Saúde: http://127.0.0.1:8023/health
- Painel: http://127.0.0.1:8023/dashboard (usuário e senha do `.env`)

Para as reservas, crie uma conta de serviço no Google Cloud, compartilhe a agenda do restaurante
com o e-mail dela e preencha `GOOGLE_CALENDAR_ID` e `GOOGLE_CREDENTIALS_PATH`.

## Estrutura

```
app/
  main.py                   rotas /health e /webhook/helena/{ambiente}
  pipeline.py               orquestra um turno: agrupamento, áudio, motor, envio
  llm.py, motor_v2.py       entrada do turno e grafo LangGraph
  entendimento.py           etapa "entender" (saída estruturada)
  politica.py               etapa "decidir" (regras de negócio)
  redacao.py                etapa "redigir"
  response_grounding.py     etapa "verificar"
  cardapio.py               leitura do cardápio da base de conhecimento
  reservas.py               operações de reserva usadas pela política
  google_calendar_client.py disponibilidade, criação, edição e cancelamento na agenda
  helena_client.py, helena_crm.py  mensagens, notas, transferência e kanban
  stt.py, tts.py            transcrição e resposta em áudio
  observabilidade.py        traces no Langfuse
  dashboard/                painel web (FastAPI + Jinja2 + htmx)
  prompts/                  persona e cardápio (system_static.md) e prompt de leitura
migrations/                 Alembic (PostgreSQL)
tests/                      testes do motor, da política e do cardápio
```

## Tecnologias

Python 3.12 · FastAPI · LangChain / LangGraph · OpenAI, Anthropic, Gemini e DeepSeek · Langfuse ·
PostgreSQL + SQLAlchemy + Alembic · Redis · Google Calendar API · ElevenLabs · API da Helena ·
Docker Compose · pytest / DeepEval

## Autor

João Victor Lopez de Arruda · [GitHub](https://github.com/JoaoVictor-tech) · [LinkedIn](https://linkedin.com/in/joao-victor-arruda)
