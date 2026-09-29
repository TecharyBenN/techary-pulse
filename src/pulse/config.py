"""Configuration model and loader."""

from datetime import time as TimeOfDay
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from pulse.agents import AGENT_NAMES
from pulse.errors import ConfigError


def domain_of(address: str) -> str:
    """Return the lower-case domain of an email address.

    Raises:
        ValueError: If the address does not have exactly one "@" with text either side.
    """
    local, sep, domain = address.strip().rpartition("@")
    if not sep or not local or not domain or "@" in local:
        raise ValueError(f"not an email address: {address!r}")
    return domain.lower()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class GraphConfig(_Model):
    tenant_id: str
    client_id: str
    certificate_path: Path
    max_retries: int = 5


class MailboxesConfig(_Model):
    submissions: str
    conversation: str
    processed_folder: str = "Processed"
    rejected_folder: str = "Rejected"


class StateConfig(_Model):
    db_path: Path


class LimitsConfig(_Model):
    max_words: int


class SectionConfig(_Model):
    category: str
    title: str
    definition: str


class LlmConfig(_Model):
    base_url: str
    api_key_env: str
    models: dict[str, str]


Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class SendConfig(_Model):
    mode: Literal["on_approval", "scheduled"]
    day: Weekday | None = None
    time: TimeOfDay | None = None

    @model_validator(mode="after")
    def _scheduled_needs_day_and_time(self) -> Self:
        if self.mode == "scheduled" and (self.day is None or self.time is None):
            raise ValueError("send.day and send.time are required when send.mode is scheduled")
        return self


class EditionConfig(_Model):
    expire_after_days: int | None = None


class ChatConfig(_Model):
    max_attempts: int = 3


class Config(_Model):
    """Contents of ``config.yaml``."""

    graph: GraphConfig
    mailboxes: MailboxesConfig
    reviewers: list[str]
    all_staff: str
    operator_alerts: list[str]
    allowed_sender_domains: list[str]
    allowed_senders: list[str] = []
    allowed_recipient_domains: list[str]
    allowed_sensitivity_labels: list[str] = []
    timezone: str
    send: SendConfig
    edition: EditionConfig = EditionConfig()
    chat: ChatConfig = ChatConfig()
    limits: LimitsConfig
    subject_template: str
    headline_title: str
    sections: list[SectionConfig]
    llm: LlmConfig
    state: StateConfig
    run_artefacts_dir: Path
    retention_days: int

    @model_validator(mode="after")
    def _models_match_agents(self) -> Self:
        missing = sorted(AGENT_NAMES - self.llm.models.keys())
        unknown = sorted(self.llm.models.keys() - AGENT_NAMES)
        if missing or unknown:
            raise ValueError(
                f"llm.models must name each agent: missing {missing}, unknown {unknown}"
            )
        return self

    @model_validator(mode="after")
    def _recipients_allowed(self) -> Self:
        # The design requires recipients to be checked when configuration loads, so a
        # mistyped or external address stops Pulse before anything is sent.
        allowed = {domain.strip().lower() for domain in self.allowed_recipient_domains}
        for field in ("reviewers", "operator_alerts"):
            for address in getattr(self, field):
                if domain_of(address) not in allowed:
                    raise ValueError(
                        f"{field} address {address!r} is outside allowed_recipient_domains"
                    )
        if domain_of(self.all_staff) not in allowed:
            raise ValueError(
                f"all_staff address {self.all_staff!r} is outside allowed_recipient_domains"
            )
        return self


def is_reviewer(config: Config, address: str) -> bool:
    """Whether ``address`` is in ``reviewers``, compared exactly but ignoring case."""
    return address.strip().lower() in {r.strip().lower() for r in config.reviewers}


def load_config(path: Path) -> Config:
    """Load the configuration file at ``path``.

    Raises:
        ConfigError: If the file cannot be read, is not a YAML mapping, or a recipient is
            outside ``allowed_recipient_domains``.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration {path} must be a YAML mapping")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration {path}:\n{exc}") from exc
