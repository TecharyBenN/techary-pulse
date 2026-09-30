"""Runs any specialist agent: validates its answer and retries once."""

from collections.abc import Callable

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse

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
    problems: list[str] = []
    for _ in range(_ATTEMPTS):
        try:
            # This runner owns the retry, so Pydantic AI's own output retries are turned off.
            result = await agent.run(prompt, retries={"output": 0})
        except UnexpectedModelBehavior:
            problems = ["the response did not match the output type"]
            continue
        if is_truncated_or_refused(result.response):
            problems = ["the response was truncated or refused"]
            continue
        problems = output_checks(result.output)
        if not problems:
            return result.output
    raise SpecialistFailed(f"{agent.name} gave no valid response: {'; '.join(problems)}")
