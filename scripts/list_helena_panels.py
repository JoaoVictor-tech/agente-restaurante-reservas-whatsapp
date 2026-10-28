"""Script de uso unico: lista os paineis (kanban) e suas etapas na conta do CRM,
para descobrir HELENA_{PROD,TEST}_PANEL_ID / STEP_ID_NOVO_LEAD / STEP_ID_EM_ATENDIMENTO
a preencher no .env.

Uso:
    python scripts/list_helena_panels.py teste
    python scripts/list_helena_panels.py producao
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_environment_credentials  # noqa: E402
from app.helena_crm import list_panels  # noqa: E402


async def main(environment: str) -> None:
    credentials = get_environment_credentials(environment)
    panels = await list_panels(credentials, include_steps=True)
    if not panels:
        print("Nenhum painel encontrado (ou a resposta da API veio num formato inesperado).")
        return

    for panel in panels:
        print(f"\nPainel: {panel.get('title') or panel.get('name')}  (id: {panel.get('id')})")
        steps = panel.get("steps") or []
        if not steps:
            print("  (sem etapas incluidas na resposta - confirme o parametro IncludeDetails)")
        for step in steps:
            print(f"  - etapa: {step.get('title') or step.get('name')}  (id: {step.get('id')})")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("teste", "producao"):
        print("Uso: python scripts/list_helena_panels.py teste|producao")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
