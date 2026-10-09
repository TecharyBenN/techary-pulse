"""The base classes every agent extends: PulseAgent for every agent, SpecialistAgent for the
specialist agents, and EmailAgent for those that read one screened email."""

import json
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from pydantic_ai import Agent, ModelRetry, NativeOutput, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model, ModelRequestContext

from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import SpecialistFailed
from pulse.entities.mail import ScreenedEmail

_INVALID_FINISH_REASONS = ("length", "content_filter")
# Who sent each message is for the store, so agents are not given it.
_FEEDBACK_FIELDS = {"received", "text"}
# Code attaches the message ID to the record, so the model is not given it.
_EMAIL_FIELDS = {"sender_name", "sender_address", "subject", "received", "unique_body", "body"}


def is_truncated_or_refused(response: ModelResponse) -> bool:
    return response.finish_reason in _INVALID_FINISH_REASONS


class PulseAgent[T, O](Agent[T, O]):
    """An agent whose instructions start with the `prompt.md` beside its module."""

    def __init__(self, model: Model, *, instructions: Sequence[str] = (), **kwargs: Any) -> None:
        package = type(self).__module__.rpartition(".")[0]
        prompt = files(package).joinpath("prompt.md").read_text(encoding="utf-8")
        super().__init__(model, instructions=[prompt, *instructions], **kwargs)


class _Invalid(ModelRetry):
    """An invalid response, retried with the problems found. They are written by code from the
    checks, so they hold no untrusted content."""

    def __init__(self, problems: list[str]) -> None:
        listed = "\n".join(f"- {problem}" for problem in problems)
        super().__init__(f"Your previous response was not valid:\n{listed}")
        self.problems = problems


@dataclass
class _RejectTruncated(AbstractCapability[Any]):
    """Rejects a truncated or refused response before Pydantic AI handles it, so it is retried
    even when it is empty."""

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        if is_truncated_or_refused(response):
            raise _Invalid(["the response was truncated or refused"])
        return response


class SpecialistAgent[T, O](PulseAgent[T, O]):
    """An agent with no tools that answers one task, its run's dependency, with a structured
    output. An invalid response is retried once, continuing the conversation.

    Gateway errors are not retried here, so they fail the orchestrator run, which resumes later.
    """

    def __init__(
        self,
        model: Model,
        *,
        name: str,
        deps_type: type[T],
        output_type: type[O],
        instructions: Sequence[str] = (),
    ) -> None:
        super().__init__(
            model,
            instructions=instructions,
            name=name,
            deps_type=deps_type,
            output_type=NativeOutput(output_type),
            retries={"output": 1},
            capabilities=[_RejectTruncated()],
        )
        self.output_validator(self._validate)

    async def answer(self, task: T) -> O:
        """Return the first valid output; raise SpecialistFailed when the retry is invalid too."""
        try:
            result = await self.run(self.message(task), deps=task)
        except UnexpectedModelBehavior as error:
            # A response that did not match the output type has no problems of its own.
            problems = ["the response did not match the output type"]
            cause = error.__cause__
            while cause is not None:
                if isinstance(cause, _Invalid):
                    problems = cause.problems
                    break
                cause = cause.__cause__
            raise SpecialistFailed(
                f"{self.name} gave no valid response: {'; '.join(problems)}"
            ) from error
        return result.output

    @abstractmethod
    def message(self, task: T) -> str:
        """The task as the message the agent is run with. Untrusted content stays inside
        delimited blocks, never in the instructions."""

    def checks(self, output: O, task: T) -> list[str]:
        """The problems the output type cannot express."""
        return []

    async def _validate(self, ctx: RunContext[T], output: O) -> O:
        if problems := self.checks(output, ctx.deps):
            raise _Invalid(problems)
        return output

    @staticmethod
    def _data_block(tag: str, data: object) -> str:
        """Untrusted data as JSON inside a tagged block, which the instructions say is data."""
        return f"<{tag}>\n{json.dumps(data, ensure_ascii=False)}\n</{tag}>"

    @classmethod
    def _feedback_block(cls, feedback: Sequence[ReviewerMessage]) -> str:
        """Every reviewer message about the newsletter, oldest first, as untrusted data."""
        data = [message.model_dump(mode="json", include=_FEEDBACK_FIELDS) for message in feedback]
        return cls._data_block("feedback", data)

    @staticmethod
    def _listed(values: Mapping[str, str]) -> str:
        """Each key and its value on its own line, for instructions built from configuration."""
        return "\n".join(f"- {key}: {value}" for key, value in values.items())

    @classmethod
    def _categories_instruction(cls, categories: Mapping[str, str]) -> str:
        """`categories` maps each configured category to its definition, from configuration, so
        it may sit in the instructions."""
        listed = cls._listed(categories)
        return f"The configured categories are:\n<categories>\n{listed}\n</categories>"


class EmailAgent[O](SpecialistAgent[ScreenedEmail, O]):
    """A specialist agent that reads one screened email."""

    def __init__(
        self, model: Model, *, name: str, output_type: type[O], instructions: Sequence[str] = ()
    ) -> None:
        super().__init__(
            model,
            name=name,
            deps_type=ScreenedEmail,
            output_type=output_type,
            instructions=instructions,
        )

    def message(self, task: ScreenedEmail) -> str:
        return self._data_block("email", task.model_dump(mode="json", include=_EMAIL_FIELDS))
