"""Pydantic models for structured semantic knowledge objects."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class KnowledgeKind(str, Enum):
    """Kind of structured knowledge object stored in the knowledge base."""

    REPOSITORY = "Repository"
    MICROSERVICE = "Microservice"
    BUSINESS_CAPABILITY = "BusinessCapability"
    EXECUTION_FLOW = "ExecutionFlow"
    BUSINESS_FLOW = "BusinessFlow"
    TECHNICAL_FLOW = "TechnicalFlow"
    PACKAGE = "Package"
    MODULE = "Module"
    CLASS = "Class"
    INTERFACE = "Interface"
    METHOD = "Method"
    API_ENDPOINT = "ApiEndpoint"
    DTO = "DTO"
    ENTITY = "Entity"
    CONFIGURATION = "Configuration"
    SQL_QUERY = "SqlQuery"
    EXCEPTION = "Exception"


class MethodInput(BaseModel):
    """One parameter of a documented method contract."""

    name: str
    type: str
    annotations: list[str] = Field(default_factory=list)


class MethodOutput(BaseModel):
    """Return type of a documented method contract."""

    type: str


class KnowledgeMethodContract(BaseModel):
    """Structured contract describing what a method does, calls, and throws."""

    purpose: str
    inputs: list[MethodInput] = Field(default_factory=list)
    outputs: MethodOutput
    callers: list[str] = Field(default_factory=list)
    callees: list[str] = Field(default_factory=list)
    exceptions_thrown: list[str] = Field(default_factory=list)
    exceptions_caught: list[str] = Field(default_factory=list)
    database_tables_used: list[str] = Field(default_factory=list)
    apis_called: list[str] = Field(default_factory=list)
    business_purpose: str


class KnowledgeObject(BaseModel):
    """A single structured knowledge record (class/method/endpoint/etc.) in objects.jsonl."""

    id: str
    kind: KnowledgeKind
    repository: str
    microservice: str
    module: str | None = None
    package: str | None = None
    name: str
    qualified_name: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    method: KnowledgeMethodContract | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
