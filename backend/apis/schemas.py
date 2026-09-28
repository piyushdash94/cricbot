"""Request models for the Cricbot HTTP API."""

from typing import Any

from pydantic import BaseModel, Field


class ScorecardOptions(BaseModel):
    top_n: int = Field(default=3, ge=1, le=10)
    max_chars: int = Field(default=1900, ge=200, le=10000)
    phases: bool = True
    charts: bool = True
    partnerships: bool = True
    fow: bool = False


class ScorecardRequest(BaseModel):
    info: dict[str, Any] = Field(default_factory=dict)
    scorecard: dict[str, Any]
    options: ScorecardOptions = Field(default_factory=ScorecardOptions)


class ScorecardOnlyRequest(BaseModel):
    scorecard: dict[str, Any]


class ViewPayload(BaseModel):
    payload: Any
    limit: int | None = Field(default=None, ge=1, le=50)


class PlayerCardRequest(BaseModel):
    bio: dict[str, Any]
    career: dict[str, Any] | None = None
    format: str = "test"


class MatchPreviewRequest(BaseModel):
    info: dict[str, Any]
    ground: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    ui_context: dict[str, Any] = Field(default_factory=dict)
