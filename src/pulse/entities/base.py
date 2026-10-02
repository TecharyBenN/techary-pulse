"""Base classes for entities."""

from pydantic import BaseModel, ConfigDict


class Entity(BaseModel):
    """A frozen model."""

    model_config = ConfigDict(frozen=True)


class StrictEntity(Entity):
    """A frozen model that rejects extra fields, for agent output and configuration."""

    model_config = ConfigDict(extra="forbid")
