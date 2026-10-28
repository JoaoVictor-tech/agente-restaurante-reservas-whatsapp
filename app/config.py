from dataclasses import dataclass
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENVIRONMENTS = ("teste", "producao")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Chaves de LLM abaixo sao usadas apenas como FALLBACK de bootstrap (1a execucao, antes de
    # configurar qualquer coisa em /dashboard/settings). Uma vez configuradas pelo painel, as
    # chaves ficam no banco (criptografadas) e tem prioridade sobre estas do .env.
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    deepseek_api_key: str | None = None
    default_llm_provider: str = "anthropic"
    default_llm_model: str = "claude-sonnet-5"

    # Observabilidade (app/observabilidade.py). Sem as duas chaves fica DESLIGADA: o SDK nem
    # e' importado e nada sai do processo.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # LANGFUSE_BASE_URL e' o nome atual no SDK; LANGFUSE_HOST, o antigo - aceita os dois.
    langfuse_host: str = Field(
        default="http://localhost:3000",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "langfuse_host"),
    )
    # URL que o NAVEGADOR abre (links do painel). So difere da de cima quando a API fala com
    # o Langfuse por um endereco interno (ex.: host.docker.internal no Docker).
    langfuse_public_url: str | None = None
    # Versao do codigo no ar (commit). Vai como tag "release:<versao>" nos traces, para
    # comparar comportamento antes/depois de uma mudanca de prompt ou de regra.
    app_version: str = "dev"

    # Falhas provocadas DE PROPOSITO para testar o diagnostico (app/falhas_simuladas.py).
    # Lista separada por virgula: llm, envio, transferencia, audio, foto. So vale no ambiente
    # "teste" - em "producao" e' ignorada, mesmo que alguem esqueca a variavel ligada.
    simular_falha: str = ""

    # Chave usada para criptografar as chaves de API salvas no banco via /dashboard/settings.
    # Gere uma com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    settings_encryption_key: str

    # OpenAI e usado sempre para transcricao de audio (Whisper), independente do provedor de LLM
    # escolhido para a conversa - e tambem o fallback de bootstrap se "openai" for o provedor ativo.
    openai_api_key: str

    elevenlabs_api_key: str
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    # Antes (ate 2026-07-30) a chamada de sintese nao enviava voice_settings nenhum - ficava no
    # default proprio da voz salvo no painel da ElevenLabs, o que pode ter contribuido pra
    # inconsistencia de pronuncia relatada pelo cliente. Valores abaixo sao os recomendados pela
    # propria documentacao da ElevenLabs (stability ~0.5, similarity ~0.75) com stability um
    # pouco mais alta pra favorecer dicção mais consistente/menos "atropelada".
    elevenlabs_stability: float = 0.55
    elevenlabs_similarity_boost: float = 0.8
    elevenlabs_style: float = 0.0
    elevenlabs_use_speaker_boost: bool = True
    # Dicionario de pronuncia (siglas de versao/modelo) - fica None ate ser criado via API (veja
    # a API de pronunciation dictionaries da ElevenLabs); enquanto None, o audio usa so o soletrar por regex
    # em app/tts.py como fallback.
    elevenlabs_pronunciation_dictionary_id: str | None = None
    elevenlabs_pronunciation_dictionary_version_id: str | None = None

    helena_base_url: str = "https://api.helena.run"
    # Endpoint usado so para encerrar/reativar o chatbot no atendimento (ver app/helena_client.py).
    helena_wts_base_url: str = "https://api.wts.chat"

    # Producao - numero real de WhatsApp do Bistrô Aurora.
    helena_prod_message_token: str
    helena_prod_tag_token: str
    helena_prod_file_token: str
    helena_prod_panel_id: str | None = None
    helena_prod_step_id_novo_lead: str | None = None
    helena_prod_step_id_em_atendimento: str | None = None
    # Equipe/departamento (core/v1/department) que recebe o atendimento apos a transferencia -
    # NAO confundir com o painel de CRM acima (aquele e o Kanban de vendas, este e a fila de
    # atendimento humano no inbox do Helena).
    helena_prod_department_id: str | None = None

    # Teste - numero separado, para validar mudancas antes de ir para producao.
    helena_test_message_token: str | None = None
    helena_test_tag_token: str | None = None
    helena_test_file_token: str | None = None
    helena_test_panel_id: str | None = None
    helena_test_step_id_novo_lead: str | None = None
    helena_test_step_id_em_atendimento: str | None = None
    helena_test_department_id: str | None = None

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    debounce_seconds: int = 5

    dashboard_username: str = "admin"
    dashboard_password: str = "changeme"

    # Reservas automaticas via Google Calendar (teste) - None ate serem configuradas; nesse
    # caso app/google_calendar_client.py recusa a chamada com um erro claro em vez de falhar
    # com um traceback de arquivo nao encontrado.
    google_calendar_id: str | None = None
    google_credentials_path: str | None = None
    max_capacity_per_slot: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass
class EnvironmentCredentials:
    environment: str
    message_token: str
    tag_token: str
    file_token: str
    panel_id: str | None
    step_id_novo_lead: str | None
    step_id_em_atendimento: str | None
    department_id: str | None


def get_environment_credentials(environment: str) -> EnvironmentCredentials:
    if environment not in ENVIRONMENTS:
        raise ValueError(f"Ambiente desconhecido: {environment!r} (esperado 'teste' ou 'producao')")

    settings = get_settings()

    if environment == "producao":
        return EnvironmentCredentials(
            environment="producao",
            message_token=settings.helena_prod_message_token,
            tag_token=settings.helena_prod_tag_token,
            file_token=settings.helena_prod_file_token,
            panel_id=settings.helena_prod_panel_id,
            step_id_novo_lead=settings.helena_prod_step_id_novo_lead,
            step_id_em_atendimento=settings.helena_prod_step_id_em_atendimento,
            department_id=settings.helena_prod_department_id,
        )

    if not settings.helena_test_message_token:
        raise RuntimeError(
            "Ambiente de teste chamado mas HELENA_TEST_MESSAGE_TOKEN nao esta configurado no .env"
        )

    return EnvironmentCredentials(
        environment="teste",
        message_token=settings.helena_test_message_token,
        tag_token=settings.helena_test_tag_token or settings.helena_test_message_token,
        file_token=settings.helena_test_file_token or settings.helena_test_message_token,
        panel_id=settings.helena_test_panel_id,
        step_id_novo_lead=settings.helena_test_step_id_novo_lead,
        step_id_em_atendimento=settings.helena_test_step_id_em_atendimento,
        department_id=settings.helena_test_department_id,
    )
