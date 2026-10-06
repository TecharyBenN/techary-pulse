"""Prompt parts every agent uses: instructions, delimited data blocks, feedback and the configured
categories."""

import json
from collections.abc import Mapping, Sequence
from importlib.resources import files

from pydantic import BaseModel

from pulse.entities.conversation import ReviewerMessage

# Who sent each message is for the store, so agents are not given it.
_FEEDBACK_FIELDS = {"received", "text"}


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
