"""Base models: entities are frozen; agent output and configuration also reject extra fields."""

from pydantic import BaseModel, ConfigDict


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)


class StrictEntity(Entity):
    model_config = ConfigDict(extra="forbid")
