"""What every agent's prompts share: instructions read from the agent's folder, untrusted data
in delimited blocks, and the configured categories."""

import json
from collections.abc import Mapping
from importlib.resources import files


def read_instructions(package: str | None) -> str:
    """The `prompt.md` beside an agent's module, passed its `__package__`."""
    return files(package).joinpath("prompt.md").read_text(encoding="utf-8")


def data_block(tag: str, data: object) -> str:
    """Untrusted data as JSON inside a tagged block, which the instructions say is data."""
    return f"<{tag}>\n{json.dumps(data, ensure_ascii=False)}\n</{tag}>"


def listed(values: Mapping[str, str]) -> str:
    """Each key and its value on its own line, for instructions built from configuration."""
    return "\n".join(f"- {key}: {value}" for key, value in values.items())


def categories_instruction(categories: Mapping[str, str]) -> str:
    """`categories` maps each configured category to its definition, from configuration, so
    it may sit in the instructions."""
    return f"The configured categories are:\n{listed(categories)}"
