"""Request and response schemas for POST /v1/chat-assistant endpoint."""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Request body for POST /v1/chat-assistant endpoint."""

    user_id: str
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    """Response body for POST /v1/chat-assistant."""

    response: str
