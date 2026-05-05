from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .compat import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ErrorStatus(str, Enum):
    OPEN = "open"
    IN_ANALYSIS = "in_analysis"
    FIX_PLANNED = "fix_planned"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class StackFrame(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw: str
    namespace: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    file_path: str | None = None
    line_number: int | None = None


class LogEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: datetime = Field(default_factory=utc_now)
    service: str | None = None
    environment: str | None = None
    log_level: str = "ERROR"
    exception_type: str | None = None
    error_message: str = ""
    exception_message: str = ""
    detailed_exception: str = ""
    stack_trace: str = ""
    fingerprint: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    inferred_severity: str | None = None
    document_id: str | None = None
    log_attributes: dict[str, Any] = Field(default_factory=dict)
    log_fields: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    frames: list[StackFrame] = Field(default_factory=list)


class ErrorRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    exception_type: str | None = None
    message: str = ""
    service: str | None = None
    environment: str | None = None
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int = 1
    status: ErrorStatus = ErrorStatus.OPEN
    pr_url: str | None = None


class ResolutionTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    log_event: LogEvent
    status: ErrorStatus = ErrorStatus.OPEN
    created_at: datetime = Field(default_factory=utc_now)
    occurrence_count: int = 1


class CodeFixPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    root_cause_summary: str
    files_to_inspect: list[str] = Field(default_factory=list)
    files_allowed_to_edit: list[str] = Field(default_factory=list)
    suggested_fix: str
    suggested_tests: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class PullRequestResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dry_run: bool = True
    branch_name: str
    commit_message: str
    pr_title: str
    pr_body: str
    pr_url: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approved: bool = False
    findings: list[str] = Field(default_factory=list)
    residual_risk: str = ""
