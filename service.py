# -*- coding: utf-8 -*-
"""
FastAPI (mesmo servidor, porta 7001):
  - GET  /healthz       -> saúde
  - POST /conversation  -> get_conversation_simple
  - POST /auto_assign   -> auto_assign_team_then_agent (somente_equipe como string)

Autenticação via cabeçalho: x-api-key
PUBLIC_API_KEY (padrão = CHATWOOT_TOKEN) pode ser definido via env para diferenciar da chave do Chatwoot.
"""

import os
import hmac
from typing import Optional, Union

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from chatwoot_balancer import (
    get_conversation_simple,
    auto_assign_team_then_agent,
)

# Carrega o .env na inicialização
load_dotenv()

# Permite separar a chave pública da chave do Chatwoot
PUBLIC_API_KEY = os.getenv("CHATWOOT_TOKEN")
if not PUBLIC_API_KEY:
    raise RuntimeError("Defina CHATWOOT_TOKEN no .env")

def _check_api_key(x_api_key: Optional[str]):
    if not x_api_key or not hmac.compare_digest(x_api_key, PUBLIC_API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida ou ausente.")

# ===========================
# Modelos de entrada
# ===========================

class ConversationIn(BaseModel):
    conversation_id: int = Field(..., description="ID da conversa no Chatwoot")

class AutoAssignIn(BaseModel):
    conversation_id: int = Field(..., description="ID da conversa no Chatwoot")
    team_id: int = Field(..., description="ID do time no Chatwoot")
    motivo: str = Field(..., description="Motivo objetivo da transferência")
    prioridade: Union[str, int] = Field("low", description="low|medium|high|urgent (ou 0..3)")
    observacoes: Optional[str] = Field(None, description="Detalhes adicionais")

# ===========================
# App
# ===========================

app = FastAPI(
    title="Chatwoot Orchestrator API",
    version="1.1.0",
    description=(
        "Endpoints para status simplificado e auto-atribuição. "
        "Use /auto_assign com somente_equipe='true' (ou equivalentes) para atribuir apenas ao time."
    ),
)

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/conversation")
def conversation_status(payload: ConversationIn, x_api_key: Optional[str] = Header(None, alias="x-api-key")):
    _check_api_key(x_api_key)
    result = get_conversation_simple(payload.conversation_id)
    if isinstance(result, dict) and result.get("error"):
        # erro upstream (Chatwoot ou rede)
        raise HTTPException(status_code=502, detail=result)
    return result

@app.post("/auto_assign")
def auto_assign(payload: AutoAssignIn, x_api_key: Optional[str] = Header(None, alias="x-api-key")):
    _check_api_key(x_api_key)
    result = auto_assign_team_then_agent(
        conversation_id=payload.conversation_id,
        team_id=payload.team_id,
        motivo=payload.motivo,
        prioridade=payload.prioridade,
        observacoes=payload.observacoes
    )
    # se falha crítica, retorna erro; se só a msg privada falhar, ainda devolve status simplificado c/ metadados
    if isinstance(result, dict) and result.get("error") and result.get("step") != "message_error":
        raise HTTPException(status_code=502, detail=result)
    return result