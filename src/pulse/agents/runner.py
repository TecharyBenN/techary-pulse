"""Runs any agent through Pydantic AI."""

import os
from collections.abc import Mapping

from pydantic import BaseModel
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import ModelRetry, NativeOutput
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.agents.base import Agent
from pulse.config import LlmConfig
from pulse.errors import AgentResponseError, ConfigError, GatewayError

# Pydantic AI otherwise prints a banner to standard output, which would break the JSON logs.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class AgentRunner:
    """Runs agents against the gateway, or against stand-in models in tests."""

    def __init__(self, llm: LlmConfig, models: Mapping[str, Model] | None = None) -> None:
        self._llm = llm
        self._models = dict(models) if models is not None else self._gateway_models(llm)

    @staticmethod
    def _gateway_models(llm: LlmConfig) -> dict[str, Model]:
        try:
            api_key = os.environ[llm.api_key_env]
        except KeyError as exc:
            raise ConfigError(f"environment variable {llm.api_key_env} is not set") from exc
        provider = OpenAIProvider(base_url=llm.base_url, api_key=api_key)
        return {
            name: OpenAIChatModel(model, provider=provider) for name, model in llm.models.items()
        }

    def run[InputT, OutputT: BaseModel](
        self, agent: Agent[InputT, OutputT], data: InputT
    ) -> OutputT:
        """Run one agent call, retrying an invalid response once.

        Raises:
            AgentResponseError: If the second response is also invalid.
            GatewayError: If the gateway fails.
        """
        pydantic_agent = PydanticAgent(
            output_type=NativeOutput(agent.output_type, strict=True),
            instructions=agent.instructions(),
            retries=1,
        )

        last_failure: list[str] = []

        def check(output: OutputT) -> OutputT:
            failures = agent.check_output(data, output)
            if failures:
                last_failure[:] = failures
                raise ModelRetry("; ".join(failures))
            return output

        pydantic_agent.output_validator(check)
        try:
            result = pydantic_agent.run_sync(
                agent.build_message(data), model=self._models[agent.name]
            )
        except UnexpectedModelBehavior as exc:
            detail = "; ".join(last_failure) or str(exc.__cause__ or exc)
            raise AgentResponseError(agent.name, agent.subject(data), detail) from exc
        except ModelAPIError as exc:
            raise GatewayError(f"{agent.name}: {type(exc).__name__}") from exc
        return result.output
