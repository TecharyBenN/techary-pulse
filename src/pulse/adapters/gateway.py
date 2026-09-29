"""The AI gateway: every model call goes to its OpenAI-compatible chat completions endpoint."""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def gateway_model(base_url: str, api_key: str, model_name: str) -> OpenAIChatModel:
    """A model reached through the gateway, sending no sampling settings."""
    return OpenAIChatModel(model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))
