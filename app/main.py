import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import ENVIRONMENTS
from app.dashboard.routes import router as dashboard_router
from app.pipeline import handle_incoming_webhook
from app.webhook_schemas import HelenaWebhookPayload

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Agente Bistrô Aurora")
app.include_router(dashboard_router, prefix="/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/helena/{environment}")
async def webhook_helena(environment: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    if environment not in ENVIRONMENTS:
        raise HTTPException(status_code=404, detail=f"Ambiente desconhecido: {environment!r}")

    raw = await request.json()
    payload = HelenaWebhookPayload.model_validate(raw)

    if payload.lastMessage is None:
        # Evento de sistema sem mensagem de cliente (ex: troca de bot ativo via #teste-XXX
        # gera um webhook proprio da Helena sem lastMessage) - nada a processar.
        logging.getLogger(__name__).info(
            "Webhook Helena sem lastMessage (evento de sistema) - ignorando. sessionId=%s",
            payload.sessionId,
        )
        return {"received": True, "ignored": "no_last_message"}

    # responde 200 imediatamente - a plataforma nao espera a resposta do agente aqui,
    # o texto/audio de volta e enviado depois via chamada HTTP propria (helena_client).
    background_tasks.add_task(handle_incoming_webhook, payload, environment)

    return {"received": True}
