from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Role(StrEnum):
    MERCHANT = "merchant"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class Workflow(StrEnum):
    QUALIFICATION = "qualification"
    INCIDENT = "incident"
    REMEDIATION = "remediation"


class CaseStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ANALYZING = "analyzing"
    WAITING_REVIEW = "waiting_review"
    NEEDS_INFO = "needs_info"
    APPROVED = "approved"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Actor(BaseModel):
    user_id: str
    role: Role
    merchant_id: str | None = None


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    workflow: Workflow
    merchant_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class CaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    merchant_id: str
    workflow: Workflow
    status: CaseStatus
    revision: int
    payload: dict[str, Any]
    is_demo: bool
    latest_analysis: dict[str, Any] | None


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    payload: dict[str, Any]


class AnalysisCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    analysis_run_id: str = Field(min_length=1, max_length=36)
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("取消说明不能为空")
        return value.strip()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class ToolResult(BaseModel):
    status: Literal["ok", "not_found", "denied", "invalid", "failed"]
    data: Any = None
    evidence_refs: list[str] = Field(default_factory=list)
    error: str | None = None


class AnalysisOutput(BaseModel):
    run_id: str
    thread_id: str
    case_id: str
    revision: int
    engine_mode: Literal["stub", "fake", "live", "partial"]
    branches: dict[str, Any]
    recommendation: str
    elapsed_ms: float | None
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    impact: dict | None = None
