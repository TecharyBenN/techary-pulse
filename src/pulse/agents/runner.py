"""Runs any specialist agent: validates its answer and retries once, telling it what was wrong."""

from collections.abc import Callable

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse

from pulse.entities.errors import SpecialistFailed

_ATTEMPTS = 2
_INVALID_FINISH_REASONS = ("length", "content_filter")


def is_truncated_or_refused(response: ModelResponse) -> bool:
    return response.finish_reason in _INVALID_FINISH_REASONS


async def run_specialist[T](
    agent: Agent[None, T], prompt: str, output_checks: Callable[[T], list[str]]
) -> T:
    """Return the first valid output; raise SpecialistFailed when the retry is invalid too.

    Gateway errors are not retried here, so they fail the orchestrator run, which resumes later.
    """
    history: list[ModelMessage] = []
    message = prompt
    problems: list[str] = []
    for _ in range(_ATTEMPTS):
        with capture_run_messages() as messages:
            try:
                # This runner owns the retry, so Pydantic AI's own output retries are turned off.
                result = await agent.run(message, message_history=history, retries={"output": 0})
            except UnexpectedModelBehavior:
                problems = ["the response did not match the output type"]
            else:
                if is_truncated_or_refused(result.response):
                    problems = ["the response was truncated or refused"]
                else:
                    problems = output_checks(result.output)
                    if not problems:
                        return result.output
        # The retry continues the conversation, so the agent sees its answer and what was wrong.
        history = list(messages)
        message = _correction(problems)
    raise SpecialistFailed(f"{agent.name} gave no valid response: {'; '.join(problems)}")


def _correction(problems: list[str]) -> str:
    """Written by code from the output checks, so it holds no untrusted content."""
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"Your previous response was not valid:\n{listed}\n"
        "Return a complete corrected response that fixes every problem."
    )
