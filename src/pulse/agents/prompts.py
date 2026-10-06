"""Prompt parts the agents share: instructions, delimited data blocks, the screened email, feedback
and the configured categories."""

import json
from collections.abc import Mapping, Sequence
from importlib.resources import files

from pydantic import BaseModel

from pulse.entities.conversation import ReviewerMessage
from pulse.entities.mail import ScreenedEmail

# Who sent each message is for the store, so agents are not given it.
_FEEDBACK_FIELDS = {"received", "text"}
# Code attaches the message ID to the record, so the model is not given it.
_EMAIL_FIELDS = {"sender_name", "sender_address", "subject", "received", "unique_body", "body"}


def read_instructions(package: str | None) -> str:
    """The `prompt.md` beside an agent's module, passed its `__package__`."""
    return files(package).joinpath("prompt.md").read_text(encoding="utf-8")


def data_block(tag: str, data: object) -> str:
    """Untrusted data as JSON inside a tagged block, which the instructions say is data."""
    return f"<{tag}>\n{json.dumps(data, ensure_ascii=False)}\n</{tag}>"


def keyed(model: BaseModel, key: str, fields: set[str]) -> dict[str, object]:
    """The model's `fields` as JSON data with `key` first, so an agent reads which item or
    record it is before its detail."""
    data = model.model_dump(mode="json", include=fields)
    return {key: data.pop(key), **data}


def email_prompt(email: ScreenedEmail) -> str:
    """One screened email for the agents that read it, the extractor and the sensitivity agent.
    The email stays inside a delimited block, never in the instructions."""
    return data_block("email", email.model_dump(mode="json", include=_EMAIL_FIELDS))


def feedback_block(feedback: Sequence[ReviewerMessage]) -> str:
    """Every reviewer message about the newsletter, oldest first, as untrusted data."""
    data = [message.model_dump(mode="json", include=_FEEDBACK_FIELDS) for message in feedback]
    return data_block("feedback", data)


def listed(values: Mapping[str, str]) -> str:
    """Each key and its value on its own line, for instructions built from configuration."""
    return "\n".join(f"- {key}: {value}" for key, value in values.items())


def categories_instruction(categories: Mapping[str, str]) -> str:
    """`categories` maps each configured category to its definition, from configuration, so
    it may sit in the instructions."""
    return f"The configured categories are:\n{listed(categories)}"
