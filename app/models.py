from __future__ import annotations

import json
import re
import uuid
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .:-]+$")
    action: Literal["record", "charge", "sleep", "fail_once", "fail_until_replay"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_is_small_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, separators=(",", ":")).encode()) > 4096:
            raise ValueError("Each step payload must be at most 4 KiB.")
        return value

    @model_validator(mode="after")
    def validate_action_payload(self) -> "WorkflowStep":
        if self.action == "sleep":
            seconds = self.payload.get("seconds", 0)
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= 30:
                raise ValueError("sleep seconds must be between 0 and 30.")
            if set(self.payload) - {"seconds"}:
                raise ValueError("sleep accepts only a seconds value.")
        if self.action == "charge":
            amount = self.payload.get("amount", 25)
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                raise ValueError("charge amount must be positive.")
        return self


class WebhookTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: UUID
    event_type: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=20)
    trigger: WebhookTriggerRequest | None = None


class DuplicateEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(default_factory=lambda: f"order-{uuid.uuid4()}", min_length=1, max_length=120, pattern=r"^[\w.:-]+$")
    order: str = Field(default="demo-order", min_length=1, max_length=80)
    amount: float = Field(default=25, gt=0, le=100000)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class DemoFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Demo DLQ failure", min_length=1, max_length=120)


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def workspace_name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name must contain a visible character.")
        return value


class WorkspaceMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["OWNER", "ADMIN", "DEVELOPER", "VIEWER"]


class WebhookCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def webhook_name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Webhook name must contain a visible character.")
        return value


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["github", "slack", "http"]
    name: str = Field(min_length=1, max_length=100)
    secret: SecretStr

    @field_validator("name")
    @classmethod
    def credential_name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Credential name must contain a visible character.")
        return value


class CredentialRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: SecretStr


def valid_idempotency_key(value: str) -> str:
    if not re.fullmatch(r"[\w.:-]{1,120}", value):
        raise ValueError("Idempotency-Key may contain letters, digits, dot, underscore, colon, and hyphen (max 120).")
    return value

