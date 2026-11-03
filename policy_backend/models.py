"""Wire contract shared by the policy service and Gateway bridge."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: Literal["https://accounts.google.com", "accounts.google.com"]
    subject: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: ActorIdentity
    provider: str = Field(min_length=1, max_length=80)
    tool: str = Field(min_length=1, max_length=200)
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_ref: str = Field(min_length=1, max_length=500)
    parent_source: str | None = Field(default=None, max_length=200)
    operation: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=200)
    preview: bool = False


class DecisionBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[DecisionRequest] = Field(min_length=1, max_length=100)


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    actor_id: str | None = None
    release: str | None = None
    reservation_id: str | None = None
    grant_tool: str | None = None


class GrantUseCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str
    request_id: str
    status: Literal["committed", "released", "uncertain"]
