"""Reading and validating config.yaml."""

from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    ConfigDict,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from pulse.entities.base import StrictEntity
from pulse.entities.errors import PulseError
from pulse.entities.lifecycle import SendRule
from pulse.entities.mail import domain_in

AGENT_NAMES = ("orchestrator", "extractor", "consolidator", "writer", "judge")


class ConfigError(PulseError):
    """config.yaml cannot be read or is invalid."""


class GraphConfig(StrictEntity):
    tenant_id: str
    client_id: str
    certificate_path: Path
    max_retries: PositiveInt


class MailboxesConfig(StrictEntity):
    submissions: str
    conversation: str
    processed_folder: str
    rejected_folder: str


class ScheduleConfig(StrictEntity):
    draft_cron: str | None = None
    poll_interval_seconds: PositiveInt


class OrchestratorConfig(StrictEntity):
    max_tool_calls: PositiveInt
    max_run_minutes: PositiveInt


class ChatConfig(StrictEntity):
    port: PositiveInt
    max_attempts: PositiveInt


class AuthConfig(StrictEntity):
    issuer: str
    audience: str
    # A URL, or a file path, holding the issuer's JSON Web Key Set.
    jwks: str
    reviewer_role: str


class LimitsConfig(StrictEntity):
    max_words: PositiveInt


class SectionConfig(StrictEntity):
    category: str
    title: str
    definition: str


class LlmConfig(StrictEntity):
    base_url: str
    api_key_env: str
    models: dict[str, str]

    @field_validator("models")
    @classmethod
    def _one_model_per_agent(cls, models: dict[str, str]) -> dict[str, str]:
        if set(models) != set(AGENT_NAMES):
            raise ValueError(f"llm.models must have exactly one entry for each of {AGENT_NAMES}")
        return models


class StateConfig(StrictEntity):
    db_path: Path


class Config(StrictEntity):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    graph: GraphConfig
    mailboxes: MailboxesConfig
    reviewers: str
    all_staff: str
    operator_alerts: list[str]
    allowed_sender_domains: list[str]
    allowed_senders: list[str]
    allowed_recipient_domains: list[str]
    allowed_sensitivity_labels: list[str]
    timezone: ZoneInfo
    schedule: ScheduleConfig
    send: SendRule
    orchestrator: OrchestratorConfig
    chat: ChatConfig
    auth: AuthConfig
    limits: LimitsConfig
    subject_template: str
    headline_title: str
    sections: list[SectionConfig]
    llm: LlmConfig
    state: StateConfig
    retention_days: PositiveInt

    @property
    def categories(self) -> dict[str, str]:
        """Each configured category with its definition, in configuration order."""
        return {section.category: section.definition for section in self.sections}

    @field_validator("timezone", mode="before")
    @classmethod
    def _zone(cls, value: object) -> ZoneInfo:
        if not isinstance(value, str):
            raise ValueError("timezone must be an IANA time zone name")
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown time zone {value!r}") from error

    @field_validator("sections")
    @classmethod
    def _unique_categories(cls, sections: list[SectionConfig]) -> list[SectionConfig]:
        categories = [section.category for section in sections]
        if len(set(categories)) != len(categories):
            raise ValueError("each section needs its own category")
        return sections

    @model_validator(mode="after")
    def _recipients_in_allowed_domains(self) -> Self:
        recipients = [self.reviewers, *self.operator_alerts, self.all_staff]
        outside = [r for r in recipients if not domain_in(r, self.allowed_recipient_domains)]
        if outside:
            raise ValueError(f"recipients outside allowed_recipient_domains: {outside}")
        return self


def load_config(path: Path) -> Config:
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Config.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ConfigError(f"cannot load {path}: {error}") from error
