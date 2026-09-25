"""Configuration model and loader."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

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


class MailboxConfig(_Model):
    address: str
    processed_folder: str = "Processed"
    rejected_folder: str = "Rejected"


class ScheduleConfig(_Model):
    cron: str
    timezone: str = "Europe/London"


class LimitsConfig(_Model):
    min_body_chars: int
    max_words: int


class SectionConfig(_Model):
    category: str
    title: str
    definition: str


class LlmConfig(_Model):
    base_url: str
    api_key_env: str
    models: dict[str, str]


class Config(_Model):
    """Contents of ``config.yaml``."""

    graph: GraphConfig
    mailbox: MailboxConfig
    reviewers: list[str]
    operator_alerts: list[str]
    allowed_sender_domains: list[str]
    allowed_senders: list[str] = []
    allowed_recipient_domains: list[str]
    allowed_sensitivity_labels: list[str] = []
    schedule: ScheduleConfig
    limits: LimitsConfig
    subject_template: str
    headline_title: str
    sections: list[SectionConfig]
    llm: LlmConfig
    run_artefacts_dir: Path
    retention_days: int

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
        return self


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
